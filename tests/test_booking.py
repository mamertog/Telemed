"""Agendamento: calendário, horários, persistência, conflitos e concorrência."""
import threading
from datetime import datetime, time, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Appointment, AppointmentEvent, AvailabilityException, Patient, PrivacyRecord, Service,
)
from app.services import scheduling
from app.utils import TZ

from .conftest import booking_payload, slot_iso


def test_booking_unavailable_without_configuration(client):
    html = client.get("/agendar/").get_data(as_text=True)
    assert "Agenda on-line em preparação" in html


def test_calendar_and_free_slots(client, agenda):
    html = client.get("/agendar/").get_data(as_text=True)
    assert "Consulta de teste" in html and 'id="calDays"' in html
    d = agenda["date"]
    r = client.get(f"/agendar/api/dias?servico={agenda['service_id']}&mes={d.strftime('%Y-%m')}")
    assert r.status_code == 200
    assert d.isoformat() in r.get_json()["dias"]
    r = client.get(f"/agendar/api/horarios?servico={agenda['service_id']}&data={d.isoformat()}")
    labels = [h["rotulo"] for h in r.get_json()["horarios"]]
    assert labels[0] == "08:00" and labels[-1] == "17:30"
    assert "12:00" not in labels and "12:30" not in labels  # intervalo
    assert len(labels) == 18


def test_slots_respect_duration_and_inactive_service(client, app, agenda):
    with app.app_context():
        svc = db.session.get(Service, agenda["service_id"])
        svc.duration_minutes = 60
        db.session.commit()
    r = client.get(f"/agendar/api/horarios?servico={agenda['service_id']}&data={agenda['date'].isoformat()}")
    labels = [h["rotulo"] for h in r.get_json()["horarios"]]
    assert "11:30" not in labels and "17:30" not in labels and "17:00" in labels
    with app.app_context():
        db.session.get(Service, agenda["service_id"]).active = False
        db.session.commit()
    assert client.get(f"/agendar/api/horarios?servico={agenda['service_id']}&data=2030-01-01").status_code == 404


def test_create_booking_persists_in_postgres(client, app, agenda):
    r = client.post("/agendar/confirmar", data=booking_payload(agenda))
    assert r.status_code == 302 and r.headers["Location"].endswith("/agendar/sucesso")
    page = client.get("/agendar/sucesso").get_data(as_text=True)
    assert "Solicitação registrada" in page
    with app.app_context():
        appt = db.session.execute(sa.select(Appointment)).scalar_one()
        assert appt.status == "solicitado"
        assert appt.reference in page
        assert appt.start_at == datetime.combine(agenda["date"], time(9), tzinfo=TZ)
        assert appt.start_at.utcoffset() is not None
        assert appt.patient.full_name == "Paciente Ficticio Silva"
        assert appt.patient.phone_norm == "19988880001"
        assert db.session.scalar(sa.select(sa.func.count(AppointmentEvent.id))) == 1
        pr = db.session.execute(sa.select(PrivacyRecord)).scalars().all()
        assert [p.record_type for p in pr] == ["aviso_privacidade_ciencia"]
        assert pr[0].patient_id == appt.patient_id
    # O horário deixa de ser oferecido
    r = client.get(f"/agendar/api/horarios?servico={agenda['service_id']}&data={agenda['date'].isoformat()}")
    assert "09:00" not in [h["rotulo"] for h in r.get_json()["horarios"]]


def test_booking_requires_privacy_ack_and_valid_data(client, app, agenda):
    data = booking_payload(agenda)
    data.pop("privacy_ack")
    assert client.post("/agendar/confirmar", data=data).status_code == 422
    assert client.post("/agendar/confirmar", data=booking_payload(agenda, phone="123")).status_code == 422
    assert client.post("/agendar/confirmar", data=booking_payload(agenda, name="Fulano")).status_code == 422
    assert client.post("/agendar/confirmar", data=booking_payload(agenda, website="spam")).status_code == 422
    with app.app_context():
        assert db.session.scalar(sa.select(sa.func.count(Appointment.id))) == 0


