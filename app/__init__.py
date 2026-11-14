"""Fábrica da aplicação Flask."""
from __future__ import annotations

import json
import logging
import os
import time

import sqlalchemy as sa
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, logout_user
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()  # antes de importar config, que lê o ambiente na importação

from .config import CONFIGS, validate_config  # noqa: E402
from .extensions import csrf, db, limiter, login_manager, migrate  # noqa: E402


def create_app(config_name: str | None = None) -> Flask:
    config_name = config_name or os.environ.get("APP_ENV", "development")
    app = Flask(__name__)
    app.config.from_object(CONFIGS.get(config_name, CONFIGS["development"]))
    app.config["ENV_NAME"] = config_name
    validate_config(app)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # Render usa proxy reverso

    db.init_app(app)
    migrate.init_app(app, db, compare_type=True)
    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)
    login_manager.session_protection = "strong"
    login_manager.login_view = "auth.login"

    from . import models  # noqa: F401  (registra modelos para o Alembic)
    from .blueprints import admin, auth, booking, public
    app.register_blueprint(public.bp)
    app.register_blueprint(booking.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(admin.bp)

    from .cli import register_cli
    register_cli(app)

    _register_hooks(app)
    _register_template_helpers(app)
    _register_errors(app)

    @app.route("/health")
    @limiter.exempt
    def health():
        try:
            db.session.execute(sa.text("SELECT 1"))
            return jsonify(status="ok")
        except Exception:  # noqa: BLE001
            db.session.rollback()
            return jsonify(status="indisponivel"), 503

    return app


def _register_hooks(app: Flask) -> None:
    @app.before_request
    def force_https_and_idle_timeout():
        if app.config.get("FORCE_HTTPS") and not request.is_secure and request.path != "/health":
            return redirect(request.url.replace("http://", "https://", 1), code=301)
        if current_user.is_authenticated:
            now = time.time()
            last = session.get("last_seen", now)
            if now - last > app.config["SESSION_IDLE_MINUTES"] * 60:
                logout_user()
                session.clear()
                from flask import flash
                flash("Sua sessão expirou por inatividade.", "info")
                return redirect(url_for("auth.login"))
            session["last_seen"] = now
        return None

    @app.after_request
    def security_headers(resp):
        h = resp.headers
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                     "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; "
                     "base-uri 'self'; object-src 'none'")
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if app.config.get("FORCE_HTTPS"):
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.path.startswith(("/acesso", "/agendar/agendamento", "/agendar/sucesso")):
            h["Cache-Control"] = "no-store"
            h["X-Robots-Tag"] = "noindex, nofollow"
        if request.path.startswith("/static/"):
            h.setdefault("Cache-Control", "public, max-age=604800")
        return resp


def _register_template_helpers(app: Flask) -> None:
    from .models import STATUS_LABELS
    from .services import settings as site_settings
    from .utils import (WEEKDAYS, WEEKDAYS_SHORT, MONTHS, fmt_date, fmt_datetime, fmt_long_date, fmt_time,
                        format_phone, now_local, to_local, whatsapp_link)

    app.jinja_env.filters.update(
        data=fmt_date, hora=fmt_time, datahora=fmt_datetime, data_extenso=fmt_long_date,
        telefone=format_phone, local=to_local,
    )

    @app.context_processor
    def inject():
        from .blueprints.public import structured_data
        try:
            s = site_settings.get_all()
        except Exception:  # noqa: BLE001  (ex.: banco ainda sem migrações)
            db.session.rollback()
            s = dict(site_settings.DEFAULTS)
        has_photo = False
        try:
            from .models import MediaAsset
            has_photo = db.session.execute(
                sa.select(MediaAsset.id).where(MediaAsset.key == "doctor_photo")).first() is not None
        except Exception:  # noqa: BLE001
            db.session.rollback()

        def jsonld():
            return Markup(json.dumps(structured_data(), ensure_ascii=False).replace("</", "<\\/"))

        def icon(name: str, cls: str = "icon") -> Markup:
            return Markup(f'<svg class="{cls}" aria-hidden="true" focusable="false">'
                          f'<use href="{url_for("static", filename="vendor/icons.svg")}#{name}"></use></svg>')

        return {
            "site": s,
            "public_title": site_settings.public_title() if "doctor_name" in s else "",
            "has_photo": has_photo,
            "status_labels": STATUS_LABELS,
            "weekdays": WEEKDAYS,
            "weekdays_short": WEEKDAYS_SHORT,
            "months": MONTHS,
            "jsonld": jsonld,
            "icon": icon,
            "wa_public": whatsapp_link(s.get("whatsapp")),
            "now_year": now_local().year,
        }


def _register_errors(app: Flask) -> None:
    @app.errorhandler(400)
    def bad_request(e):
        return render_template("errors/error.html", code=400, message="Requisição inválida."), 400

    @app.errorhandler(401)
    def unauthorized(e):
        return redirect(url_for("auth.login"))

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/error.html", code=403,
                               message="Você não tem permissão para acessar esta página."), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/error.html", code=404, message="Página não encontrada."), 404

    @app.errorhandler(413)
    def too_large(e):
        return render_template("errors/error.html", code=413, message="Arquivo muito grande."), 413

    @app.errorhandler(429)
    def too_many(e):
        return render_template("errors/error.html", code=429,
                               message="Muitas tentativas em pouco tempo. Aguarde alguns minutos e tente novamente."), 429

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        return render_template("errors/error.html", code=500,
                               message="Ocorreu um erro inesperado. Tente novamente em instantes."), 500

    from flask_wtf.csrf import CSRFError

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        return render_template("errors/error.html", code=400,
                               message="O formulário expirou. Volte, recarregue a página e tente novamente."), 400
