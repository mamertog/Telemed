"""The simplified flow persists a request before opening WhatsApp."""
import re
import uuid
import sqlalchemy as sa
from urllib.parse import parse_qs, urlparse
from app.extensions import db
from app.models import BookingRequest, Patient, Appointment, PrivacyRecord
from app.services import settings


def payload(client, **changes):
    page = client.get("/agendar/").get_data(as_text=True)
    key = re.search(r'name="submission_key"[^>]*value="([^"]+)"', page).group(1)
    data = {"submission_key": key, "full_name": "Paciente Exemplo Silva", "phone": "(19) 98888-0001", "email": "example@example.com", "privacy_ack": "y"}
    data.update(changes)
    return data


def test_form_without_calendar_or_availability(client):
    page = client.get("/agendar/").get_data(as_text=True)
    assert 'id="requestForm"' in page
    assert 'id="calDays"' not in page
    assert 'js/booking.js' not in page


def test_request_is_saved_and_whatsapp_contains_reference(client, app):
    with app.app_context():
        settings.set_many({"whatsapp": "19989811414"})
        db.session.commit()
    data = payload(client)
    response = client.post("/agendar/solicitar", data=data, headers={"Accept": "application/json"})
    assert response.status_code == 200
    result = response.get_json()
    assert result["saved"] is True
    link = urlparse(result["whatsapp"])
    assert link.path == "/5519989811414"
    text = parse_qs(link.query)["text"][0]
    with app.app_context():
        item = db.session.execute(sa.select(BookingRequest)).scalar_one()
        assert item.reference in text
        assert item.patient.full_name == data["full_name"]
        assert item.status == "pendente"
        assert db.session.scalar(sa.select(sa.func.count(Appointment.id))) == 0
        assert db.session.scalar(sa.select(sa.func.count(PrivacyRecord.id))) == 1
    assert data["email"] not in text and data["phone"] not in text
    assert client.get(result["next"]).status_code == 200
    # Repeating the same submission does not duplicate either record.
    assert client.post("/agendar/solicitar", data=data).status_code == 302
    with app.app_context():
        assert db.session.scalar(sa.select(sa.func.count(BookingRequest.id))) == 1
        assert db.session.scalar(sa.select(sa.func.count(Patient.id))) == 1


def test_optional_email_and_no_whatsapp_still_persist(client, app):
    with app.app_context():
        settings.set_many({"whatsapp": ""})
        db.session.commit()
    response = client.post("/agendar/solicitar", data=payload(client, email=""), headers={"Accept": "application/json"})
    assert response.status_code == 200 and response.get_json()["whatsapp"] is None
    assert client.get(response.get_json()["next"]).status_code == 200


def test_invalid_contact_and_missing_privacy_do_not_save(client, app):
    for changes in ({"phone": "123"}, {"privacy_ack": ""}, {"website": "spam"}, {"email": "invalid"}):
        assert client.post("/agendar/solicitar", data=payload(client, **changes)).status_code == 422
    with app.app_context():
        assert db.session.scalar(sa.select(sa.func.count(BookingRequest.id))) == 0
        assert db.session.scalar(sa.select(sa.func.count(Patient.id))) == 0


def test_submission_key_must_belong_to_session(client):
    data = payload(client, submission_key="x" * 40)
    assert client.post("/agendar/solicitar", data=data).status_code == 400


def test_disabled_requests(client, app):
    data = payload(client)
    with app.app_context():
        settings.set_many({"booking_enabled": False})
        db.session.commit()
    assert client.post("/agendar/solicitar", data=data).status_code == 404


def test_request_management_and_role_protection(client, app, users):
    from .conftest import login
    client.post("/agendar/solicitar", data=payload(client, whatsapp_opt_in="y"))
    with app.app_context():
        item = db.session.execute(sa.select(BookingRequest)).scalar_one()
        ident = item.id
    assert client.get("/painel/solicitacoes").status_code == 302
    login(client, app, "medica@example.com", users["doctor"][1])
    assert "Paciente Exemplo Silva" in client.get("/painel/solicitacoes").get_data(as_text=True)
    detail = client.get(f"/painel/solicitacoes/{ident}")
    assert detail.status_code == 200
    assert "wa.me/5519988880001" in detail.get_data(as_text=True)
    assert client.post(f"/painel/solicitacoes/{ident}", data={"status": "em_contato"}).status_code == 302
    assert client.post(f"/painel/solicitacoes/{ident}", data={"status": "forged"}).status_code == 200
    with app.app_context():
        assert db.session.get(BookingRequest, ident).status == "em_contato"
    assert client.get(f"/painel/solicitacoes/{uuid.uuid4()}").status_code == 404
    client.post("/acesso/sair")
    login(client, app, "tecnico@example.com", users["tech"][1])
    assert client.get("/painel/solicitacoes").status_code == 403
    assert client.post(f"/painel/solicitacoes/{ident}", data={"status": "concluida"}).status_code == 403


def test_csrf_protects_request_form(client, monkeypatch):
    monkeypatch.setitem(client.application.config, "WTF_CSRF_ENABLED", True)
    assert client.post("/agendar/solicitar", data={"full_name": "Paciente Exemplo"}).status_code == 400


def test_json_validation_preserves_no_records(client, app):
    response = client.post("/agendar/solicitar", data=payload(client, phone="123"), headers={"Accept": "application/json"})
    assert response.status_code == 422 and "phone" in response.get_json()["errors"]
    with app.app_context():
        assert db.session.scalar(sa.select(sa.func.count(BookingRequest.id))) == 0
