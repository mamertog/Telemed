"""Autenticação: senha (Argon2id) + MFA obrigatório por TOTP, redefinição segura de senha."""
from __future__ import annotations

import io
import time
from datetime import datetime, timedelta, timezone

import pyotp
import qrcode
import qrcode.image.svg
import sqlalchemy as sa
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from markupsafe import Markup

from ..extensions import db, limiter, login_manager
from ..forms import ChangePasswordForm, ForgotPasswordForm, LoginForm, MFAForm, ResetPasswordForm
from ..models import User
from ..security import (
    audit, burn_password_check, decrypt_secret, encrypt_secret, hash_password, needs_rehash,
    new_session_token, password_problems, verify_password,
)
from ..services import notifications
from ..utils import now_utc

bp = Blueprint("auth", __name__, url_prefix="/acesso")

MAX_FAILED = 5
LOCK_MINUTES = 15
PRE_MFA_TTL = 300  # segundos para concluir a etapa de MFA
GENERIC_LOGIN_ERROR = "E-mail ou senha inválidos, ou conta temporariamente bloqueada."


@login_manager.user_loader
def load_user(user_id: str):
    try:
        uid, token = user_id.split(":", 1)
    except ValueError:
        return None
    user = db.session.get(User, uid)
    if not user or user.session_token != token or not user.is_active:
        return None
    return user


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/painel/api/"):
        return {"erro": "Não autenticado."}, 401
    flash("Faça login para continuar.", "info")
    return redirect(url_for("auth.login"))


def _pending_user() -> User | None:
    uid = session.get("pre_mfa_uid")
    started = session.get("pre_mfa_at", 0)
    if not uid or time.time() - started > PRE_MFA_TTL:
        session.pop("pre_mfa_uid", None)
        return None
    user = db.session.get(User, uid)
    return user if user and user.is_active else None


@bp.route("/entrar", methods=["GET", "POST"])
@limiter.limit("10/minute;60/hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = db.session.execute(sa.select(User).where(User.email == email)).scalar_one_or_none()
        now = now_utc()
        if user is None:
            burn_password_check(form.password.data)
        elif user.locked_until and user.locked_until > now:
            burn_password_check(form.password.data)
            user = None
        elif not verify_password(user.password_hash, form.password.data) or not user.is_active:
            user.failed_logins = (user.failed_logins or 0) + 1
            if user.failed_logins >= MAX_FAILED:
                user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                user.failed_logins = 0
            db.session.commit()
            user = None
        if user is None:
            flash(GENERIC_LOGIN_ERROR, "danger")
            return render_template("auth/login.html", form=form), 401
        user.failed_logins = 0
        user.locked_until = None
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(form.password.data)
        db.session.commit()
        session.clear()
        session["pre_mfa_uid"] = str(user.id)
        session["pre_mfa_at"] = time.time()
        return redirect(url_for("auth.mfa_verify" if user.mfa_enabled else "auth.mfa_setup"))
    return render_template("auth/login.html", form=form)


def _check_totp(user: User, secret: str, code: str) -> bool:
    totp = pyotp.TOTP(secret)
    code = (code or "").strip()
    if not code.isdigit():
        return False
    now = datetime.now(timezone.utc)
    for offset in (-1, 0, 1):
        tc = totp.timecode(now) + offset
        if totp.generate_otp(tc) == code:
            if user.mfa_last_timecode is not None and tc <= user.mfa_last_timecode:
                return False  # proteção contra reutilização do código
            user.mfa_last_timecode = tc
            return True
    return False


def _finish_login(user: User):
    session.clear()
    session.permanent = True
    login_user(user, remember=False)
    session["last_seen"] = time.time()
    user.last_login_at = now_utc()
    audit("login", "user", user.id)
    db.session.commit()
    nxt = request.args.get("next", "")
    if nxt.startswith("/painel") and "//" not in nxt:
        return redirect(nxt)
    return redirect(url_for("admin.dashboard"))


@bp.route("/mfa", methods=["GET", "POST"])
@limiter.limit("10/minute", methods=["POST"])
def mfa_verify():
    user = _pending_user()
    if not user:
        flash("Sessão expirada. Faça login novamente.", "warning")
        return redirect(url_for("auth.login"))
    if not user.mfa_enabled:
        return redirect(url_for("auth.mfa_setup"))
    form = MFAForm()
    if form.validate_on_submit():
        secret = decrypt_secret(user.mfa_secret_enc)
        if secret and _check_totp(user, secret, form.code.data):
            return _finish_login(user)
        db.session.commit()
        flash("Código inválido. Tente novamente.", "danger")
    return render_template("auth/mfa.html", form=form)


@bp.route("/mfa/configurar", methods=["GET", "POST"])
@limiter.limit("10/minute", methods=["POST"])
def mfa_setup():
    user = _pending_user()
    if not user:
        flash("Sessão expirada. Faça login novamente.", "warning")
        return redirect(url_for("auth.login"))
    if user.mfa_enabled:
        return redirect(url_for("auth.mfa_verify"))
    secret = decrypt_secret(user.mfa_pending_secret_enc)
    if not secret:
        secret = pyotp.random_base32()
        user.mfa_pending_secret_enc = encrypt_secret(secret)
        db.session.commit()
    form = MFAForm()
    if form.validate_on_submit():
        if _check_totp(user, secret, form.code.data):
            user.mfa_secret_enc = user.mfa_pending_secret_enc
            user.mfa_pending_secret_enc = None
            user.mfa_enabled = True
            audit("mfa_ativado", "user", user.id)
            db.session.commit()
            flash("Autenticação em dois fatores ativada.", "success")
            return _finish_login(user)
        flash("Código inválido. Confira o horário do celular e tente novamente.", "danger")
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=current_app.config["MFA_ISSUER"])
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    svg = svg[svg.index("<svg"):]
    return render_template("auth/mfa_setup.html", form=form, qr_svg=Markup(svg), secret=secret)