def test_booking_outside_availability_rejected(client, app, agenda):
    for hh, mm in [(12, 0), (7, 30), (18, 0), (9, 10)]:
        r = client.post("/agendar/confirmar", data=booking_payload(agenda, hh=hh, mm=mm))
        assert r.status_code == 422
    with app.app_context():
        assert db.session.scalar(sa.select(sa.func.count(Appointment.id))) == 0


def test_conflicting_booking_blocked(client, app, agenda):
    assert client.post("/agendar/confirmar", data=booking_payload(agenda)).status_code == 302
    r = client.post("/agendar/confirmar", data=booking_payload(agenda, name="Outra Pessoa Teste", phone="(11) 97777-0002"))
    assert r.status_code == 422
    assert "indisponível" in r.get_data(as_text=True) or "reservado" in r.get_data(as_text=True)
    with app.app_context():
        assert db.session.scalar(sa.select(sa.func.count(Appointment.id))) == 1


def test_concurrent_booking_same_slot_only_one_succeeds(app, agenda):
    n = 8
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def worker(i):
        with app.app_context():
            barrier.wait()
            try:
                scheduling.create_public_booking(
                    service_id=agenda["service_id"], start_iso=slot_iso(agenda["date"], 10),
                    full_name=f"Paciente Concorrente {i}", phone=f"(11) 9{i:04d}-1234", email=None,
                    birth_date=None, whatsapp_opt_in=False)
                outcome = "ok"
            except scheduling.SlotUnavailable:
                outcome = "conflito"
            finally:
                db.session.remove()
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count("conflito") == n - 1
    with app.app_context():
        assert db.session.scalar(sa.select(sa.func.count(Appointment.id)).where(
            Appointment.status.in_(["solicitado", "confirmado"]))) == 1


def test_database_exclusion_constraint(app, agenda):
    """Mesmo contornando a aplicação, o PostgreSQL rejeita sobreposição de agendamentos ativos."""
    with app.app_context():
        p = scheduling.build_patient("Paciente Banco Teste", "11 98888-7777")
        db.session.add(p)
        start = datetime.combine(agenda["date"], time(14), tzinfo=TZ)
        db.session.add(Appointment(reference="TEST-0001", patient=p, service_id=agenda["service_id"],
                                   start_at=start, end_at=start + timedelta(minutes=30), status="confirmado"))
        db.session.commit()
        db.session.add(Appointment(reference="TEST-0002", patient=p, service_id=agenda["service_id"],
                                   start_at=start + timedelta(minutes=15), end_at=start + timedelta(minutes=45),
                                   status="solicitado"))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        # Cancelados não bloqueiam o horário
        db.session.add(Appointment(reference="TEST-0003", patient=p, service_id=agenda["service_id"],
                                   start_at=start, end_at=start + timedelta(minutes=30), status="cancelado"))
        db.session.commit()


def test_exception_blocks_slots(client, app, agenda):
    with app.app_context():
        d = agenda["date"]
        db.session.add(AvailabilityException(start_at=datetime.combine(d, time(8), tzinfo=TZ),
                                             end_at=datetime.combine(d, time(10), tzinfo=TZ), reason="Feriado"))
        db.session.commit()
    r = client.get(f"/agendar/api/horarios?servico={agenda['service_id']}&data={agenda['date'].isoformat()}")
    labels = [h["rotulo"] for h in r.get_json()["horarios"]]
    assert labels[0] == "10:00"


