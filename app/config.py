"""Configurações da aplicação, lidas de variáveis de ambiente."""
from __future__ import annotations

import os
from datetime import timedelta


def _normalize_db_url(url: str | None) -> str | None:
    """Converte URLs do Render (postgres://) para o driver psycopg 3."""
    if not url:
        return None
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "sim", "on"}


class Config:
    APP_ENV = os.environ.get("APP_ENV", "development")
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    SQLALCHEMY_DATABASE_URI = _normalize_db_url(os.environ.get("DATABASE_URL"))
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 280}
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    TIMEZONE = "America/Sao_Paulo"

    # Sessões e cookies
    SESSION_COOKIE_NAME = "jgr_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
    REMEMBER_COOKIE_DURATION = timedelta(seconds=0)
    PERMANENT_SESSION_LIFETIME = timedelta(hours=int(os.environ.get("SESSION_MAX_HOURS", "8")))
    SESSION_IDLE_MINUTES = int(os.environ.get("SESSION_IDLE_MINUTES", "30"))

    WTF_CSRF_TIME_LIMIT = 3600 * 4
    MAX_CONTENT_LENGTH = 4 * 1024 * 1024  # 4 MB (upload de foto)

    # Rate limiting (memória por processo; ver README)
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = False
    RATELIMIT_ENABLED = _bool("RATELIMIT_ENABLED", True)

    # E-mail transacional
    EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "none").lower()  # none|console|resend|brevo
    BOOKING_ALERT_EMAIL = os.environ.get("BOOKING_ALERT_EMAIL", "").strip()
    EMAIL_API_KEY = os.environ.get("EMAIL_API_KEY", "")
    EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
    EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "Consultório Dra. Juliana Gonçalves Rosa")

    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    FORCE_HTTPS = _bool("FORCE_HTTPS", False)
    MFA_ISSUER = os.environ.get("MFA_ISSUER", "Consultorio Dra. Juliana")


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = _normalize_db_url(
        os.environ.get("TEST_DATABASE_URL", "postgresql://medico:medico@localhost:5432/medico_test")
    )
    SECRET_KEY = "test-secret-key-only-for-automated-tests-000000"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    EMAIL_PROVIDER = "none"
    SERVER_NAME = "localhost"


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    FORCE_HTTPS = _bool("FORCE_HTTPS", True)
    PREFERRED_URL_SCHEME = "https"


CONFIGS = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def validate_config(app) -> None:
    """Impede a inicialização com configuração insegura em produção."""
    uri = app.config.get("SQLALCHEMY_DATABASE_URI") or ""
    if not uri.startswith("postgresql+psycopg://"):
        raise RuntimeError("DATABASE_URL ausente ou não é PostgreSQL. SQLite não é suportado.")
    if app.config.get("APP_ENV") == "production" or app.config.get("ENV_NAME") == "production":
        key = app.config.get("SECRET_KEY") or ""
        if len(key) < 32:
            raise RuntimeError("SECRET_KEY deve ter pelo menos 32 caracteres em produção.")
        if not app.config.get("SESSION_COOKIE_SECURE"):
            raise RuntimeError("SESSION_COOKIE_SECURE deve estar ativo em produção.")
    elif not app.config.get("SECRET_KEY"):
        raise RuntimeError("Defina SECRET_KEY no arquivo .env (veja .env.example).")
