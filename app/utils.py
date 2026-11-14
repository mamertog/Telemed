"""Funções utilitárias: fuso horário, normalização, formatação."""
from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from flask import current_app, request

TZ = ZoneInfo("America/Sao_Paulo")

WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
WEEKDAYS_SHORT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
          "agosto", "setembro", "outubro", "novembro", "dezembro"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    return datetime.now(TZ)


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def today_local() -> date:
    return now_local().date()


def fmt_date(dt) -> str:
    if dt is None:
        return ""
    if isinstance(dt, datetime):
        dt = to_local(dt)
    return dt.strftime("%d/%m/%Y")


def fmt_time(dt) -> str:
    return to_local(dt).strftime("%H:%M") if dt else ""


def fmt_datetime(dt) -> str:
    return f"{fmt_date(dt)} às {fmt_time(dt)}" if dt else ""


def fmt_long_date(d) -> str:
    if isinstance(d, datetime):
        d = to_local(d).date()
    return f"{WEEKDAYS[d.weekday()]}, {d.day} de {MONTHS[d.month - 1]} de {d.year}"


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_name(name: str) -> str:
    """Remove espaços duplicados e capitaliza de forma neutra."""
    return re.sub(r"\s+", " ", (name or "").strip())


def name_key(name: str) -> str:
    """Chave de comparação: sem acentos, minúscula, espaços simples."""
    return re.sub(r"\s+", " ", strip_accents(name or "").lower().strip())


def normalize_phone(phone: str) -> str | None:
    """Retorna os dígitos de um telefone brasileiro (DDD + número) ou None se inválido."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("55") and len(digits) in (12, 13):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) in (11, 12):
        digits = digits[1:]
    if len(digits) not in (10, 11):
        return None
    if digits[0] == "0" or digits[1] == "0":
        return None
    return digits


def format_phone(digits: str | None) -> str:
    if not digits:
        return ""
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return digits


def normalize_email(email: str | None) -> str | None:
    email = (email or "").strip().lower()
    return email or None


REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_reference() -> str:
    raw = "".join(secrets.choice(REF_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hashed_ip() -> str | None:
    """Hash do IP com a SECRET_KEY: permite correlação de abuso sem armazenar o IP em claro."""
    try:
        ip = request.remote_addr or ""
    except RuntimeError:
        return None
    if not ip:
        return None
    return hashlib.sha256((current_app.config["SECRET_KEY"] + "|" + ip).encode()).hexdigest()


def slugify(text: str) -> str:
    s = strip_accents(text or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or secrets.token_hex(4)


def whatsapp_link(digits: str | None, text: str = "") -> str | None:
    if not digits:
        return None
    from urllib.parse import quote
    url = f"https://wa.me/55{digits}"
    if text:
        url += "?text=" + quote(text)
    return url
