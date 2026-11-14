"""Comandos administrativos (flask <comando>)."""
from __future__ import annotations

import getpass
import os
from datetime import timedelta

import click
import sqlalchemy as sa

from .extensions import db
from .models import ACTIVE_STATUSES, ROLE_LABELS, Appointment, Patient, Service, User
from .security import hash_password, new_session_token, password_problems
from .utils import now_utc, slugify

DEFAULT_SERVICES = [
    ("Consulta em Medicina de Família e Comunidade",
     "Consulta com abordagem integral, considerando a história, o contexto e as prioridades de cada pessoa.", "heart-pulse"),
    ("Acompanhamento de condições crônicas",
     "Seguimento regular de condições de longa duração, com plano de cuidado compartilhado.", "activity"),
    ("Cuidados preventivos",
     "Avaliação de rastreamentos e medidas preventivas adequadas a cada fase da vida.", "shield-check"),
    ("Promoção da saúde",
     "Apoio à construção de hábitos e escolhas que favoreçam o bem-estar.", "flower1"),
    ("Saúde da mulher e do homem",
     "Cuidados específicos, com escuta respeitosa e informação clara.", "person-hearts"),
    ("Acompanhamento de adultos e idosos",
     "Atenção às necessidades de cada etapa da vida adulta, com foco em autonomia.", "people"),
    ("Orientações sobre hábitos saudáveis",
     "Conversa prática sobre alimentação, sono, atividade física e saúde emocional.", "sun"),
    ("Cuidado integral e longitudinal",
     "Acompanhamento contínuo ao longo do tempo, com coordenação do cuidado.", "tree"),
]


def register_cli(app):
    @app.cli.command("create-admin")
    @click.option("--email", prompt="E-mail")
    @click.option("--name", prompt="Nome")
    @click.option("--role", type=click.Choice(list(ROLE_LABELS)), default="medica", show_default=True)
    def create_admin(email, name, role):
        """Cria uma conta interna. A senha é lida de forma oculta (ou de ADMIN_INITIAL_PASSWORD)."""
        email = email.strip().lower()
        if db.session.execute(sa.select(User.id).where(User.email == email)).first():
            raise click.ClickException("Já existe uma conta com esse e-mail.")
        password = os.environ.pop("ADMIN_INITIAL_PASSWORD", None)
        if not password:
            password = getpass.getpass("Senha (mín. 12 caracteres): ")
            if password != getpass.getpass("Confirme a senha: "):
                raise click.ClickException("As senhas não coincidem.")
        problems = password_problems(password)
        if problems:
            raise click.ClickException(" ".join(problems))
        user = User(email=email, name=name.strip(), role=role, password_hash=hash_password(password),
                    session_token=new_session_token(), password_changed_at=now_utc())
        db.session.add(user)
        db.session.commit()
        click.echo(f"Conta criada ({ROLE_LABELS[role]}). O MFA será configurado no primeiro login.")

    @app.cli.command("reset-password")
    @click.option("--email", prompt="E-mail")
    def reset_password(email):
        """Redefine a senha de uma conta (recuperação pelo operador do servidor)."""
        user = db.session.execute(sa.select(User).where(User.email == email.strip().lower())).scalar_one_or_none()
        if not user:
            raise click.ClickException("Conta não encontrada.")
        password = getpass.getpass("Nova senha: ")
        if password != getpass.getpass("Confirme: "):
            raise click.ClickException("As senhas não coincidem.")
        problems = password_problems(password)
        if problems:
            raise click.ClickException(" ".join(problems))
        user.password_hash = hash_password(password)
        user.session_token = new_session_token()
        user.failed_logins, user.locked_until = 0, None
        db.session.commit()
        click.echo("Senha redefinida.")

    @app.cli.command("reset-mfa")
    @click.option("--email", prompt="E-mail")
    def reset_mfa(email):
        """Remove o MFA de uma conta; um novo autenticador será exigido no próximo login."""
        user = db.session.execute(sa.select(User).where(User.email == email.strip().lower())).scalar_one_or_none()
        if not user:
            raise click.ClickException("Conta não encontrada.")
        user.mfa_enabled, user.mfa_secret_enc, user.mfa_pending_secret_enc = False, None, None
        user.mfa_last_timecode = None
        user.session_token = new_session_token()
        db.session.commit()
        click.echo("MFA redefinido.")

    @app.cli.command("seed-defaults")
    def seed_defaults():
        """Cadastra os tipos de atendimento sugeridos, INATIVOS (idempotente)."""
        created = 0
        for i, (name, desc, icon) in enumerate(DEFAULT_SERVICES):
            if not db.session.execute(sa.select(Service.id).where(Service.name == name)).first():
                db.session.add(Service(name=name, slug=slugify(name), description=desc, duration_minutes=30,
                                       active=False, position=i, icon=icon))
                created += 1
        db.session.commit()
        click.echo(f"{created} atendimento(s) cadastrado(s) como inativos. Ative-os no painel.")

    @app.cli.command("send-reminders")
    def send_reminders():
        """Envia lembretes de consultas confirmadas (agendar como Cron Job)."""
        from .services.notifications import send_due_reminders
        click.echo(f"{send_due_reminders()} lembrete(s) enviado(s).")

    @app.cli.command("anonymize-inactive")
    @click.option("--years", type=int, default=None, help="Anos sem atendimentos (padrão: configuração).")
    @click.option("--execute", is_flag=True, help="Sem esta opção, apenas simula.")
    def anonymize_inactive(years, execute):
        """Política de retenção: anonimiza cadastros sem atividade há N anos e sem agendamentos futuros."""
        from .services import settings as site_settings
        years = years or int(site_settings.get("retention_years") or 5)
        cutoff = now_utc() - timedelta(days=365 * years)
        last_activity = (sa.select(sa.func.max(Appointment.start_at)).where(Appointment.patient_id == Patient.id)
                         .correlate(Patient).scalar_subquery())
        future_active = sa.exists().where(Appointment.patient_id == Patient.id,
                                          Appointment.start_at >= now_utc(),
                                          Appointment.status.in_(ACTIVE_STATUSES))
        q = sa.select(Patient).where(Patient.status != "anonimizado", Patient.created_at < cutoff, ~future_active,
                                     sa.or_(last_activity.is_(None), last_activity < cutoff))
        patients = db.session.execute(q).scalars().all()
        click.echo(f"{len(patients)} cadastro(s) elegível(is) (inativos há mais de {years} anos).")
        if not execute:
            click.echo("Simulação. Use --execute para anonimizar.")
            return
        for p in patients:
            p.full_name = "Cadastro anonimizado"
            p.name_key = f"anonimizado-{p.id}"
            p.phone, p.phone_norm = "", f"anon{str(p.id)[:12]}"
            p.email = p.email_norm = None
            p.birth_date = None
            p.whatsapp_opt_in = False
            p.status = "anonimizado"
        db.session.commit()
        click.echo("Anonimização concluída.")

    @app.cli.command("check-db")
    def check_db():
        """Verifica a conexão com o PostgreSQL."""
        version = db.session.execute(sa.text("SHOW server_version")).scalar()
        click.echo(f"PostgreSQL conectado (versão {version}).")
