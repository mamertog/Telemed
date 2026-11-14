"""Painel: agenda, status, reagendamento, pacientes, serviços e configurações."""
import sqlalchemy as sa

from app.extensions import db
from app.models import Appointment, AppointmentEvent, AuditLog, AvailabilityException, Patient, Service, SiteSetting

from .conftest import booking_payload


def _book(client, agenda, **kw):
    r = client.post("/agendar/confirmar", data=booking_payload(agenda, **kw))
    assert r.status_code == 302
    client.get("/agendar/sucesso")


def _appt(app):
    with app.app_context():
        return db.session.execute(sa.select(Appointment)).scalar_one()


def test_booking_appears_in_admin_panel(client, app, agenda, users):
    _book(client, agenda)
    from .conftest import login
    login(client, app, "medica@example.com", users["doctor"][1])
    dash = client.get("/painel/").get_data(as_text=True)
    assert "Aguardando confirmação" in dash and "Paciente Ficticio Silva" in dash
    lst = client.get("/painel/agendamentos").get_data(as_text=True)
    assert "Paciente Ficticio Silva" in lst
    for view in ("dia", "semana", "mes"):
        r = client.get(f"/painel/agenda?view={view}&data={agenda['date'].isoformat()}")
        assert r.status_code == 200
        assert "Paciente Ficticio Silva" in r.get_data(as_text=True) or view == "mes"
    appt = _appt(app)
    assert client.get(f"/painel/agendamentos/{appt.id}").status_code == 200


def test_confirm_cancel_and_audit(client, app, agenda, users):
    _book(client, agenda)
    from .conftest import login
    login(client, app, "secretaria@example.com", users["secretary"][1])
    appt = _appt(app)
    r = client.post(f"/painel/agendamentos/{appt.id}/status", data={"status": "confirmado", "reason": "outro"})
    assert r.status_code == 302
    assert _appt(app).status == "confirmado"
    # transição inválida (confirmado → solicitado) é recusada
    client.post(f"/painel/agendamentos/{appt.id}/status", data={"status": "solicitado", "reason": "outro"})
    assert _appt(app).status == "confirmado"
    client.post(f"/painel/agendamentos/{appt.id}/status", data={"status": "cancelado", "reason": "paciente_solicitou"})
    a = _appt(app)
    assert a.status == "cancelado" and a.cancel_reason == "paciente_solicitou"
    with app.app_context():
        events = db.session.execute(sa.select(AppointmentEvent).order_by(AppointmentEvent.id)).scalars().all()
        assert [(e.from_status, e.to_status) for e in events] == [
            (None, "solicitado"), ("solicitado", "confirmado"), ("confirmado", "cancelado")]
        assert events[1].user_id is not None
        actions = db.session.execute(sa.select(AuditLog.action)).scalars().all()
        assert "agendamento_confirmado" in actions and "agendamento_cancelado" in actions


def test_reschedule(client, app, agenda, users):
    _book(client, agenda, hh=9)
    _book(client, agenda, hh=11, name="Segunda Pessoa Teste", phone="11 97777-1111")
    from .conftest import login
    login(client, app, "medica@example.com", users["doctor"][1])
    with app.app_context():
        first = db.session.execute(sa.select(Appointment).order_by(Appointment.start_at)).scalars().first()
    d = agenda["date"].isoformat()
    # conflito com o segundo agendamento
    r = client.post(f"/painel/agendamentos/{first.id}/reagendar", data={"date": d, "time": "11:00"})
    assert r.status_code == 200 and "conflita" in r.get_data(as_text=True)
    # fora da disponibilidade sem autorização explícita
    r = client.post(f"/painel/agendamentos/{first.id}/reagendar", data={"date": d, "time": "19:00"})
    assert "fora da disponibilidade" in r.get_data(as_text=True)
    # horário livre
    r = client.post(f"/painel/agendamentos/{first.id}/reagendar", data={"date": d, "time": "15:00"})
    assert r.status_code == 302
    with app.app_context():
        a = db.session.get(Appointment, first.id)
        assert a.start_at.astimezone().hour is not None
        from app.utils import to_local
        assert to_local(a.start_at).strftime("%H:%M") == "15:00"
        assert any(e.event_type == "reagendado" for e in a.events)


def test_manual_appointment_creation(client, app, agenda, users):
    from .conftest import login
    login(client, app, "secretaria@example.com", users["secretary"][1])
    r = client.post("/painel/pacientes/novo", data={"full_name": "Maria Ficticia Souza", "phone": "(21) 98765-4321",
                                                    "email": "", "status": "ativo"})
    assert r.status_code == 302
    with app.app_context():
        p = db.session.execute(sa.select(Patient)).scalar_one()
    r = client.post(f"/painel/agendamentos/novo?paciente={p.id}", data={
        "patient_id": str(p.id), "service_id": agenda["service_id"], "date": agenda["date"].isoformat(),
        "time": "16:00", "modality": "presencial", "status": "confirmado"})
    assert r.status_code == 302
    a = _appt(app)
    assert a.origin == "painel" and a.status == "confirmado"
    # mesmo horário de novo → conflito
    r = client.post(f"/painel/agendamentos/novo?paciente={p.id}", data={
        "patient_id": str(p.id), "service_id": agenda["service_id"], "date": agenda["date"].isoformat(),
        "time": "16:00", "modality": "presencial", "status": "confirmado"})
    assert r.status_code == 200 and "conflita" in r.get_data(as_text=True)


