"""Agendamento online público."""
from __future__ import annotations

import secrets
from datetime import date, timedelta

import sqlalchemy as sa
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from ..extensions import db, limiter
from ..forms import BookingForm, LookupForm, BookingRequestForm
from ..models import ACTIVE_STATUSES, ST_CANCELLED, Appointment, Service, BookingRequest, PrivacyRecord
from ..services import notifications
from ..services import scheduling
from ..services import settings as site_settings
from ..utils import fmt_date, fmt_time, whatsapp_link, fmt_long_date, normalize_phone, normalize_email, hashed_ip, new_reference, now_utc, sha256_hex, today_local

bp = Blueprint("booking", __name__, url_prefix="/agendar")


def _bookable_services():
    return db.session.execute(
        sa.select(Service).where(Service.active.is_(True)).order_by(Service.position, Service.name)
    ).scalars().all()


def booking_available() -> bool:
    return bool(site_settings.get("booking_enabled")) and bool(_bookable_services()) and bool(scheduling.active_rules())


@bp.route("/", methods=["GET"])
def start():
    form = BookingRequestForm()
    key = secrets.token_urlsafe(32)
    session["request_submission_key"] = key
    form.submission_key.data = key
    return render_template("booking/request.html", form=form,
                           available=bool(site_settings.get("booking_enabled")))


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
                notifications.notify_doctor(appt)
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
    message = (
        f"Olá! Fiz um agendamento para {fmt_date(appt.start_at)} "
        f"às {fmt_time(appt.start_at)}. Meu código é {appt.reference}. "
        "Aguardo a confirmação."
    )
    booking_whatsapp = whatsapp_link(site_settings.get("whatsapp"), message)
    return render_template("booking/success.html", appt=appt, token=data["token"],
                           booking_whatsapp=booking_whatsapp)


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


@bp.route("/solicitar", methods=["POST"])
@limiter.limit("8/minute;40/hour")
def request_submit():
    if not site_settings.get("booking_enabled"):
        abort(404)
    form = BookingRequestForm()
    if not form.validate_on_submit():
        if request.accept_mimetypes.best == "application/json":
            return jsonify({"errors": form.errors}), 422
        return render_template("booking/request.html", form=form, available=True), 422
    key = form.submission_key.data
    if key != session.get("request_submission_key"):
        abort(400)
    # Serialize identical submissions; retrying cannot create another request.
    db.session.execute(sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})
    item = db.session.execute(sa.select(BookingRequest).where(BookingRequest.submission_key == key)).scalar_one_or_none()
    if item is None:
        patient = scheduling.find_matching_patient(form.full_name.data, normalize_phone(form.phone.data))
        if patient is None:
            patient = scheduling.build_patient(form.full_name.data, form.phone.data, form.email.data,
                form.birth_date.data if site_settings.get("ask_birth_date") else None, form.whatsapp_opt_in.data)
            db.session.add(patient)
        email = normalize_email(form.email.data)
        if email and not patient.email:
            patient.email, patient.email_norm = email, email
        item = BookingRequest(reference=new_reference(), patient=patient, email=email, submission_key=key)
        db.session.add(item)
        db.session.flush()
        evidence = {"ip_hash": hashed_ip(), "canal": "solicitacao_consulta"}
        db.session.add(PrivacyRecord(patient_id=patient.id, record_type="aviso_privacidade_ciencia",
            document_version=site_settings.get("privacy_version"), granted=True, evidence=evidence))
        if form.whatsapp_opt_in.data:
            patient.whatsapp_opt_in = True
            db.session.add(PrivacyRecord(patient_id=patient.id, record_type="consentimento_contato_whatsapp",
                document_version=site_settings.get("privacy_version"), granted=True, evidence=evidence))
        db.session.commit()
    else:
        db.session.commit()
    session["request_done"] = str(item.id)
    next_url = url_for("booking.request_success")
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"saved": True, "whatsapp": _request_whatsapp(item), "next": next_url})
    return redirect(next_url)


def _request_whatsapp(item):
    return whatsapp_link(site_settings.get("whatsapp"),
        f"Olá! Gostaria de agendar uma consulta com a Dra. Juliana. "
        f"Preenchi meus dados no site. Minha referência é {item.reference}. "
        "Podemos combinar uma data e horário?")


@bp.route("/solicitacao-enviada")
def request_success():
    ident = session.get("request_done")
    item = db.session.get(BookingRequest, ident) if ident else None
    if item is None:
        return redirect(url_for("booking.start"))
    return render_template("booking/request_success.html", item=item, whatsapp=_request_whatsapp(item))


@bp.after_request
def private_request_pages(response):
    if request.endpoint in {"booking.start", "booking.request_submit", "booking.request_success"}:
        response.headers["Cache-Control"] = "no-store"
    return response
