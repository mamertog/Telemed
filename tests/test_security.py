"""Autenticação, MFA, autorização, CSRF, migrações e configuração de produção."""
import os
from datetime import datetime, timezone

import pyotp
import pytest
import sqlalchemy as sa

from app import create_app
from app.config import ProductionConfig, validate_config
from app.extensions import db
from app.models import User

from .conftest import PASSWORD, booking_payload, login


def test_login_logout_flow(client, app, users):
    r = client.post("/acesso/entrar", data={"email": "medica@example.com", "password": "errada-123456"})
    assert r.status_code == 401
    msg_bad_pwd = r.get_data(as_text=True)
    r = client.post("/acesso/entrar", data={"email": "naoexiste@example.com", "password": "errada-123456"})
    assert r.status_code == 401
    assert ("E-mail ou senha inválidos" in msg_bad_pwd) and ("E-mail ou senha inválidos" in r.get_data(as_text=True))
    # senha correta ainda exige MFA
    r = client.post("/acesso/entrar", data={"email": "medica@example.com", "password": PASSWORD})
    assert "/acesso/mfa" in r.headers["Location"]
    assert client.get("/painel/").status_code == 302
    assert client.post("/acesso/mfa", data={"code": "000000"}).status_code == 200
    login(client, app, "medica@example.com", users["doctor"][1])
    assert client.get("/painel/").status_code == 200
    client.post("/acesso/sair")
    r = client.get("/painel/")
    assert r.status_code == 302 and "/acesso/entrar" in r.headers["Location"]


def test_totp_code_cannot_be_reused(client, app, users):
    secret = users["doctor"][1]
    login(client, app, "medica@example.com", secret)
    client.post("/acesso/sair")
    code = pyotp.TOTP(secret).now()
    client.post("/acesso/entrar", data={"email": "medica@example.com", "password": PASSWORD})
    r = client.post("/acesso/mfa", data={"code": code})
    # O código usado no login anterior (mesma janela) é recusado
    assert r.status_code == 200 and "Código inválido" in r.get_data(as_text=True)