def test_patient_registry(client, app, users):
    from .conftest import login
    login(client, app, "secretaria@example.com", users["secretary"][1])
    data = {"full_name": "João Ficticio Lima", "phone": "11 91234-5678", "email": "joao@example.com", "status": "ativo"}
    assert client.post("/painel/pacientes/novo", data=data).status_code == 302
    # duplicidade por telefone exige confirmação explícita
    dup = dict(data, full_name="Joana Ficticia Lima", email="")
    r = client.post("/painel/pacientes/novo", data=dup)
    assert r.status_code == 200 and "mesmo telefone" in r.get_data(as_text=True)
    assert client.post("/painel/pacientes/novo", data=dict(dup, confirm_duplicate="y")).status_code == 302
    with app.app_context():
        assert db.session.scalar(sa.select(sa.func.count(Patient.id))) == 2
        p = db.session.execute(sa.select(Patient).where(Patient.email_norm == "joao@example.com")).scalar_one()
    # busca por nome, telefone e e-mail
    assert "João Ficticio Lima" in client.get("/painel/pacientes?q=joao").get_data(as_text=True)
    assert "João Ficticio Lima" in client.get("/painel/pacientes?q=91234").get_data(as_text=True)
    assert "João Ficticio Lima" in client.get("/painel/pacientes?q=joao@example.com").get_data(as_text=True)
    # edição
    r = client.post(f"/painel/pacientes/{p.id}/editar", data={"full_name": "João Ficticio Lima Neto",
                                                             "phone": "11 91234-5678", "email": "joao@example.com",
                                                             "status": "ativo"})
    assert r.status_code == 302
    page = client.get(f"/painel/pacientes/{p.id}").get_data(as_text=True)
    assert "João Ficticio Lima Neto" in page and "Possível cadastro duplicado" in page
    with app.app_context():
        actions = db.session.execute(sa.select(AuditLog.action)).scalars().all()
        assert {"paciente_criado", "paciente_atualizado", "paciente_visualizado"} <= set(actions)


def test_services_and_availability_management(client, app, users):
    from .conftest import login
    login(client, app, "medica@example.com", users["doctor"][1])
    r = client.post("/painel/servicos/novo", data={"name": "Consulta inicial", "description": "Primeira consulta.",
                                                   "duration_minutes": 50, "price": "300.00", "show_price": "y",
                                                   "active": "y", "position": 1, "icon": "heart-pulse"})
    assert r.status_code == 302
    with app.app_context():
        svc = db.session.execute(sa.select(Service)).scalar_one()
        assert svc.active and svc.duration_minutes == 50 and float(svc.price) == 300.0
    client.post(f"/painel/servicos/{svc.id}/alternar")
    with app.app_context():
        assert db.session.get(Service, svc.id).active is False
    r = client.post("/painel/disponibilidade", data={"weekday": 0, "start_time": "08:00", "end_time": "12:00"})
    assert r.status_code == 302
    r = client.post("/painel/disponibilidade", data={"weekday": 0, "start_time": "11:00", "end_time": "13:00"})
    assert "sobrepõe" in r.get_data(as_text=True)
    r = client.post("/painel/bloqueios", data={"start_date": "2030-01-01", "end_date": "2030-01-15", "reason": "Férias"})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.scalar(sa.select(sa.func.count(AvailabilityException.id))) == 1


def test_settings_and_professional_data(client, app, users):
    from .conftest import login
    login(client, app, "medica@example.com", users["doctor"][1])
    r = client.post("/painel/configuracoes", data={"address": "Rua Exemplo de Teste, 100", "city": "Campinas/SP",
                                                   "phone": "(19) 3333-4444", "modality_presencial": "y"})
    assert r.status_code == 302
    html = client.get("/contato").get_data(as_text=True)
    assert "Rua Exemplo de Teste, 100" in html and "(19) 3333-4444" in html
    # dados profissionais exigem senha
    data = {"doctor_name": "Dra. Juliana Gonçalves Rosa", "crm": "CRM-SP 238.870", "rqe": "", "graduation": "x",
            "residency": "y", "current_password": "senha-errada"}
    client.post("/painel/configuracoes/profissional", data=data)
    with app.app_context():
        assert db.session.get(SiteSetting, "graduation") is None
    r = client.post("/painel/configuracoes/profissional", data=dict(data, crm="CRM 1", current_password="Senha-de-teste-123!"))
    assert "Formato esperado" in r.get_data(as_text=True)
    for path in ["/painel/configuracoes/textos", "/painel/configuracoes/agendamento", "/painel/configuracoes/notificacoes",
                 "/painel/configuracoes/foto", "/painel/configuracoes/profissional", "/painel/usuarios", "/painel/auditoria",
                 "/painel/servicos", "/painel/disponibilidade", "/painel/bloqueios", "/painel/pacientes/exportar"]:
        assert client.get(path).status_code == 200, path


def test_photo_upload_is_stored_in_database(client, app, users):
    import io
    from PIL import Image
    from app.models import MediaAsset
    from .conftest import login
    login(client, app, "medica@example.com", users["doctor"][1])
    buf = io.BytesIO()
    Image.new("RGB", (40, 50), (120, 160, 130)).save(buf, "PNG")
    buf.seek(0)
    r = client.post("/painel/configuracoes/foto", data={"photo": (buf, "foto.png"), "authorized": "y"},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    with app.app_context():
        assert db.session.execute(sa.select(MediaAsset)).scalar_one().content_type == "image/webp"
    r = client.get("/media/foto-medica")
    assert r.status_code == 200 and r.mimetype == "image/webp"
