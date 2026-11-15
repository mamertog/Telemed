"""Notificações transacionais por e-mail (API Resend ou Brevo).

O agendamento funciona normalmente sem provedor configurado (EMAIL_PROVIDER=none).
As mensagens nunca contêm informações clínicas.
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

import requests
import sqlalchemy as sa
from flask import current_app, url_for

from ..extensions import db
from ..models import ST_CONFIRMED, Appointment
from ..utils import fmt_date, fmt_time, now_utc, sha256_hex
from . import settings as site_settings
from .scheduling import add_event

log = logging.getLogger(__name__)

KINDS = {
    "requested": "Solicitação recebida",
    "confirmed": "Confirmação definitiva",
    "cancelled": "Cancelamento",
    "rescheduled": "Reagendamento",
    "reminder": "Lembrete",
}


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def provider_configured() -> bool:
    cfg = current_app.config
    if cfg["EMAIL_PROVIDER"] == "console":
        return True
    return cfg["EMAIL_PROVIDER"] in {"resend", "brevo"} and bool(cfg["EMAIL_API_KEY"]) and bool(cfg["EMAIL_FROM"])


def send_email(to: str, subject: str, body: str) -> bool:
    cfg = current_app.config
    provider = cfg["EMAIL_PROVIDER"]
    try:
        if provider == "console":
            log.info("E-mail (console) para %s: %s", to.split("@")[-1], subject)
            return True
        if provider == "resend":
            r = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {cfg['EMAIL_API_KEY']}"},
                json={"from": f"{cfg['EMAIL_FROM_NAME']} <{cfg['EMAIL_FROM']}>", "to": [to],
                      "subject": subject, "text": body},
                timeout=10,
            )
            return r.status_code < 300
        if provider == "brevo":
            r = requests.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={"api-key": cfg["EMAIL_API_KEY"], "accept": "application/json"},
                json={"sender": {"name": cfg["EMAIL_FROM_NAME"], "email": cfg["EMAIL_FROM"]},
                      "to": [{"email": to}], "subject": subject, "textContent": body},
                timeout=10,
            )
            return r.status_code < 300
    except requests.RequestException:
        log.exception("Falha ao enviar e-mail")
    return False


def manage_url(token: str | None) -> str:
    if not token:
        return url_for("public.index", _external=True)
    base = current_app.config.get("PUBLIC_BASE_URL")
    path = url_for("booking.manage", token=token)
    return f"{base}{path}" if base else url_for("booking.manage", token=token, _external=True)


def render(kind: str, appt: Appointment, token: str | None = None) -> tuple[str, str]:
    s = site_settings.get_all()
    ctx = _SafeDict(
        nome=appt.patient.first_name, data=fmt_date(appt.start_at), hora=fmt_time(appt.start_at),
        servico=appt.service.name, codigo=appt.reference, medica=s["doctor_name"],
        link=manage_url(token),
    )
    subject = str(s.get(f"tpl_{kind}_subject", "")).format_map(ctx)
    body = str(s.get(f"tpl_{kind}_body", "")).format_map(ctx)
    return subject, body


def notify(appt: Appointment, kind: str, token: str | None = None, user_id=None) -> bool:
    """Envia a notificação se houver e-mail e provedor configurado. Nunca interrompe o fluxo."""
    if not appt.patient or not appt.patient.email or not provider_configured():
        return False
    try:
        if token is None:
            # O token em claro não é armazenado; gera-se um novo link a cada envio (o anterior expira).
            token = secrets.token_urlsafe(32)
            appt.public_token_hash = sha256_hex(token)
        subject, body = render(kind, appt, token)
        ok = send_email(appt.patient.email, subject, body)
        add_event(appt, "notificacao", details={"tipo": kind, "canal": "email", "enviado": ok}, user_id=user_id)
        db.session.commit()
        return ok
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("Erro ao processar notificação")
        return False


def send_due_reminders() -> int:
    """Envia lembretes de consultas confirmadas dentro da janela configurada."""
    hours = int(site_settings.get("reminder_hours") or 24)
    now = now_utc()
    appts = db.session.execute(
        sa.select(Appointment).where(
            Appointment.status == ST_CONFIRMED,
            Appointment.reminder_sent_at.is_(None),
            Appointment.start_at > now,
            Appointment.start_at <= now + timedelta(hours=hours),
        )
    ).scalars().all()
    sent = 0
    for appt in appts:
        if notify(appt, "reminder"):
            sent += 1
        appt.reminder_sent_at = now
        db.session.commit()
    return sent


def notify_doctor(appt: Appointment) -> bool:
    """Notify the configured doctor independently of the patient's email."""
    recipient = current_app.config.get("BOOKING_ALERT_EMAIL", "")
    if not recipient or not provider_configured():
        return False
    try:
        path = url_for("admin.appointment_detail", appt_id=appt.id)
        base = current_app.config.get("PUBLIC_BASE_URL", "").rstrip("/")
        link = base + path if base else url_for("admin.appointment_detail", appt_id=appt.id, _external=True)
        subject = f"Novo agendamento recebido - {appt.reference}"
        body = (
            f"Uma nova consulta foi solicitada pelo site.\n\n"
            f"Referencia: {appt.reference}\n"
            f"Atendimento: {appt.service.name}\n"
            f"Data: {fmt_date(appt.start_at)}\n"
            f"Horario: {fmt_time(appt.start_at)} (Brasilia)\n\n"
            f"Entre no painel para revisar e confirmar: {link}\n"
        )
        ok = send_email(recipient, subject, body)
        add_event(appt, "notificacao", details={"tipo": "novo_agendamento_medica", "canal": "email", "enviado": ok})
        db.session.commit()
        return ok
    except Exception:
        db.session.rollback()
        log.exception("Falha ao avisar a medica sobre novo agendamento")
        return False