def test_mfa_setup_required_for_new_account(client, app):
    from app.security import hash_password, new_session_token
    with app.app_context():
        db.session.add(User(name="Nova Conta", email="nova@example.com", role="secretaria",
                            password_hash=hash_password(PASSWORD), session_token=new_session_token()))
        db.session.commit()
    r = client.post("/acesso/entrar", data={"email": "nova@example.com", "password": PASSWORD})
    assert "/acesso/mfa/configurar" in r.headers["Location"]
    page = client.get("/acesso/mfa/configurar").get_data(as_text=True)
    assert "<svg" in page
    with app.app_context():
        from app.security import decrypt_secret
        u = db.session.execute(sa.select(User).where(User.email == "nova@example.com")).scalar_one()
        secret = decrypt_secret(u.mfa_pending_secret_enc)
    r = client.post("/acesso/mfa/configurar", data={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 302 and "/painel" in r.headers["Location"]
    with app.app_context():
        u = db.session.execute(sa.select(User).where(User.email == "nova@example.com")).scalar_one()
        assert u.mfa_enabled and u.mfa_pending_secret_enc is None
        assert u.password_hash.startswith("$argon2id$")


def test_account_lockout(client, app, users):
    for _ in range(5):
        client.post("/acesso/entrar", data={"email": "medica@example.com", "password": "errada-0000000"})
    r = client.post("/acesso/entrar", data={"email": "medica@example.com", "password": PASSWORD})
    assert r.status_code == 401
    with app.app_context():
        u = db.session.execute(sa.select(User).where(User.email == "medica@example.com")).scalar_one()
        assert u.locked_until is not None and u.locked_until > datetime.now(timezone.utc)


def test_all_admin_routes_require_authentication(app, client):
    count = 0
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/painel"):
            continue
        url = rule.rule
        for arg in rule.arguments:
            url = url.replace(f"<uuid:{arg}>", "00000000-0000-0000-0000-000000000000").replace(f"<int:{arg}>", "1")
        for method in rule.methods & {"GET", "POST"}:
            r = client.open(url, method=method)
            assert r.status_code in (302, 401), f"{method} {url} -> {r.status_code}"
            if r.status_code == 302:
                assert "/acesso/entrar" in r.headers["Location"]
            count += 1
    assert count > 30


SECRETARY_FORBIDDEN = ["/painel/configuracoes", "/painel/configuracoes/profissional", "/painel/servicos",
                       "/painel/usuarios", "/painel/pacientes/exportar", "/painel/auditoria", "/painel/sistema"]
TECH_FORBIDDEN = ["/painel/pacientes", "/painel/agenda", "/painel/agendamentos", "/painel/agendamentos/novo",
                  "/painel/disponibilidade", "/painel/bloqueios", "/painel/configuracoes", "/painel/servicos",
                  "/painel/pacientes/exportar", "/painel/api/horarios"]


def test_secretary_permission_isolation(as_secretary):
    for url in SECRETARY_FORBIDDEN:
        assert as_secretary.get(url).status_code == 403, url
    for url in ["/painel/", "/painel/agenda", "/painel/agendamentos", "/painel/pacientes", "/painel/pacientes/novo"]:
        assert as_secretary.get(url).status_code == 200, url
    assert as_secretary.post("/painel/disponibilidade", data={"weekday": 0, "start_time": "08:00",
                                                              "end_time": "12:00"}).status_code == 403


def test_tech_admin_has_no_patient_access(as_tech, app):
    for url in TECH_FORBIDDEN:
        assert as_tech.get(url).status_code == 403, url
    for url in ["/painel/", "/painel/usuarios", "/painel/auditoria", "/painel/sistema"]:
        assert as_tech.get(url).status_code == 200, url
    # não pode criar contas com acesso a pacientes
    r = as_tech.post("/painel/usuarios/novo", data={"name": "X Y", "email": "x@example.com",
                                                    "role": "secretaria", "password": "Senha-longa-1234"})
    assert r.status_code == 200
    with app.app_context():
        assert db.session.execute(sa.select(User).where(User.email == "x@example.com")).first() is None
    html = as_tech.get("/painel/").get_data(as_text=True)
    assert "não possui acesso a agenda" in html


def test_doctor_access(as_doctor):
    for url in ["/painel/", "/painel/configuracoes", "/painel/usuarios", "/painel/pacientes", "/painel/servicos"]:
        assert as_doctor.get(url).status_code == 200, url
    assert as_doctor.get("/painel/sistema").status_code == 403  # configurações técnicas


def test_session_invalidated_when_account_disabled(client, app, users, as_secretary):
    with app.app_context():
        u = db.session.execute(sa.select(User).where(User.email == "secretaria@example.com")).scalar_one()
        u.is_active_account = False
        db.session.commit()
    assert client.get("/painel/").status_code == 302


def test_csrf_protection(agenda):
    os.environ["TEST_DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    app = create_app("testing")
    app.config["WTF_CSRF_ENABLED"] = True
    c = app.test_client()
    r = c.post("/agendar/confirmar", data=booking_payload(agenda))
    assert r.status_code == 400
    r = c.post("/acesso/entrar", data={"email": "a@b.c", "password": "x"})
    assert r.status_code == 400
    # com token válido o formulário é aceito
    page = c.get("/acesso/entrar").get_data(as_text=True)
    import re
    token = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', page).group(1)
    r = c.post("/acesso/entrar", data={"email": "a@example.com", "password": "x", "csrf_token": token})
    assert r.status_code == 401


def test_migrations_are_current(app):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from flask_migrate import Migrate
    with app.app_context():
        conn = db.engine.connect()
        mc = MigrationContext.configure(conn)
        cfg = app.extensions["migrate"].migrate.get_config()
        assert mc.get_current_revision() == ScriptDirectory.from_config(cfg).get_current_head()
        diff = [d for d in compare_metadata(mc, db.metadata)]
        assert diff == [], diff
        conn.close()


def test_production_config_validation():
    class FakeApp:
        def __init__(self, cfg):
            self.config = cfg
    good = {"SQLALCHEMY_DATABASE_URI": "postgresql+psycopg://u:p@h/db", "APP_ENV": "production",
            "SECRET_KEY": "x" * 40, "SESSION_COOKIE_SECURE": True}
    validate_config(FakeApp(good))
    with pytest.raises(RuntimeError):
        validate_config(FakeApp(dict(good, SECRET_KEY="curta")))
    with pytest.raises(RuntimeError):
        validate_config(FakeApp(dict(good, SQLALCHEMY_DATABASE_URI="sqlite:///x.db")))
    assert ProductionConfig.SESSION_COOKIE_SECURE is True
    assert ProductionConfig.SESSION_COOKIE_HTTPONLY is True
    assert ProductionConfig.SESSION_COOKIE_SAMESITE == "Lax"
    assert ProductionConfig.DEBUG is False


def test_render_database_url_normalization():
    from app.config import _normalize_db_url
    assert _normalize_db_url("postgres://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d"
    assert _normalize_db_url("postgresql://u:p@h/d") == "postgresql+psycopg://u:p@h/d"


def test_https_redirect_and_hsts(app):
    app.config["FORCE_HTTPS"] = True
    try:
        c = app.test_client()
        r = c.get("/sobre")
        assert r.status_code == 301 and r.headers["Location"].startswith("https://")
        r = c.get("/sobre", headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200 and "max-age" in r.headers["Strict-Transport-Security"]
        assert c.get("/health").status_code == 200
    finally:
        app.config["FORCE_HTTPS"] = False


def test_admin_pages_not_cached_or_indexed(as_doctor):
    r = as_doctor.get("/painel/")
    assert r.headers["Cache-Control"] == "no-store"
    assert "noindex" in r.headers["X-Robots-Tag"]


def test_password_reset_token_single_use(client, app, users):
    from app.blueprints.auth import make_reset_token
    with app.app_context():
        u = db.session.execute(sa.select(User).where(User.email == "medica@example.com")).scalar_one()
        token = make_reset_token(u)
    new = "Nova-senha-segura-2026"
    r = client.post(f"/acesso/redefinir/{token}", data={"password": new, "confirm": new})
    assert r.status_code == 302 and "/acesso/entrar" in r.headers["Location"]
    r = client.post(f"/acesso/redefinir/{token}", data={"password": "Outra-senha-2026x", "confirm": "Outra-senha-2026x"})
    assert "/acesso/esqueci" in r.headers["Location"]
    r = client.post("/acesso/esqueci", data={"email": "naoexiste@example.com"})
    assert r.status_code == 302  # resposta idêntica (sem enumeração)
