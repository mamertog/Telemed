"""Fixtures de teste. Usa um banco PostgreSQL dedicado (TEST_DATABASE_URL) — nunca dados reais."""
from __future__ import annotations

import os
from datetime import time, timedelta

import pyotp
import pytest
import sqlalchemy as sa

os.environ.setdefault("TEST_DATABASE_URL", "postgresql://medico:medico@localhost:5432/medico_test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-automated-tests-000000")

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (  # noqa: E402
    ROLE_DOCTOR, ROLE_SECRETARY, ROLE_TECH_ADMIN, AvailabilityRule, Service, User,
)
from app.security import encrypt_secret, hash_password, new_session_token  # noqa: E402
from app.services import settings as site_settings  # noqa: E402
from app.utils import today_local  # noqa: E402

PASSWORD = "Senha-de-teste-123!"


@pytest.fixture(scope="session")
def app():
    app = create_app("testing")
    with app.app_context():
        db.session.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()
        from flask_migrate import upgrade
        upgrade(directory=os.path.join(os.path.dirname(__file__), "..", "migrations"))
    yield app


@pytest.fixture(autouse=True)
def clean_db(app):
    yield
    with app.app_context():
        db.session.remove()
        tables = [t.name for t in reversed(db.metadata.sorted_tables)]
        db.session.execute(sa.text("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE"))
        db.session.commit()


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield


@pytest.fixture
def client(app):
    return app.test_client()


def make_user(role: str, email: str, name: str = "Usuária Teste") -> tuple[str, str]:
    secret = pyotp.random_base32()
    u = User(name=name, email=email, role=role, password_hash=hash_password(PASSWORD),
             session_token=new_session_token(), mfa_enabled=True, mfa_secret_enc=encrypt_secret(secret))
    db.session.add(u)
    db.session.commit()
    return str(u.id), secret


@pytest.fixture
def users(app):
    with app.app_context():
        return {
            "doctor": make_user(ROLE_DOCTOR, "medica@example.com", "Médica Teste"),
            "secretary": make_user(ROLE_SECRETARY, "secretaria@example.com", "Secretaria Teste"),
            "tech": make_user(ROLE_TECH_ADMIN, "tecnico@example.com", "Técnico Teste"),
        }


def login(client, app, email: str, secret: str):
    with app.app_context():
        u = db.session.execute(sa.select(User).where(User.email == email)).scalar_one()
        u.mfa_last_timecode = None
        db.session.commit()
    r = client.post("/acesso/entrar", data={"email": email, "password": PASSWORD})
    assert r.status_code == 302 and "/acesso/mfa" in r.headers["Location"], r.data[:500]
    r = client.post("/acesso/mfa", data={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 302 and "/painel" in r.headers["Location"], r.data[:500]
    return client


@pytest.fixture
def as_doctor(client, app, users):
    return login(client, app, "medica@example.com", users["doctor"][1])


@pytest.fixture
def as_secretary(client, app, users):
    return login(client, app, "secretaria@example.com", users["secretary"][1])


@pytest.fixture
def as_tech(client, app, users):
    return login(client, app, "tecnico@example.com", users["tech"][1])


@pytest.fixture
def agenda(app):
    """Serviço ativo de 30 min e atendimento todos os dias, 08h–18h com intervalo 12h–13h."""
    with app.app_context():
        svc = Service(name="Consulta de teste", slug="consulta-de-teste", description="Teste",
                      duration_minutes=30, active=True)
        db.session.add(svc)
        for wd in range(7):
            db.session.add(AvailabilityRule(weekday=wd, start_time=time(8), end_time=time(18),
                                            break_start=time(12), break_end=time(13), active=True))
        site_settings.set_many({"min_notice_hours": 0, "max_days_ahead": 60, "slot_step_minutes": 30,
                                "buffer_minutes": 0})
        db.session.commit()
        target = today_local() + timedelta(days=3)
        return {"service_id": svc.id, "date": target}


def slot_iso(d, hh: int, mm: int = 0) -> str:
    from datetime import datetime
    from app.utils import TZ
    return datetime.combine(d, time(hh, mm), tzinfo=TZ).isoformat()


def booking_payload(agenda, hh=9, mm=0, name="Paciente Ficticio Silva", phone="(19) 98888-0001", **extra):
    data = {
        "service_id": agenda["service_id"], "start": slot_iso(agenda["date"], hh, mm),
        "modality": "presencial", "full_name": name, "phone": phone, "email": "ficticio@example.com",
        "privacy_ack": "y",
    }
    data.update(extra)
    return data