@bp.route("/sair", methods=["POST"])
def logout():
    if current_user.is_authenticated:
        audit("logout", "user", current_user.id, commit=True)
    logout_user()
    session.clear()
    flash("Você saiu do sistema.", "info")
    return redirect(url_for("auth.login"))


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="redefinir-senha")


def make_reset_token(user: User) -> str:
    # Inclui fragmento do hash atual: o link deixa de valer assim que a senha muda (uso único).
    return _serializer().dumps({"u": str(user.id), "h": user.password_hash[-16:]})


@bp.route("/esqueci", methods=["GET", "POST"])
@limiter.limit("5/minute;20/hour", methods=["POST"])
def forgot():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user = db.session.execute(
            sa.select(User).where(User.email == form.email.data.strip().lower())
        ).scalar_one_or_none()
        if user and user.is_active and notifications.provider_configured():
            link = url_for("auth.reset", token=make_reset_token(user), _external=True)
            base = current_app.config.get("PUBLIC_BASE_URL")
            if base:
                link = base + url_for("auth.reset", token=make_reset_token(user))
            notifications.send_email(
                user.email, "Redefinição de senha",
                f"Olá, {user.name}.\n\nPara redefinir sua senha, acesse (válido por 1 hora):\n{link}\n\n"
                "Se você não solicitou, ignore esta mensagem.",
            )
        # Resposta idêntica em todos os casos (anti-enumeração).
        flash("Se o e-mail estiver cadastrado, você receberá as instruções em instantes. "
              "Caso o envio de e-mails não esteja configurado, solicite a redefinição ao administrador.", "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html", form=form)


@bp.route("/redefinir/<token>", methods=["GET", "POST"])
@limiter.limit("10/minute", methods=["POST"])
def reset(token):
    try:
        data = _serializer().loads(token, max_age=3600)
    except (SignatureExpired, BadSignature):
        flash("Link inválido ou expirado.", "danger")
        return redirect(url_for("auth.forgot"))
    user = db.session.get(User, data.get("u"))
    if not user or user.password_hash[-16:] != data.get("h") or not user.is_active:
        flash("Link inválido ou expirado.", "danger")
        return redirect(url_for("auth.forgot"))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        problems = password_problems(form.password.data)
        if problems:
            for p in problems:
                flash(p, "danger")
        else:
            user.password_hash = hash_password(form.password.data)
            user.password_changed_at = now_utc()
            user.session_token = new_session_token()
            audit("senha_redefinida", "user", user.id)
            db.session.commit()
            flash("Senha redefinida. Faça login (o código MFA continuará sendo exigido).", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", form=form)


@bp.route("/senha", methods=["GET", "POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not verify_password(current_user.password_hash, form.current.data):
            flash("Senha atual incorreta.", "danger")
        else:
            problems = password_problems(form.password.data)
            if problems:
                for p in problems:
                    flash(p, "danger")
            else:
                current_user.password_hash = hash_password(form.password.data)
                current_user.password_changed_at = now_utc()
                current_user.session_token = new_session_token()  # encerra outras sessões
                audit("senha_alterada", "user", current_user.id)
                db.session.commit()
                login_user(current_user)
                session["last_seen"] = time.time()
                flash("Senha alterada com sucesso.", "success")
                return redirect(url_for("admin.dashboard"))
    return render_template("auth/change_password.html", form=form)