def test_patient_matching_avoids_duplicates(client, app, agenda):
    client.post("/agendar/confirmar", data=booking_payload(agenda, hh=9))
    # mesmo nome (com variação de acento/caixa) e mesmo telefone → mesmo cadastro
    client.post("/agendar/confirmar", data=booking_payload(agenda, hh=10, name="paciente fictício SILVA",
                                                           phone="+55 19 98888-0001"))
    # mesmo telefone, nome diferente (ex.: familiar) → cadastro separado
    client.post("/agendar/confirmar", data=booking_payload(agenda, hh=11, name="Outro Familiar Silva"))
    with app.app_context():
        assert db.session.scalar(sa.select(sa.func.count(Appointment.id))) == 3
        assert db.session.scalar(sa.select(sa.func.count(Patient.id))) == 2


def test_patient_manage_link_and_cancel(client, app, agenda):
    client.post("/agendar/confirmar", data=booking_payload(agenda))
    with client.session_transaction() as s:
        token = s["booking_done"]["token"]
    page = client.get(f"/agendar/agendamento/{token}")
    assert page.status_code == 200 and "Cancelar agendamento" in page.get_data(as_text=True)
    r = client.post(f"/agendar/agendamento/{token}/cancelar")
    assert r.status_code == 302
    with app.app_context():
        appt = db.session.execute(sa.select(Appointment)).scalar_one()
        assert appt.status == "cancelado" and appt.cancel_reason == "paciente_solicitou"
    assert client.get("/agendar/agendamento/token-invalido").status_code == 404


def test_lookup_by_reference_and_phone(client, app, agenda):
    client.post("/agendar/confirmar", data=booking_payload(agenda))
    with app.app_context():
        ref = db.session.execute(sa.select(Appointment.reference)).scalar_one()
    r = client.post("/agendar/consultar", data={"reference": ref, "phone": "19988880001"})
    assert r.status_code == 302 and "/agendar/agendamento/" in r.headers["Location"]
    r = client.post("/agendar/consultar", data={"reference": ref, "phone": "11900000000"})
    assert r.status_code == 200 and "Não foi possível localizar" in r.get_data(as_text=True)


def test_doctor_alert_without_patient_email(client, agenda, monkeypatch):
    from app.services import notifications
    sent = []
    client.application.config.update(EMAIL_PROVIDER="brevo", EMAIL_API_KEY="fake", EMAIL_FROM="sender@example.com", BOOKING_ALERT_EMAIL="doctor@example.com", PUBLIC_BASE_URL="https://clinic.example")
    monkeypatch.setattr(notifications, "send_email", lambda to, subject, body: sent.append((to, subject, body)) or True)
    payload = booking_payload(agenda)
    payload["email"] = ""
    response = client.post("/agendar/confirmar", data=payload)
    assert response.status_code == 302
    assert len(sent) == 1
    assert sent[0][0] == "doctor@example.com"
    assert "https://clinic.example/painel/" in sent[0][2]
    assert "/agendar/agendamento/" not in sent[0][2]


def test_success_whatsapp_message(client, app, agenda):
    import html
    import re
    from urllib.parse import urlparse, parse_qs
    from app.services import settings
    with app.app_context():
        settings.set_many({"whatsapp": "19999990000"})
        db.session.commit()
    assert client.post("/agendar/confirmar", data=booking_payload(agenda)).status_code == 302
    page = client.get("/agendar/sucesso").get_data(as_text=True)
    match = re.search(r'href="(https://wa.me/[^"]+)"', page)
    assert match
    link = urlparse(html.unescape(match.group(1)))
    assert link.path == "/5519999990000"
    message = parse_qs(link.query)["text"][0]
    assert "09:00" in message
    assert "Paciente Ficticio" not in message
    assert "ficticio@example.com" not in message
    assert "/agendar/agendamento/" not in message
    with app.app_context():
        appt = db.session.execute(sa.select(Appointment)).scalar_one()
        assert appt.reference in message
        assert appt.status == "solicitado"


def test_success_without_whatsapp_number(client, agenda):
    assert client.post("/agendar/confirmar", data=booking_payload(agenda)).status_code == 302
    page = client.get("/agendar/sucesso").get_data(as_text=True)
    assert "Avisar o consult" not in page
