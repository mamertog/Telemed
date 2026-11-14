"""Painel administrativo (acesso restrito, permissões verificadas no servidor em cada rota)."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import sqlalchemy as sa
from flask import (
    Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required
from PIL import ImageOps, Image, UnidentifiedImageError
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..forms import (
    AdminAppointmentForm, BookingSettingsForm, ContactSettingsForm, ExceptionForm, PatientForm, PhotoForm,
    ProfessionalSettingsForm, RescheduleForm, RuleForm, ServiceForm, StatusForm, TextsSettingsForm,
    UserEditForm, UserForm, build_templates_form,
)
from ..models import (
    ACTIVE_STATUSES, ALLOWED_TRANSITIONS, CANCEL_REASONS, ROLE_DOCTOR, ROLE_LABELS, ROLE_TECH_ADMIN,
    ST_CANCELLED, ST_COMPLETED, ST_CONFIRMED, ST_REQUESTED, STATUS_LABELS, Appointment, AuditLog,
    AvailabilityException, AvailabilityRule, MediaAsset, Patient, PrivacyRecord, Service, User,
)
from ..security import (
    audit, decrypt_secret, hash_password, new_session_token, password_problems, permission_required,
    verify_password,
)
from ..services import notifications, scheduling
from ..services import settings as site_settings
from ..utils import (
    MONTHS, TZ, WEEKDAYS, format_phone, name_key, normalize_email, normalize_name, normalize_phone,
    now_local, now_utc, slugify, to_local, today_local, whatsapp_link,
)

bp = Blueprint("admin", __name__, url_prefix="/painel")


@bp.before_request
@login_required
def _require_login():
    """Todas as rotas do painel exigem autenticação completa (senha + MFA)."""
    return None


@bp.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _local(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=TZ)


def _day_range(d: date) -> tuple[datetime, datetime]:
    return _local(d, time.min), _local(d + timedelta(days=1), time.min)


def _appointments_between(start: datetime, end: datetime, include_inactive=True):
    q = (sa.select(Appointment).options(joinedload(Appointment.patient), joinedload(Appointment.service))
         .where(Appointment.start_at >= start, Appointment.start_at < end).order_by(Appointment.start_at))
    if not include_inactive:
        q = q.where(Appointment.status.in_(ACTIVE_STATUSES))
    return db.session.execute(q).scalars().all()


def _parse_date(value: str | None, default: date) -> date:
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


# ============================================================================ Dashboard
@bp.route("/")
def dashboard():
    ctx: dict = {}
    if current_user.can("agenda.view"):
        today = today_local()
        t0, t1 = _day_range(today)
        week_start = today - timedelta(days=today.weekday())
        w0, w1 = _local(week_start, time.min), _local(week_start + timedelta(days=7), time.min)
        count = lambda *conds: db.session.scalar(sa.select(sa.func.count(Appointment.id)).where(*conds))  # noqa: E731
        ctx.update(
            today_count=count(Appointment.start_at >= t0, Appointment.start_at < t1, Appointment.status.in_(ACTIVE_STATUSES)),
            week_count=count(Appointment.start_at >= w0, Appointment.start_at < w1, Appointment.status.in_(ACTIVE_STATUSES)),
            pending_count=count(Appointment.status == ST_REQUESTED, Appointment.start_at >= now_utc()),
            cancelled_count=count(Appointment.status == ST_CANCELLED, Appointment.updated_at >= now_utc() - timedelta(days=30)),
            free_today=scheduling.count_free_slots(today, today),
            free_week=scheduling.count_free_slots(today, today + timedelta(days=6)),
            upcoming=db.session.execute(
                sa.select(Appointment).options(joinedload(Appointment.patient), joinedload(Appointment.service))
                .where(Appointment.start_at >= now_utc(), Appointment.status.in_(ACTIVE_STATUSES))
                .order_by(Appointment.start_at).limit(8)
            ).scalars().all(),
            pending=db.session.execute(
                sa.select(Appointment).options(joinedload(Appointment.patient), joinedload(Appointment.service))
                .where(Appointment.status == ST_REQUESTED, Appointment.start_at >= now_utc())
                .order_by(Appointment.start_at).limit(5)
            ).scalars().all(),
            month=_month_grid(today),
            today=today,
        )
    if current_user.can("patients.view"):
        ctx["patients_count"] = db.session.scalar(
            sa.select(sa.func.count(Patient.id)).where(Patient.status != "anonimizado"))
    ctx["setup_missing"] = _setup_checklist() if current_user.can("settings.manage") else []
    return render_template("admin/dashboard.html", **ctx)


def _setup_checklist() -> list[tuple[str, str]]:
    items = []
    if not db.session.scalar(sa.select(sa.func.count(Service.id)).where(Service.active.is_(True))):
        items.append(("Ative ao menos um tipo de atendimento", url_for("admin.services")))
    if not scheduling.active_rules():
        items.append(("Configure os dias e horários de atendimento", url_for("admin.availability")))
    s = site_settings.get_all()
    if not (s.get("phone") or s.get("whatsapp") or s.get("email")):
        items.append(("Cadastre os canais de contato", url_for("admin.settings_contact")))
    if not s.get("address") and s.get("modality_presencial"):
        items.append(("Informe o endereço do atendimento presencial", url_for("admin.settings_contact")))
    if not notifications.provider_configured():
        items.append(("Configure o serviço de e-mail (opcional)", url_for("admin.system")))
    return items


def _month_grid(ref: date):
    first = ref.replace(day=1)
    start = first - timedelta(days=first.weekday())
    nxt = (first + timedelta(days=32)).replace(day=1)
    end = nxt + timedelta(days=(7 - nxt.weekday()) % 7)
    appts = _appointments_between(_local(start, time.min), _local(end, time.min))
    by_day: dict[date, list] = {}
    for a in appts:
        by_day.setdefault(to_local(a.start_at).date(), []).append(a)
    weeks, d = [], start
    while d < end:
        weeks.append([(d + timedelta(days=i), by_day.get(d + timedelta(days=i), [])) for i in range(7)])
        d += timedelta(days=7)
    return {"weeks": weeks, "first": first, "label": f"{MONTHS[first.month - 1].capitalize()} de {first.year}",
            "prev": (first - timedelta(days=1)).replace(day=1), "next": nxt}


# ============================================================================ Agenda
@bp.route("/agenda")
@permission_required("agenda.view")
def agenda():
    view = request.args.get("view", "semana")
    if view not in ("mes", "semana", "dia"):
        view = "semana"
    d = _parse_date(request.args.get("data"), today_local())
    ctx = {"view": view, "ref": d, "today": today_local()}
    if view == "mes":
        ctx["month"] = _month_grid(d)
    elif view == "semana":
        ws = d - timedelta(days=d.weekday())
        appts = _appointments_between(_local(ws, time.min), _local(ws + timedelta(days=7), time.min))
        days = [(ws + timedelta(days=i), [a for a in appts if to_local(a.start_at).date() == ws + timedelta(days=i)])
                for i in range(7)]
        ctx.update(days=days, prev=ws - timedelta(days=7), next=ws + timedelta(days=7),
                   label=f"{ws.strftime('%d/%m')} a {(ws + timedelta(days=6)).strftime('%d/%m/%Y')}")
    else:
        t0, t1 = _day_range(d)
        appts = _appointments_between(t0, t1)
        exceptions = db.session.execute(sa.select(AvailabilityException).where(
            AvailabilityException.start_at < t1, AvailabilityException.end_at > t0)).scalars().all()
        step = timedelta(minutes=int(site_settings.get("slot_step_minutes") or 30))
        rows = []
        for w in scheduling.day_windows(d, scheduling.active_rules()):
            t = w.start
            while t + step <= w.end:
                e = t + step
                occ = [a for a in appts if a.is_active and a.start_at < e and a.end_at > t]
                blk = [x for x in exceptions if x.start_at < e and x.end_at > t]
                rows.append({"start": t, "appts": occ, "blocked": blk})
                t = e
        ctx.update(rows=rows, appts=appts, exceptions=exceptions, prev=d - timedelta(days=1), next=d + timedelta(days=1),
                   label=f"{WEEKDAYS[d.weekday()].capitalize()}, {d.strftime('%d/%m/%Y')}")
    return render_template("admin/agenda.html", **ctx)


@bp.route("/agendamentos")
@permission_required("agenda.view")
def appointments():
    status = request.args.get("status", "")
    q = sa.select(Appointment).options(joinedload(Appointment.patient), joinedload(Appointment.service))
    if status in STATUS_LABELS:
        q = q.where(Appointment.status == status)
    d_from = _parse_date(request.args.get("de"), None) if request.args.get("de") else None
    d_to = _parse_date(request.args.get("ate"), None) if request.args.get("ate") else None
    if d_from:
        q = q.where(Appointment.start_at >= _local(d_from, time.min))
    if d_to:
        q = q.where(Appointment.start_at < _local(d_to + timedelta(days=1), time.min))
    if not (d_from or d_to or status):
        q = q.where(Appointment.start_at >= _local(today_local(), time.min))
    q = q.order_by(Appointment.start_at)
    page = db.paginate(q, page=request.args.get("page", 1, type=int), per_page=25, error_out=False)
    return render_template("admin/appointments.html", page=page, status=status, statuses=STATUS_LABELS,
                           d_from=d_from, d_to=d_to)


def _get_appointment(appt_id) -> Appointment:
    try:
        appt = db.session.get(Appointment, appt_id)
    except Exception:  # UUID inválido
        db.session.rollback()
        abort(404)
    if not appt:
        abort(404)
    return appt


@bp.route("/agendamentos/<uuid:appt_id>")
@permission_required("agenda.view")
def appointment_detail(appt_id):
    appt = _get_appointment(appt_id)
    form = StatusForm()
    order = [ST_CONFIRMED, ST_COMPLETED, "nao_compareceu", ST_CANCELLED]
    allowed = [st for st in order if st in ALLOWED_TRANSITIONS.get(appt.status, set())]
    form.status.choices = [(s, STATUS_LABELS[s]) for s in allowed]
    wa = None
    if site_settings.get("whatsapp_links_enabled") and appt.patient.whatsapp_opt_in:
        wa = whatsapp_link(appt.patient.phone_norm,
                           f"Olá, {appt.patient.first_name}. Contato referente ao agendamento {appt.reference}.")
    audit("agendamento_visualizado", "appointment", appt.id, commit=True)
    return render_template("admin/appointment_detail.html", appt=appt, form=form, allowed=allowed, wa_link=wa,
                           cancel_reasons=CANCEL_REASONS, statuses=STATUS_LABELS)


@bp.route("/agendamentos/<uuid:appt_id>/status", methods=["POST"])
@permission_required("agenda.manage")
def appointment_status(appt_id):
    appt = _get_appointment(appt_id)
    form = StatusForm()
    form.status.choices = [(s, STATUS_LABELS[s]) for s in ALLOWED_TRANSITIONS.get(appt.status, set())]
    if not form.validate_on_submit():
        flash("Alteração inválida.", "danger")
        return redirect(url_for("admin.appointment_detail", appt_id=appt.id))
    try:
        scheduling.change_status(appt, form.status.data, user_id=current_user.id, reason=form.reason.data)
    except scheduling.InvalidTransition as exc:
        flash(str(exc), "danger")
        return redirect(url_for("admin.appointment_detail", appt_id=appt.id))
    audit(f"agendamento_{form.status.data}", "appointment", appt.id, commit=True)
    if form.notify.data and form.status.data in (ST_CONFIRMED, ST_CANCELLED):
        notifications.notify(appt, "confirmed" if form.status.data == ST_CONFIRMED else "cancelled",
                             user_id=current_user.id)
    flash(f"Status alterado para “{STATUS_LABELS[form.status.data]}”.", "success")
    nxt = request.form.get("next", "")
    if nxt.startswith("/painel/") and "//" not in nxt:
        return redirect(nxt)
    return redirect(url_for("admin.appointment_detail", appt_id=appt.id))


@bp.route("/agendamentos/<uuid:appt_id>/reagendar", methods=["GET", "POST"])
@permission_required("agenda.manage")
def appointment_reschedule(appt_id):
    appt = _get_appointment(appt_id)
    if not appt.is_active:
        flash("Somente agendamentos ativos podem ser reagendados.", "warning")
        return redirect(url_for("admin.appointment_detail", appt_id=appt.id))
    form = RescheduleForm()
    if request.method == "GET":
        form.date.data = to_local(appt.start_at).date()
        form.time.data = to_local(appt.start_at).time()
    if form.validate_on_submit():
        new_start = _local(form.date.data, form.time.data)
        new_end = new_start + (appt.end_at - appt.start_at)
        if not form.allow_outside_rules.data and not scheduling.is_within_rules(new_start, new_end):
            flash("O horário está fora da disponibilidade configurada. Marque a opção para permitir.", "warning")
        else:
            try:
                scheduling.reschedule(appt, new_start, user_id=current_user.id)
            except (scheduling.SlotUnavailable, scheduling.InvalidTransition) as exc:
                flash(str(exc), "danger")
            else:
                audit("agendamento_reagendado", "appointment", appt.id, commit=True)
                if form.notify.data:
                    notifications.notify(appt, "rescheduled", user_id=current_user.id)
                flash("Agendamento reagendado.", "success")
                return redirect(url_for("admin.appointment_detail", appt_id=appt.id))
    return render_template("admin/appointment_reschedule.html", appt=appt, form=form)


@bp.route("/agendamentos/novo", methods=["GET", "POST"])
@permission_required("agenda.manage", "patients.view")
def appointment_new():
    form = AdminAppointmentForm()
    services = db.session.execute(sa.select(Service).order_by(Service.active.desc(), Service.position, Service.name)).scalars().all()
    form.service_id.choices = [(s.id, s.name + ("" if s.active else " (inativo)")) for s in services]
    patient = None
    pid = request.values.get("paciente") or form.patient_id.data
    if pid:
        try:
            patient = db.session.get(Patient, pid)
        except Exception:
            db.session.rollback()
            patient = None
        if patient:
            form.patient_id.data = str(patient.id)
    q = request.args.get("q", "").strip()
    results = _search_patients(q).limit(10).all() if q and not patient else []
    if request.method == "GET" and request.args.get("data"):
        form.date.data = _parse_date(request.args.get("data"), today_local())
    if form.validate_on_submit() and patient:
        service = db.session.get(Service, form.service_id.data)
        start = _local(form.date.data, form.time.data)
        end = start + timedelta(minutes=service.duration_minutes)
        if not form.allow_outside_rules.data and not scheduling.is_within_rules(start, end):
            flash("O horário está fora da disponibilidade configurada. Marque a opção para permitir.", "warning")
        else:
            try:
                appt, token = scheduling.create_admin_appointment(
                    patient=patient, service=service, start=start, status=form.status.data,
                    modality=form.modality.data, user_id=current_user.id)
            except scheduling.SlotUnavailable as exc:
                flash(str(exc), "danger")
            else:
                audit("agendamento_criado", "appointment", appt.id, commit=True)
                notifications.notify(appt, "confirmed" if appt.status == ST_CONFIRMED else "requested",
                                     token=token, user_id=current_user.id)
                flash(f"Agendamento {appt.reference} criado.", "success")
                return redirect(url_for("admin.appointment_detail", appt_id=appt.id))
    elif request.method == "POST" and not patient:
        flash("Selecione um paciente.", "danger")
    return render_template("admin/appointment_new.html", form=form, patient=patient, results=results, q=q)


@bp.route("/api/horarios")
@permission_required("agenda.view")
def api_slots():
    service = db.session.get(Service, request.args.get("servico", type=int) or 0)
    if not service:
        return jsonify({"horarios": []})
    d = _parse_date(request.args.get("data"), None) if request.args.get("data") else None
    if not d:
        return jsonify({"horarios": []})
    try:
        exclude = uuid.UUID(request.args["excluir"]) if request.args.get("excluir") else None
    except ValueError:
        exclude = None
    slots = scheduling.slots_for_day(d, service.duration_minutes, public=False, exclude_appointment_id=exclude)
    return jsonify({"horarios": [s.strftime("%H:%M") for s in slots]})


# ============================================================================ Pacientes
def _search_patients(q: str):
    query = db.session.query(Patient).filter(Patient.status != "anonimizado")
    if q:
        digits = re.sub(r"\D", "", q)
        conds = [Patient.name_key.contains(name_key(q))]
        if len(digits) >= 4:
            conds.append(Patient.phone_norm.contains(digits))
        if "@" in q:
            conds.append(Patient.email_norm.contains(q.strip().lower()))
        query = query.filter(sa.or_(*conds))
    return query.order_by(Patient.full_name)


@bp.route("/pacientes")
@permission_required("patients.view")
def patients():
    q = request.args.get("q", "").strip()[:100]
    status = request.args.get("status", "")
    query = _search_patients(q)
    if status in ("ativo", "inativo"):
        query = query.filter(Patient.status == status)
    page = query.paginate(page=request.args.get("page", 1, type=int), per_page=20, error_out=False)
    ids = [p.id for p in page.items]
    stats = {}
    if ids:
        rows = db.session.execute(
            sa.select(Appointment.patient_id,
                      sa.func.count(Appointment.id),
                      sa.func.count(Appointment.id).filter(Appointment.status == ST_COMPLETED),
                      sa.func.min(Appointment.start_at).filter(Appointment.start_at >= now_utc(),
                                                               Appointment.status.in_(ACTIVE_STATUSES)))
            .where(Appointment.patient_id.in_(ids)).group_by(Appointment.patient_id))
        stats = {r[0]: {"total": r[1], "done": r[2], "next": r[3]} for r in rows}
    if q:
        audit("pacientes_pesquisados", "patient", None, commit=True)
    return render_template("admin/patients.html", page=page, q=q, status=status, stats=stats)


def _get_patient(pid) -> Patient:
    p = db.session.get(Patient, pid)
    if not p or p.status == "anonimizado":
        abort(404)
    return p


@bp.route("/pacientes/<uuid:pid>")
@permission_required("patients.view")
def patient_detail(pid):
    p = _get_patient(pid)
    appts = p.appointments.options(joinedload(Appointment.service)).order_by(Appointment.start_at.desc()).all()
    now = now_utc()
    stats = {
        "total": len(appts),
        "scheduled": sum(1 for a in appts if a.is_active),
        "done": sum(1 for a in appts if a.status == ST_COMPLETED),
        "next": min((a for a in appts if a.is_active and a.start_at >= now), key=lambda a: a.start_at, default=None),
    }
    privacy = db.session.execute(sa.select(PrivacyRecord).where(PrivacyRecord.patient_id == p.id)
                                 .order_by(PrivacyRecord.created_at.desc())).scalars().all()
    dups = scheduling.possible_duplicates(p.phone_norm, p.email_norm, exclude_id=p.id)
    audit("paciente_visualizado", "patient", p.id, commit=True)
    return render_template("admin/patient_detail.html", p=p, appts=appts, stats=stats, privacy=privacy, dups=dups)


@bp.route("/pacientes/novo", methods=["GET", "POST"])
@permission_required("patients.edit")
def patient_new():
    form = PatientForm()
    dups = []
    if form.validate_on_submit():
        phone_norm = normalize_phone(form.phone.data)
        dups = scheduling.possible_duplicates(phone_norm, normalize_email(form.email.data))
        if dups and not form.confirm_duplicate.data:
            flash("Encontramos cadastros com o mesmo telefone ou e-mail. Verifique antes de continuar.", "warning")
        else:
            p = scheduling.build_patient(form.full_name.data, form.phone.data, form.email.data,
                                         form.birth_date.data, form.whatsapp_opt_in.data)
            p.status = form.status.data
            db.session.add(p)
            db.session.flush()
            audit("paciente_criado", "patient", p.id)
            db.session.commit()
            flash("Paciente cadastrado.", "success")
            nxt = request.args.get("next")
            if nxt == "agendar":
                return redirect(url_for("admin.appointment_new", paciente=p.id))
            return redirect(url_for("admin.patient_detail", pid=p.id))
    return render_template("admin/patient_form.html", form=form, dups=dups, p=None)


@bp.route("/pacientes/<uuid:pid>/editar", methods=["GET", "POST"])
@permission_required("patients.edit")
def patient_edit(pid):
    p = _get_patient(pid)
    form = PatientForm(obj=p)
    dups = []
    if form.validate_on_submit():
        phone_norm = normalize_phone(form.phone.data)
        email_norm = normalize_email(form.email.data)
        dups = scheduling.possible_duplicates(phone_norm, email_norm, exclude_id=p.id)
        if dups and not form.confirm_duplicate.data and (phone_norm != p.phone_norm or email_norm != p.email_norm):
            flash("Outro cadastro usa o mesmo telefone ou e-mail. Confirme para continuar.", "warning")
        else:
            prev_opt = p.whatsapp_opt_in
            p.full_name = normalize_name(form.full_name.data)
            p.name_key = name_key(p.full_name)
            p.phone_norm, p.phone = phone_norm, format_phone(phone_norm)
            p.email = p.email_norm = email_norm
            p.birth_date = form.birth_date.data
            p.whatsapp_opt_in = form.whatsapp_opt_in.data
            p.status = form.status.data
            if prev_opt != p.whatsapp_opt_in:
                db.session.add(PrivacyRecord(
                    patient_id=p.id, record_type="consentimento_contato_whatsapp",
                    document_version=site_settings.get("privacy_version"), granted=p.whatsapp_opt_in,
                    evidence={"canal": "painel", "registrado_por": str(current_user.id)}))
            audit("paciente_atualizado", "patient", p.id)
            db.session.commit()
            flash("Cadastro atualizado.", "success")
            return redirect(url_for("admin.patient_detail", pid=p.id))
    return render_template("admin/patient_form.html", form=form, dups=dups, p=p)


@bp.route("/pacientes/exportar")
@permission_required("patients.view", "patients.export")
def patients_export():
    rows = db.session.execute(sa.select(Patient).where(Patient.status != "anonimizado").order_by(Patient.full_name)).scalars()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "nome", "telefone", "email", "data_nascimento", "situacao", "autoriza_whatsapp", "criado_em"])
    for p in rows:
        w.writerow([p.id, p.full_name, p.phone, p.email or "", p.birth_date.isoformat() if p.birth_date else "",
                    p.status, "sim" if p.whatsapp_opt_in else "não", to_local(p.created_at).strftime("%d/%m/%Y")])
    audit("pacientes_exportados", "patient", None, commit=True)
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=pacientes-{today_local().isoformat()}.csv"})


# ============================================================================ Serviços
@bp.route("/servicos")
@permission_required("services.manage")
def services():
    items = db.session.execute(sa.select(Service).order_by(Service.position, Service.name)).scalars().all()
    return render_template("admin/services.html", services=items)


@bp.route("/servicos/novo", methods=["GET", "POST"])
@bp.route("/servicos/<int:sid>/editar", methods=["GET", "POST"])
@permission_required("services.manage")
def service_form(sid=None):
    svc = db.session.get(Service, sid) if sid else None
    if sid and not svc:
        abort(404)
    form = ServiceForm(obj=svc)
    if form.validate_on_submit():
        dup = db.session.execute(sa.select(Service).where(sa.func.lower(Service.name) == form.name.data.strip().lower(),
                                                          Service.id != (svc.id if svc else -1))).scalar_one_or_none()
        if dup:
            flash("Já existe um atendimento com esse nome.", "danger")
        else:
            if svc is None:
                svc = Service(slug=slugify(form.name.data))
                while db.session.execute(sa.select(Service.id).where(Service.slug == svc.slug)).first():
                    svc.slug += "-1"
                db.session.add(svc)
            svc.name = form.name.data.strip()
            svc.description = (form.description.data or "").strip()
            svc.duration_minutes = form.duration_minutes.data
            svc.price = Decimal(form.price.data) if form.price.data is not None else None
            svc.show_price = form.show_price.data and svc.price is not None
            svc.active = form.active.data
            svc.position = form.position.data or 0
            svc.icon = form.icon.data
            db.session.flush()
            audit("servico_salvo", "service", svc.id)
            db.session.commit()
            flash("Atendimento salvo.", "success")
            return redirect(url_for("admin.services"))
    return render_template("admin/service_form.html", form=form, svc=svc)


@bp.route("/servicos/<int:sid>/alternar", methods=["POST"])
@permission_required("services.manage")
def service_toggle(sid):
    svc = db.session.get(Service, sid) or abort(404)
    svc.active = not svc.active
    audit("servico_ativado" if svc.active else "servico_desativado", "service", svc.id)
    db.session.commit()
    flash(f"“{svc.name}” {'ativado' if svc.active else 'desativado'}.", "success")
    return redirect(url_for("admin.services"))


# ============================================================================ Disponibilidade
@bp.route("/disponibilidade", methods=["GET", "POST"])
@permission_required("agenda.view")
def availability():
    form = RuleForm()
    if request.method == "POST":
        if not current_user.can("agenda.configure"):
            abort(403)
        if form.validate_on_submit():
            rule = AvailabilityRule(weekday=form.weekday.data, start_time=form.start_time.data,
                                    end_time=form.end_time.data, break_start=form.break_start.data,
                                    break_end=form.break_end.data, active=True)
            overlapping = [r for r in scheduling.active_rules() if r.weekday == rule.weekday
                           and r.start_time < rule.end_time and r.end_time > rule.start_time]
            if overlapping:
                flash("Já existe um período que se sobrepõe a este horário no mesmo dia.", "danger")
            else:
                db.session.add(rule)
                db.session.flush()
                audit("disponibilidade_criada", "availability_rule", rule.id)
                db.session.commit()
                flash("Período de atendimento adicionado.", "success")
                return redirect(url_for("admin.availability"))
    rules = db.session.execute(sa.select(AvailabilityRule).order_by(AvailabilityRule.weekday,
                                                                    AvailabilityRule.start_time)).scalars().all()
    return render_template("admin/availability.html", form=form, rules=rules, weekdays=WEEKDAYS)


@bp.route("/disponibilidade/<int:rid>/alternar", methods=["POST"])
@permission_required("agenda.configure")
def availability_toggle(rid):
    rule = db.session.get(AvailabilityRule, rid) or abort(404)
    rule.active = not rule.active
    audit("disponibilidade_alterada", "availability_rule", rule.id)
    db.session.commit()
    return redirect(url_for("admin.availability"))


@bp.route("/disponibilidade/<int:rid>/excluir", methods=["POST"])
@permission_required("agenda.configure")
def availability_delete(rid):
    rule = db.session.get(AvailabilityRule, rid) or abort(404)
    db.session.delete(rule)
    audit("disponibilidade_excluida", "availability_rule", rid)
    db.session.commit()
    flash("Período removido. Agendamentos existentes não foram alterados.", "info")
    return redirect(url_for("admin.availability"))


@bp.route("/bloqueios", methods=["GET", "POST"])
@permission_required("agenda.view")
def exceptions():
    form = ExceptionForm()
    conflicts = []
    if request.method == "POST":
        if not current_user.can("agenda.block"):
            abort(403)
        if form.validate_on_submit():
            start = _local(form.start_date.data, form.start_time.data or time.min)
            end = (_local(form.end_date.data, form.end_time.data) if form.end_time.data
                   else _local(form.end_date.data + timedelta(days=1), time.min))
            if end <= start:
                flash("O fim do bloqueio deve ser posterior ao início.", "danger")
            else:
                exc = AvailabilityException(start_at=start, end_at=end, reason=form.reason.data,
                                            created_by_id=current_user.id)
                db.session.add(exc)
                db.session.flush()
                audit("bloqueio_criado", "availability_exception", exc.id)
                db.session.commit()
                conflicts = _appointments_between(start, end, include_inactive=False)
                if conflicts:
                    flash(f"Bloqueio criado. Atenção: {len(conflicts)} agendamento(s) ativo(s) no período "
                          "precisam ser reagendados ou cancelados.", "warning")
                else:
                    flash("Bloqueio criado.", "success")
                    return redirect(url_for("admin.exceptions"))
    items = db.session.execute(sa.select(AvailabilityException).where(AvailabilityException.end_at >= now_utc())
                               .order_by(AvailabilityException.start_at)).scalars().all()
    return render_template("admin/exceptions.html", form=form, items=items, conflicts=conflicts)


@bp.route("/bloqueios/<int:eid>/excluir", methods=["POST"])
@permission_required("agenda.block")
def exception_delete(eid):
    exc = db.session.get(AvailabilityException, eid) or abort(404)
    db.session.delete(exc)
    audit("bloqueio_excluido", "availability_exception", eid)
    db.session.commit()
    flash("Bloqueio removido.", "info")
    return redirect(url_for("admin.exceptions"))


# ============================================================================ Configurações
def _save_settings(values: dict, label: str) -> None:
    changed = site_settings.set_many(values, user_id=current_user.id)
    for key in changed:
        audit("configuracao_alterada", "setting", key)
    db.session.commit()
    flash(f"{label} salvas." if changed else "Nenhuma alteração.", "success" if changed else "info")


@bp.route("/configuracoes", methods=["GET", "POST"])
@permission_required("settings.manage")
def settings_contact():
    s = site_settings.get_all()
    form = ContactSettingsForm(data=s)
    if form.validate_on_submit():
        values = {k: (getattr(form, k).data.strip() if isinstance(getattr(form, k).data, str) else getattr(form, k).data)
                  for k in ("address", "address_complement", "city", "email", "hours_text", "map_url",
                            "modality_presencial", "modality_teleconsulta")}
        values["phone"] = normalize_phone(form.phone.data) or ""
        values["whatsapp"] = normalize_phone(form.whatsapp.data) or ""
        values["email"] = (values["email"] or "").lower()
        _save_settings(values, "Informações de contato")
        return redirect(url_for("admin.settings_contact"))
    if request.method == "GET":
        form.phone.data = format_phone(s.get("phone"))
        form.whatsapp.data = format_phone(s.get("whatsapp"))
    return render_template("admin/settings_contact.html", form=form, tab="contato")


@bp.route("/configuracoes/textos", methods=["GET", "POST"])
@permission_required("settings.manage")
def settings_texts():
    form = TextsSettingsForm(data=site_settings.get_all())
    form.theme.choices = list(site_settings.THEMES.items())
    if form.validate_on_submit():
        _save_settings({k: getattr(form, k).data.strip() for k in
                        ("hero_title", "hero_subtitle", "about_intro", "about_approach", "theme")},
                       "Textos e identidade visual")
        return redirect(url_for("admin.settings_texts"))
    return render_template("admin/settings_texts.html", form=form, tab="textos")


@bp.route("/configuracoes/agendamento", methods=["GET", "POST"])
@permission_required("settings.manage")
def settings_booking():
    form = BookingSettingsForm(data=site_settings.get_all())
    if form.validate_on_submit():
        keys = ("booking_enabled", "min_notice_hours", "max_days_ahead", "slot_step_minutes", "buffer_minutes",
                "require_email", "ask_birth_date", "patient_cancel_hours", "reminder_hours",
                "whatsapp_links_enabled", "retention_years", "privacy_version")
        _save_settings({k: getattr(form, k).data for k in keys}, "Parâmetros de agendamento")
        return redirect(url_for("admin.settings_booking"))
    return render_template("admin/settings_booking.html", form=form, tab="agendamento")


@bp.route("/configuracoes/profissional", methods=["GET", "POST"])
@permission_required("settings.professional")
def settings_professional():
    form = ProfessionalSettingsForm(data=site_settings.get_all())
    if form.validate_on_submit():
        if not verify_password(current_user.password_hash, form.current_password.data):
            flash("Senha incorreta. Nenhuma alteração foi feita.", "danger")
        else:
            values = {k: getattr(form, k).data.strip() for k in ("doctor_name", "crm", "rqe", "graduation", "residency")}
            values["rqe_confirmed"] = bool(form.rqe_confirmed.data and values["rqe"])
            _save_settings(values, "Dados profissionais")
            return redirect(url_for("admin.settings_professional"))
    form.current_password.data = ""
    return render_template("admin/settings_professional.html", form=form, tab="profissional")


@bp.route("/configuracoes/foto", methods=["GET", "POST"])
@permission_required("settings.manage")
def settings_photo():
    form = PhotoForm()
    asset = db.session.execute(sa.select(MediaAsset).where(MediaAsset.key == "doctor_photo")).scalar_one_or_none()
    if form.validate_on_submit():
        try:
            img = Image.open(form.photo.data.stream)
            img.verify()
            form.photo.data.stream.seek(0)
            img = ImageOps.exif_transpose(Image.open(form.photo.data.stream)).convert("RGB")  # remove metadados (EXIF/GPS)
            img.thumbnail((900, 1200))
            out = io.BytesIO()
            img.save(out, "WEBP", quality=85)
            data = out.getvalue()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            flash("Não foi possível processar a imagem enviada.", "danger")
        else:
            if asset is None:
                asset = MediaAsset(key="doctor_photo")
                db.session.add(asset)
            asset.content_type, asset.data = "image/webp", data
            asset.sha256 = hashlib.sha256(data).hexdigest()
            audit("foto_atualizada", "media", "doctor_photo")
            db.session.commit()
            flash("Fotografia atualizada.", "success")
            return redirect(url_for("admin.settings_photo"))
    return render_template("admin/settings_photo.html", form=form, asset=asset, tab="foto")


@bp.route("/configuracoes/foto/remover", methods=["POST"])
@permission_required("settings.manage")
def settings_photo_delete():
    asset = db.session.execute(sa.select(MediaAsset).where(MediaAsset.key == "doctor_photo")).scalar_one_or_none()
    if asset:
        db.session.delete(asset)
        audit("foto_removida", "media", "doctor_photo")
        db.session.commit()
        flash("Fotografia removida.", "info")
    return redirect(url_for("admin.settings_photo"))


@bp.route("/configuracoes/notificacoes", methods=["GET", "POST"])
@permission_required("settings.manage")
def settings_notifications():
    s = site_settings.get_all()
    form = build_templates_form(s)()
    if form.validate_on_submit():
        values = {f.name: f.data for f in form if f.name.startswith("tpl_")}
        _save_settings(values, "Modelos de notificação")
        return redirect(url_for("admin.settings_notifications"))
    groups = [(kind, label, form[f"tpl_{kind}_subject"], form[f"tpl_{kind}_body"])
              for kind, label in notifications.KINDS.items()]
    return render_template("admin/settings_notifications.html", form=form, groups=groups, tab="notificacoes",
                           provider_ok=notifications.provider_configured())


@bp.route("/sistema")
@permission_required("settings.technical")
def system():
    try:
        db_ok = db.session.execute(sa.text("SELECT 1")).scalar() == 1
        revision = db.session.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        db.session.rollback()
        db_ok, revision = False, None
    cfg = current_app.config
    return render_template("admin/system.html", db_ok=db_ok, revision=revision,
                           email_provider=cfg["EMAIL_PROVIDER"], email_ok=notifications.provider_configured(),
                           env=cfg.get("APP_ENV"), https=cfg.get("FORCE_HTTPS"),
                           ratelimit=cfg.get("RATELIMIT_STORAGE_URI"))


# ============================================================================ Usuários
def _manageable_roles() -> dict:
    if current_user.role == ROLE_DOCTOR:
        return dict(ROLE_LABELS)
    # Administrador técnico gerencia apenas contas técnicas (não se concede acesso a pacientes).
    return {ROLE_TECH_ADMIN: ROLE_LABELS[ROLE_TECH_ADMIN]}


def _can_manage(user: User) -> bool:
    return user.role in _manageable_roles()


@bp.route("/usuarios")
@permission_required("users.manage")
def users():
    items = db.session.execute(sa.select(User).order_by(User.name)).scalars().all()
    return render_template("admin/users.html", users=items, can_manage=_can_manage)


@bp.route("/usuarios/novo", methods=["GET", "POST"])
@permission_required("users.manage")
def user_new():
    form = UserForm()
    form.role.choices = list(_manageable_roles().items())
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        problems = password_problems(form.password.data)
        if db.session.execute(sa.select(User.id).where(User.email == email)).first():
            flash("Já existe uma conta com esse e-mail.", "danger")
        elif problems:
            for p in problems:
                flash(p, "danger")
        else:
            u = User(name=form.name.data.strip(), email=email, role=form.role.data,
                     password_hash=hash_password(form.password.data), session_token=new_session_token(),
                     password_changed_at=now_utc())
            db.session.add(u)
            db.session.flush()
            audit("usuario_criado", "user", u.id)
            db.session.commit()
            flash("Conta criada. No primeiro acesso, o usuário configurará o MFA.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form, u=None)


@bp.route("/usuarios/<uuid:uid>/editar", methods=["GET", "POST"])
@permission_required("users.manage")
def user_edit(uid):
    u = db.session.get(User, uid) or abort(404)
    if not _can_manage(u):
        abort(403)
    form = UserEditForm(obj=u)
    form.role.choices = list(_manageable_roles().items())
    reset_link = None
    if form.validate_on_submit():
        if u.id == current_user.id and (form.role.data != u.role or not form.is_active_account.data):
            flash("Você não pode alterar o próprio papel nem desativar a própria conta.", "danger")
        elif u.role == ROLE_DOCTOR and (form.role.data != ROLE_DOCTOR or not form.is_active_account.data) and \
                db.session.scalar(sa.select(sa.func.count(User.id)).where(User.role == ROLE_DOCTOR,
                                                                          User.is_active_account.is_(True))) <= 1:
            flash("É necessário manter ao menos uma conta ativa de médica.", "danger")
        else:
            role_changed = form.role.data != u.role
            deactivated = u.is_active_account and not form.is_active_account.data
            u.name = form.name.data.strip()
            u.role = form.role.data
            u.is_active_account = form.is_active_account.data
            if role_changed or deactivated:
                u.session_token = new_session_token()
            audit("usuario_atualizado", "user", u.id)
            db.session.commit()
            flash("Conta atualizada.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form, u=u, reset_link=reset_link)


@bp.route("/usuarios/<uuid:uid>/redefinir-mfa", methods=["POST"])
@permission_required("users.manage")
def user_reset_mfa(uid):
    u = db.session.get(User, uid) or abort(404)
    if not _can_manage(u) or u.id == current_user.id:
        abort(403)
    u.mfa_enabled = False
    u.mfa_secret_enc = None
    u.mfa_pending_secret_enc = None
    u.mfa_last_timecode = None
    u.session_token = new_session_token()
    audit("mfa_redefinido", "user", u.id)
    db.session.commit()
    flash("MFA redefinido. O usuário configurará um novo autenticador no próximo login.", "success")
    return redirect(url_for("admin.user_edit", uid=u.id))


@bp.route("/usuarios/<uuid:uid>/link-senha", methods=["POST"])
@permission_required("users.manage")
def user_reset_link(uid):
    from .auth import make_reset_token
    u = db.session.get(User, uid) or abort(404)
    if not _can_manage(u) or u.id == current_user.id:
        abort(403)
    audit("link_redefinicao_gerado", "user", u.id, commit=True)
    link = url_for("auth.reset", token=make_reset_token(u), _external=True)
    form = UserEditForm(obj=u)
    form.role.choices = list(_manageable_roles().items())
    return render_template("admin/user_form.html", form=form, u=u, reset_link=link)


# ============================================================================ Auditoria
@bp.route("/auditoria")
@permission_required("audit.view")
def audit_logs():
    q = sa.select(AuditLog).options(joinedload(AuditLog.user)).order_by(AuditLog.created_at.desc())
    action = request.args.get("acao", "").strip()
    if action:
        q = q.where(AuditLog.action.contains(action[:60]))
    page = db.paginate(q, page=request.args.get("page", 1, type=int), per_page=40, error_out=False)
    return render_template("admin/audit.html", page=page, action=action)
