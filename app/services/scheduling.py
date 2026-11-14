"""Cálculo de disponibilidade e criação transacional de agendamentos.

Proteções contra conflito (em camadas):
1. Validação no servidor: o horário precisa constar da lista de horários livres recalculada.
2. pg_advisory_xact_lock: serializa reservas concorrentes dentro da transação.
3. Restrição de exclusão (EXCLUDE USING gist) no PostgreSQL: impossibilita dois
   agendamentos ativos com intervalos sobrepostos, mesmo que as camadas acima falhem.
"""
from __future__ import annotations

import calendar
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (
    ACTIVE_STATUSES, ALLOWED_TRANSITIONS, ST_CANCELLED, ST_REQUESTED, Appointment,
    AppointmentEvent, AvailabilityException, AvailabilityRule, Patient, PrivacyRecord, Service,
)
from ..utils import (
    TZ, format_phone, hashed_ip, name_key, new_reference, normalize_email, normalize_name,
    normalize_phone, now_utc, sha256_hex, to_local, today_local,
)
from . import settings as site_settings

BOOKING_LOCK_KEY = 72_388_700  # chave arbitrária do advisory lock da agenda


class SlotUnavailable(Exception):
    """O horário escolhido não está (mais) disponível."""


class InvalidTransition(Exception):
    pass


@dataclass
class Interval:
    start: datetime
    end: datetime

    def overlaps(self, s: datetime, e: datetime) -> bool:
        return s < self.end and e > self.start


# ---------------------------------------------------------------------------
# Disponibilidade
# ---------------------------------------------------------------------------
def _local_dt(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=TZ)


def day_windows(d: date, rules: list[AvailabilityRule]) -> list[Interval]:
    """Janelas de atendimento do dia, já descontados os intervalos (pausas)."""
    windows: list[Interval] = []
    for r in rules:
        if not r.active or r.weekday != d.weekday():
            continue
        if r.break_start and r.break_end:
            windows.append(Interval(_local_dt(d, r.start_time), _local_dt(d, r.break_start)))
            windows.append(Interval(_local_dt(d, r.break_end), _local_dt(d, r.end_time)))
        else:
            windows.append(Interval(_local_dt(d, r.start_time), _local_dt(d, r.end_time)))
    return sorted((w for w in windows if w.end > w.start), key=lambda w: w.start)


def busy_intervals(start: datetime, end: datetime, exclude_appointment_id=None) -> list[Interval]:
    """Agendamentos ativos e bloqueios que se sobrepõem a [start, end)."""
    buffer = timedelta(minutes=int(site_settings.get("buffer_minutes") or 0))
    q = sa.select(Appointment.start_at, Appointment.end_at).where(
        Appointment.status.in_(ACTIVE_STATUSES),
        Appointment.start_at < end + buffer,
        Appointment.end_at > start - buffer,
    )
    if exclude_appointment_id is not None:
        q = q.where(Appointment.id != exclude_appointment_id)
    busy = [Interval(s - buffer, e + buffer) for s, e in db.session.execute(q)]
    q2 = sa.select(AvailabilityException.start_at, AvailabilityException.end_at).where(
        AvailabilityException.start_at < end, AvailabilityException.end_at > start
    )
    busy += [Interval(s, e) for s, e in db.session.execute(q2)]
    return busy


def booking_window() -> tuple[datetime, date]:
    """(primeiro instante reservável, última data reservável) para o público."""
    s = site_settings.get_all()
    min_start = now_utc() + timedelta(hours=int(s.get("min_notice_hours") or 0))
    max_date = today_local() + timedelta(days=int(s.get("max_days_ahead") or 60))
    return min_start, max_date


def active_rules() -> list[AvailabilityRule]:
    return list(db.session.execute(
        sa.select(AvailabilityRule).where(AvailabilityRule.active.is_(True))
    ).scalars())


def slots_for_day(d: date, duration_minutes: int, *, public: bool = True, rules=None,
                  busy: list[Interval] | None = None, exclude_appointment_id=None) -> list[datetime]:
    """Horários de início disponíveis (timezone America/Sao_Paulo) para a duração informada."""
    rules = rules if rules is not None else active_rules()
    windows = day_windows(d, rules)
    if not windows:
        return []
    step = timedelta(minutes=int(site_settings.get("slot_step_minutes") or 30))
    duration = timedelta(minutes=duration_minutes)
    min_start, max_date = booking_window() if public else (now_utc(), None)
    if max_date and d > max_date:
        return []
    if busy is None:
        busy = busy_intervals(windows[0].start, windows[-1].end, exclude_appointment_id)
    result = []
    for w in windows:
        t = w.start
        while t + duration <= w.end:
            e = t + duration
            if t >= min_start and not any(b.overlaps(t, e) for b in busy):
                result.append(t)
            t += step
    return result


