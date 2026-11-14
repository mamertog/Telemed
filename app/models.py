"""Modelos de dados (PostgreSQL)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from flask_login import UserMixin
from sqlalchemy.dialects.postgresql import JSONB, UUID, ExcludeConstraint

from .extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Papéis e permissões
# ---------------------------------------------------------------------------
ROLE_DOCTOR = "medica"
ROLE_SECRETARY = "secretaria"
ROLE_TECH_ADMIN = "admin_tecnico"

ROLE_LABELS = {
    ROLE_DOCTOR: "Médica",
    ROLE_SECRETARY: "Secretaria",
    ROLE_TECH_ADMIN: "Administrador técnico",
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    ROLE_DOCTOR: {
        "agenda.view", "agenda.manage", "agenda.block", "agenda.configure",
        "patients.view", "patients.edit", "patients.export",
        "services.manage", "settings.manage", "settings.professional",
        "reports.view", "audit.view", "users.manage",
    },
    ROLE_SECRETARY: {
        "agenda.view", "agenda.manage", "agenda.block",
        "patients.view", "patients.edit",
    },
    # O administrador técnico NÃO recebe acesso a dados de pacientes.
    ROLE_TECH_ADMIN: {"users.manage", "settings.technical", "audit.view"},
}

# ---------------------------------------------------------------------------
# Status de agendamento
# ---------------------------------------------------------------------------
ST_REQUESTED = "solicitado"
ST_CONFIRMED = "confirmado"
ST_CANCELLED = "cancelado"
ST_COMPLETED = "concluido"
ST_NO_SHOW = "nao_compareceu"

STATUS_LABELS = {
    ST_REQUESTED: "Solicitado",
    ST_CONFIRMED: "Confirmado",
    ST_CANCELLED: "Cancelado",
    ST_COMPLETED: "Concluído",
    ST_NO_SHOW: "Não compareceu",
}
ACTIVE_STATUSES = (ST_REQUESTED, ST_CONFIRMED)

ALLOWED_TRANSITIONS = {
    ST_REQUESTED: {ST_CONFIRMED, ST_CANCELLED},
    ST_CONFIRMED: {ST_CANCELLED, ST_COMPLETED, ST_NO_SHOW},
    ST_CANCELLED: set(),
    ST_COMPLETED: set(),
    ST_NO_SHOW: set(),
}

CANCEL_REASONS = {
    "paciente_solicitou": "Solicitação do paciente",
    "indisponibilidade_agenda": "Indisponibilidade da agenda",
    "duplicidade": "Solicitação duplicada",
    "sem_contato": "Não foi possível confirmar o contato",
    "outro": "Outro motivo administrativo",
}


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = sa.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = sa.Column(sa.String(120), nullable=False)
    email = sa.Column(sa.String(255), nullable=False, unique=True)
    password_hash = sa.Column(sa.String(255), nullable=False)
    role = sa.Column(sa.String(20), nullable=False)
    is_active_account = sa.Column(sa.Boolean, nullable=False, default=True)
    mfa_secret_enc = sa.Column(sa.Text)
    mfa_pending_secret_enc = sa.Column(sa.Text)
    mfa_enabled = sa.Column(sa.Boolean, nullable=False, default=False)
    mfa_last_timecode = sa.Column(sa.BigInteger)
    session_token = sa.Column(sa.String(64), nullable=False)
    failed_logins = sa.Column(sa.Integer, nullable=False, default=0)
    locked_until = sa.Column(sa.DateTime(timezone=True))
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    last_login_at = sa.Column(sa.DateTime(timezone=True))
    password_changed_at = sa.Column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint(
            f"role IN ('{ROLE_DOCTOR}','{ROLE_SECRETARY}','{ROLE_TECH_ADMIN}')", name="ck_users_role"
        ),
    )

    def get_id(self) -> str:  # invalida sessões quando session_token muda
        return f"{self.id}:{self.session_token}"

    @property
    def is_active(self) -> bool:
        return bool(self.is_active_account)

    @property
    def permissions(self) -> set[str]:
        return ROLE_PERMISSIONS.get(self.role, set())

    def can(self, perm: str) -> bool:
        return perm in self.permissions

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)


class Patient(db.Model):
    __tablename__ = "patients"

    id = sa.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name = sa.Column(sa.String(160), nullable=False)
    name_key = sa.Column(sa.String(160), nullable=False, index=True)
    phone = sa.Column(sa.String(30), nullable=False)
    phone_norm = sa.Column(sa.String(20), nullable=False, index=True)
    email = sa.Column(sa.String(255))
    email_norm = sa.Column(sa.String(255), index=True)
    birth_date = sa.Column(sa.Date)
    whatsapp_opt_in = sa.Column(sa.Boolean, nullable=False, default=False)
    status = sa.Column(sa.String(20), nullable=False, default="ativo")
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    appointments = db.relationship("Appointment", back_populates="patient", lazy="dynamic")

    __table_args__ = (
        sa.CheckConstraint("status IN ('ativo','inativo','anonimizado')", name="ck_patients_status"),
        sa.Index("ix_patients_phone_name", "phone_norm", "name_key"),
    )

    @property
    def first_name(self) -> str:
        return (self.full_name or "").split(" ")[0]


class Service(db.Model):
    __tablename__ = "services"

    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(120), nullable=False, unique=True)
    slug = sa.Column(sa.String(140), nullable=False, unique=True)
    description = sa.Column(sa.Text, nullable=False, default="")
    duration_minutes = sa.Column(sa.Integer, nullable=False, default=30)
    price = sa.Column(sa.Numeric(10, 2))
    show_price = sa.Column(sa.Boolean, nullable=False, default=False)
    active = sa.Column(sa.Boolean, nullable=False, default=False)
    position = sa.Column(sa.Integer, nullable=False, default=0)
    icon = sa.Column(sa.String(40), nullable=False, default="heart-pulse")
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        sa.CheckConstraint("duration_minutes BETWEEN 10 AND 480", name="ck_services_duration"),
        sa.CheckConstraint("price IS NULL OR price >= 0", name="ck_services_price"),
    )


class Appointment(db.Model):
    __tablename__ = "appointments"

    id = sa.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference = sa.Column(sa.String(12), nullable=False, unique=True)
    patient_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False, index=True)
    service_id = sa.Column(sa.Integer, sa.ForeignKey("services.id", ondelete="RESTRICT"), nullable=False)
    start_at = sa.Column(sa.DateTime(timezone=True), nullable=False, index=True)
    end_at = sa.Column(sa.DateTime(timezone=True), nullable=False)
    status = sa.Column(sa.String(20), nullable=False, default=ST_REQUESTED, index=True)
    modality = sa.Column(sa.String(20), nullable=False, default="presencial")
    origin = sa.Column(sa.String(20), nullable=False, default="online")
    cancel_reason = sa.Column(sa.String(40))
    public_token_hash = sa.Column(sa.String(64), unique=True)
    reminder_sent_at = sa.Column(sa.DateTime(timezone=True))
    created_by_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    patient = db.relationship("Patient", back_populates="appointments")
    service = db.relationship("Service")
    events = db.relationship(
        "AppointmentEvent", back_populates="appointment", order_by="AppointmentEvent.created_at",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        sa.CheckConstraint("end_at > start_at", name="ck_appointments_range"),
        sa.CheckConstraint(
            "status IN ('solicitado','confirmado','cancelado','concluido','nao_compareceu')",
            name="ck_appointments_status",
        ),
        sa.CheckConstraint("modality IN ('presencial','teleconsulta')", name="ck_appointments_modality"),
        sa.CheckConstraint("origin IN ('online','painel')", name="ck_appointments_origin"),
        # Garantia definitiva contra conflitos: nenhum par de agendamentos ativos pode se sobrepor.
        ExcludeConstraint(
            (sa.literal_column("tstzrange(start_at, end_at, '[)')"), "&&"),
            using="gist",
            where=sa.text("status IN ('solicitado','confirmado')"),
            name="appointments_no_overlap",
        ),
    )

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


class AvailabilityRule(db.Model):
    __tablename__ = "availability_rules"

    id = sa.Column(sa.Integer, primary_key=True)
    weekday = sa.Column(sa.SmallInteger, nullable=False)  # 0 = segunda ... 6 = domingo
    start_time = sa.Column(sa.Time, nullable=False)
    end_time = sa.Column(sa.Time, nullable=False)
    break_start = sa.Column(sa.Time)
    break_end = sa.Column(sa.Time)
    active = sa.Column(sa.Boolean, nullable=False, default=True)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_rules_weekday"),
        sa.CheckConstraint("end_time > start_time", name="ck_rules_range"),
        sa.CheckConstraint(
            "(break_start IS NULL AND break_end IS NULL) OR "
            "(break_start IS NOT NULL AND break_end IS NOT NULL AND break_end > break_start "
            "AND break_start >= start_time AND break_end <= end_time)",
            name="ck_rules_break",
        ),
    )


class AvailabilityException(db.Model):
    __tablename__ = "availability_exceptions"

    id = sa.Column(sa.Integer, primary_key=True)
    start_at = sa.Column(sa.DateTime(timezone=True), nullable=False, index=True)
    end_at = sa.Column(sa.DateTime(timezone=True), nullable=False)
    reason = sa.Column(sa.String(160), nullable=False)
    created_by_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (sa.CheckConstraint("end_at > start_at", name="ck_exceptions_range"),)


class AppointmentEvent(db.Model):
    __tablename__ = "appointment_events"

    id = sa.Column(sa.BigInteger, primary_key=True)
    appointment_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = sa.Column(sa.String(30), nullable=False)
    from_status = sa.Column(sa.String(20))
    to_status = sa.Column(sa.String(20))
    details = sa.Column(JSONB, nullable=False, default=dict)
    user_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)

    appointment = db.relationship("Appointment", back_populates="events")
    user = db.relationship("User")


class PrivacyRecord(db.Model):
    __tablename__ = "privacy_records"

    id = sa.Column(sa.BigInteger, primary_key=True)
    patient_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey("patients.id", ondelete="SET NULL"), index=True)
    record_type = sa.Column(sa.String(40), nullable=False)
    document_version = sa.Column(sa.String(20), nullable=False)
    granted = sa.Column(sa.Boolean, nullable=False, default=True)
    evidence = sa.Column(JSONB, nullable=False, default=dict)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = sa.Column(sa.BigInteger, primary_key=True)
    user_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action = sa.Column(sa.String(60), nullable=False)
    entity_type = sa.Column(sa.String(40))
    entity_id = sa.Column(sa.String(64))
    ip_hash = sa.Column(sa.String(64))
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    user = db.relationship("User")


class SiteSetting(db.Model):
    __tablename__ = "site_settings"

    key = sa.Column(sa.String(80), primary_key=True)
    value = sa.Column(JSONB)
    updated_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    updated_by_id = sa.Column(UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"))


class MediaAsset(db.Model):
    """Arquivos de mídia institucional (ex.: foto da médica) persistidos no PostgreSQL,
    pois o disco do Render é efêmero."""
    __tablename__ = "media_assets"

    id = sa.Column(sa.Integer, primary_key=True)
    key = sa.Column(sa.String(60), nullable=False, unique=True)
    content_type = sa.Column(sa.String(40), nullable=False)
    data = sa.Column(sa.LargeBinary, nullable=False)
    sha256 = sa.Column(sa.String(64), nullable=False)
    updated_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
