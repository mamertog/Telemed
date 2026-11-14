"""Agendamento online público."""
from __future__ import annotations

import secrets
from datetime import date, timedelta

import sqlalchemy as sa
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for

from ..extensions import db, limiter
from ..forms import BookingForm, LookupForm
from ..models import ACTIVE_STATUSES, ST_CANCELLED, Appointment, Service
from ..services import notifications
from ..services import scheduling
from ..services import settings as site_settings
from ..utils import fmt_long_date, normalize_phone, now_utc, sha256_hex, today_local

bp = Blueprint("booking", __name__, url_prefix="/agendar")


def _bookable_services():
    return db.session.execute(
        sa.select(Service).where(Service.active.is_(True)).order_by(Service.position, Service.name)
    ).scalars().all()


def booking_available() -> bool:
    return bool(site_settings.get("booking_enabled")) and bool(_bookable_services()) and bool(scheduling.active_rules())


@bp.route("/", methods=["GET"])
def start():
    form = BookingForm()
    services = _bookable_services()
    available = booking_available()
    return render_template("booking/start.html", form=form, services=services, available=available,
                           today=today_local())


@bp.route("/api/dias")
@limiter.limit("60/minute")
def api_days():
    service = db.session.get(Service, request.args.get("servico", type=int) or 0)
    month = request.args.get("mes", "")
    if not service or not service.active:
        return jsonify({"erro": "Serviço indisponível."}), 404
    try:
        year, mon = (int(x) for x in month.split("-"))
        date(year, mon, 1)
    except ValueError:
        return jsonify({"erro": "Mês inválido."}), 400
    _, max_date = scheduling.booking_window()
    return jsonify({
        "dias": scheduling.available_days(service, year, mon),
        "min": today_local().isoformat(),
        "max": max_date.isoformat(),
    })


@bp.route("/api/horarios")
@limiter.limit("60/minute")
def api_slots():
    service = db.session.get(Service, request.args.get("servico", type=int) or 0)
    if not service or not service.active:
        return jsonify({"erro": "Serviço indisponível."}), 404
    try:
        d = date.fromisoformat(request.args.get("data", ""))
    except ValueError:
        return jsonify({"erro": "Data inválida."}), 400
    slots = scheduling.slots_for_day(d, service.duration_minutes)
    return jsonify({
        "data": d.isoformat(),
        "rotulo": fmt_long_date(d),
        "horarios": [{"inicio": s.isoformat(), "rotulo": s.strftime("%H:%M")} for s in slots],
    })


@bp.route("/confirmar", methods=["POST"])
@limiter.limit("8/minute;40/hour")
def submit():
    form = BookingForm()
    services = _bookable_services()
    if not booking_available():
        abort(404)
    s = site_settings.get_all()
    if form.validate_on_submit():
        errors = []
        if s.get("require_email") and not form.email.data:
            errors.append("Informe o e-mail para receber a confirmação.")
        modality = form.modality.data if form.modality.data in ("presencial", "teleconsulta") else "presencial"
        if modality == "teleconsulta" and not s.get("modality_teleconsulta"):
            modality = "presencial"
        if not errors:
            try:
                appt, token = scheduling.create_public_booking(
                    service_id=int(form.service_id.data), start_iso=form.start.data,
                    full_name=form.full_name.data, phone=form.phone.data, email=form.email.data,
                    birth_date=form.birth_date.data if s.get("ask_birth_date") else None,
                    whatsapp_opt_in=form.whatsapp_opt_in.data, modality=modality,
                )
            except scheduling.SlotUnavailable as exc:
                errors.append(str(exc))
            except ValueError as exc:
                errors.append(str(exc))
            else:
                notifications.notify(appt, "requested", token=token)
                session["booking_done"] = {"id": str(appt.id), "token": token}
                return redirect(url_for("booking.success"))
        for e in errors:
            flash(e, "danger")
    else:
        flash("Revise os campos destacados e tente novamente.", "danger")
    return render_template("booking/start.html", form=form, services=services, available=True,
                           today=today_local()), 422


@bp.route("/sucesso")
def success():
    data = session.pop("booking_done", None)
    if not data:
        return redirect(url_for("booking.start"))
    appt = db.session.get(Appointment, data["id"])
    if not appt:
        return redirect(url_for("booking.start"))
    return render_template("booking/success.html", appt=appt, token=data["token"])


def _appointment_by_token(token: str) -> Appointment:
    if not token or len(token) > 100:
        abort(404)
    appt = db.session.execute(
        sa.select(Appointment).where(Appointment.public_token_hash == sha256_hex(token))
    ).scalar_one_or_none()
    if not appt:
        abort(404)
    return appt


def _can_patient_cancel(appt: Appointment) -> bool:
    hours = int(site_settings.get("patient_cancel_hours") or 0)
    return appt.status in ACTIVE_STATUSES and appt.start_at - now_utc() >= timedelta(hours=hours)


@bp.route("/agendamento/<token>")
@limiter.limit("30/minute")
def manage(token):
    appt = _appointment_by_token(token)
    return render_template("booking/manage.html", appt=appt, token=token, can_cancel=_can_patient_cancel(appt))


@bp.route("/agendamento/<token>/cancelar", methods=["POST"])
@limiter.limit("10/minute")
def cancel(token):
    appt = _appointment_by_token(token)
    if not _can_patient_cancel(appt):
        flash("Este agendamento não pode mais ser cancelado on-line. Entre em contato com o consultório.", "warning")
        return redirect(url_for("booking.manage", token=token))
    scheduling.change_status(appt, ST_CANCELLED, reason="paciente_solicitou", source="paciente")
    notifications.notify(appt, "cancelled", token=token)
    flash("Agendamento cancelado.", "success")
    return redirect(url_for("booking.manage", token=token))


@bp.route("/consultar", methods=["GET", "POST"])
@limiter.limit("10/minute;30/hour", methods=["POST"])
def lookup():
    form = LookupForm()
    if form.validate_on_submit():
        ref = form.reference.data.strip().upper().replace(" ", "")
        if len(ref) == 8:
            ref = f"{ref[:4]}-{ref[4:]}"
        phone = normalize_phone(form.phone.data)
        appt = db.session.execute(sa.select(Appointment).where(Appointment.reference == ref)).scalar_one_or_none()
        if appt and appt.patient.phone_norm == phone:
            token = secrets.token_urlsafe(32)
            appt.public_token_hash = sha256_hex(token)
            db.session.commit()
            return redirect(url_for("booking.manage", token=token))
        # Mensagem genérica: não revela se o código existe.
        flash("Não foi possível localizar um agendamento com os dados informados.", "warning")
    return render_template("booking/lookup.html", form=form)