def available_days(service: Service, year: int, month: int) -> list[str]:
    """Datas (ISO) do mês com ao menos um horário livre para o serviço."""
    rules = active_rules()
    if not rules:
        return []
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    today = today_local()
    _, max_date = booking_window()
    start_d, end_d = max(first, today), min(last, max_date)
    if start_d > end_d:
        return []
    busy = busy_intervals(_local_dt(start_d, time.min), _local_dt(end_d + timedelta(days=1), time.min))
    days = []
    d = start_d
    while d <= end_d:
        if slots_for_day(d, service.duration_minutes, rules=rules, busy=busy):
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def count_free_slots(start_d: date, end_d: date, duration_minutes: int | None = None) -> int:
    rules = active_rules()
    if not rules:
        return 0
    duration = duration_minutes or int(site_settings.get("slot_step_minutes") or 30)
    busy = busy_intervals(_local_dt(start_d, time.min), _local_dt(end_d + timedelta(days=1), time.min))
    total, d = 0, start_d
    while d <= end_d:
        total += len(slots_for_day(d, duration, public=False, rules=rules, busy=busy))
        d += timedelta(days=1)
    return total


def is_within_rules(start: datetime, end: datetime) -> bool:
    d = to_local(start).date()
    return any(w.start <= start and end <= w.end for w in day_windows(d, active_rules()))


# ---------------------------------------------------------------------------
# Pacientes
# ---------------------------------------------------------------------------
def find_matching_patient(full_name: str, phone_norm: str) -> Patient | None:
    """Associação conservadora: mesmo telefone normalizado E mesmo nome normalizado.
    Não depende apenas do nome e nunca sobrescreve dados existentes a partir do formulário público."""
    return db.session.execute(
        sa.select(Patient).where(
            Patient.phone_norm == phone_norm,
            Patient.name_key == name_key(full_name),
            Patient.status != "anonimizado",
        ).order_by(Patient.created_at)
    ).scalars().first()


def possible_duplicates(phone_norm: str | None, email_norm: str | None, exclude_id=None) -> list[Patient]:
    conds = []
    if phone_norm:
        conds.append(Patient.phone_norm == phone_norm)
    if email_norm:
        conds.append(Patient.email_norm == email_norm)
    if not conds:
        return []
    q = sa.select(Patient).where(sa.or_(*conds), Patient.status != "anonimizado")
    if exclude_id:
        q = q.where(Patient.id != exclude_id)
    return list(db.session.execute(q.limit(10)).scalars())


def build_patient(full_name, phone, email=None, birth_date=None, whatsapp_opt_in=False) -> Patient:
    phone_norm = normalize_phone(phone)
    if not phone_norm:
        raise ValueError("Telefone inválido.")
    name = normalize_name(full_name)
    em = normalize_email(email)
    return Patient(
        full_name=name, name_key=name_key(name), phone=format_phone(phone_norm), phone_norm=phone_norm,
        email=em, email_norm=em, birth_date=birth_date, whatsapp_opt_in=bool(whatsapp_opt_in),
    )


# ---------------------------------------------------------------------------
# Agendamentos
# ---------------------------------------------------------------------------
def _lock_agenda() -> None:
    db.session.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": BOOKING_LOCK_KEY})


def _unique_reference() -> str:
    for _ in range(10):
        ref = new_reference()
        if not db.session.execute(sa.select(Appointment.id).where(Appointment.reference == ref)).first():
            return ref
    raise RuntimeError("Não foi possível gerar código de referência.")


def add_event(appt: Appointment, event_type: str, *, from_status=None, to_status=None,
              details: dict | None = None, user_id=None) -> None:
    db.session.add(AppointmentEvent(
        appointment=appt, event_type=event_type, from_status=from_status, to_status=to_status,
        details=details or {}, user_id=user_id,
    ))


def create_public_booking(*, service_id: int, start_iso: str, full_name: str, phone: str,
                          email: str | None, birth_date, whatsapp_opt_in: bool,
                          modality: str = "presencial") -> tuple[Appointment, str]:
    """Cria uma solicitação pública. Retorna (agendamento, token de gestão em claro)."""
    service = db.session.get(Service, service_id)
    if not service or not service.active:
        raise SlotUnavailable("Tipo de atendimento indisponível.")
    try:
        start = datetime.fromisoformat(start_iso)
    except (TypeError, ValueError):
        raise SlotUnavailable("Horário inválido.")
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    end = start + timedelta(minutes=service.duration_minutes)
    phone_norm = normalize_phone(phone)
    if not phone_norm:
        raise ValueError("Telefone inválido.")

    token = secrets.token_urlsafe(32)
    try:
        _lock_agenda()
        slots = slots_for_day(to_local(start).date(), service.duration_minutes)
        if start not in slots:
            raise SlotUnavailable("Este horário acabou de ficar indisponível. Escolha outro, por favor.")

        patient = find_matching_patient(full_name, phone_norm)
        if patient is None:
            patient = build_patient(full_name, phone, email, birth_date, whatsapp_opt_in)
            db.session.add(patient)
        else:
            # Apenas completa dados ausentes; não sobrescreve cadastro existente.
            em = normalize_email(email)
            if em and not patient.email:
                patient.email, patient.email_norm = em, em
            if birth_date and not patient.birth_date:
                patient.birth_date = birth_date
            if whatsapp_opt_in and not patient.whatsapp_opt_in:
                patient.whatsapp_opt_in = True

        appt = Appointment(
            reference=_unique_reference(), patient=patient, service=service,
            start_at=start, end_at=end, status=ST_REQUESTED, origin="online",
            modality=modality, public_token_hash=sha256_hex(token),
        )
        db.session.add(appt)
        add_event(appt, "criado", to_status=ST_REQUESTED, details={"origem": "online"})

        db.session.flush()  # obtém patient.id e valida restrições antes do commit
        version = site_settings.get("privacy_version")
        evidence = {"ip_hash": hashed_ip(), "canal": "formulario_agendamento"}
        db.session.add(PrivacyRecord(patient_id=patient.id, record_type="aviso_privacidade_ciencia",
                                     document_version=version, granted=True, evidence=evidence))
        if whatsapp_opt_in:
            db.session.add(PrivacyRecord(patient_id=patient.id, record_type="consentimento_contato_whatsapp",
                                         document_version=version, granted=True, evidence=evidence))
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        if "appointments_no_overlap" in str(exc.orig):
            raise SlotUnavailable("Este horário acabou de ser reservado. Escolha outro, por favor.") from exc
        raise
    except Exception:
        db.session.rollback()
        raise
    return appt, token


def create_admin_appointment(*, patient: Patient, service: Service, start: datetime, status: str,
                             modality: str, user_id) -> tuple[Appointment, str]:
    end = start + timedelta(minutes=service.duration_minutes)
    token = secrets.token_urlsafe(32)
    try:
        _lock_agenda()
        if any(b.overlaps(start, end) for b in busy_intervals(start, end)):
            raise SlotUnavailable("O horário conflita com outro agendamento ou bloqueio.")
        appt = Appointment(
            reference=_unique_reference(), patient=patient, service=service, start_at=start, end_at=end,
            status=status, origin="painel", modality=modality, created_by_id=user_id,
            public_token_hash=sha256_hex(token),
        )
        db.session.add(appt)
        add_event(appt, "criado", to_status=status, details={"origem": "painel"}, user_id=user_id)
        db.session.commit()
    except SlotUnavailable:
        db.session.rollback()
        raise
    except IntegrityError as exc:
        db.session.rollback()
        if "appointments_no_overlap" in str(exc.orig):
            raise SlotUnavailable("O horário conflita com outro agendamento.") from exc
        raise
    return appt, token


def change_status(appt: Appointment, new_status: str, *, user_id=None, reason: str | None = None,
                  source: str = "painel") -> None:
    if new_status not in ALLOWED_TRANSITIONS.get(appt.status, set()):
        raise InvalidTransition("Alteração de status não permitida.")
    old = appt.status
    appt.status = new_status
    if new_status == ST_CANCELLED:
        appt.cancel_reason = reason or "outro"
    add_event(appt, "status", from_status=old, to_status=new_status,
              details={"origem": source, **({"motivo": appt.cancel_reason} if new_status == ST_CANCELLED else {})},
              user_id=user_id)
    db.session.commit()


def reschedule(appt: Appointment, new_start: datetime, *, user_id=None) -> None:
    if not appt.is_active:
        raise InvalidTransition("Somente agendamentos ativos podem ser reagendados.")
    duration = appt.end_at - appt.start_at
    new_end = new_start + duration
    old_start = appt.start_at
    try:
        _lock_agenda()
        if any(b.overlaps(new_start, new_end) for b in busy_intervals(new_start, new_end, appt.id)):
            raise SlotUnavailable("O novo horário conflita com outro agendamento ou bloqueio.")
        appt.start_at, appt.end_at = new_start, new_end
        appt.reminder_sent_at = None
        add_event(appt, "reagendado", from_status=appt.status, to_status=appt.status,
                  details={"de": old_start.isoformat(), "para": new_start.isoformat()}, user_id=user_id)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise SlotUnavailable("O novo horário conflita com outro agendamento.") from exc
    except SlotUnavailable:
        db.session.rollback()
        raise
