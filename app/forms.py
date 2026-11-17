"""Formulários (Flask-WTF) com validação no servidor e CSRF."""
from __future__ import annotations

import re
from datetime import date

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import (
    BooleanField, DateField, DecimalField, EmailField, HiddenField, IntegerField, PasswordField,
    SelectField, StringField, TextAreaField, TimeField,
)
from wtforms.validators import URL, DataRequired, Email, InputRequired, Length, NumberRange, Optional, ValidationError

from .models import CANCEL_REASONS, ROLE_LABELS, STATUS_LABELS
from .utils import normalize_phone

MSG_REQ = "Campo obrigatório."


def phone_validator(form, field):
    if field.data and not normalize_phone(field.data):
        raise ValidationError("Informe um telefone válido com DDD.")


def name_validator(form, field):
    value = (field.data or "").strip()
    if len(value.split()) < 2:
        raise ValidationError("Informe nome e sobrenome.")
    if re.search(r"[<>{}\[\]\\/;=]|https?:", value):
        raise ValidationError("O nome contém caracteres não permitidos.")


def birth_validator(form, field):
    if field.data:
        if field.data > date.today() or field.data.year < 1900:
            raise ValidationError("Data de nascimento inválida.")


# ---------------------------------------------------------------- Autenticação
class LoginForm(FlaskForm):
    email = EmailField("E-mail", validators=[DataRequired(MSG_REQ), Email("E-mail inválido."), Length(max=255)])
    password = PasswordField("Senha", validators=[DataRequired(MSG_REQ), Length(max=200)])


class MFAForm(FlaskForm):
    code = StringField("Código de 6 dígitos", validators=[DataRequired(MSG_REQ), Length(min=6, max=6)],
                       render_kw={"inputmode": "numeric", "autocomplete": "one-time-code", "pattern": "[0-9]{6}"})


class ForgotPasswordForm(FlaskForm):
    email = EmailField("E-mail", validators=[DataRequired(MSG_REQ), Email("E-mail inválido."), Length(max=255)])


class ResetPasswordForm(FlaskForm):
    password = PasswordField("Nova senha", validators=[DataRequired(MSG_REQ), Length(max=200)],
                             render_kw={"autocomplete": "new-password"})
    confirm = PasswordField("Confirme a nova senha", validators=[DataRequired(MSG_REQ), Length(max=200)],
                            render_kw={"autocomplete": "new-password"})

    def validate_confirm(self, field):
        if field.data != self.password.data:
            raise ValidationError("As senhas não coincidem.")


class ChangePasswordForm(ResetPasswordForm):
    current = PasswordField("Senha atual", validators=[DataRequired(MSG_REQ), Length(max=200)],
                            render_kw={"autocomplete": "current-password"})


# ---------------------------------------------------------------- Agendamento público
class BookingForm(FlaskForm):
    service_id = HiddenField(validators=[DataRequired("Selecione o tipo de atendimento.")])
    start = HiddenField(validators=[DataRequired("Selecione um horário.")])
    modality = HiddenField(default="presencial")
    full_name = StringField("Nome completo", validators=[DataRequired(MSG_REQ), Length(min=5, max=160), name_validator],
                            render_kw={"autocomplete": "name"})
    phone = StringField("Telefone (com DDD)", validators=[DataRequired(MSG_REQ), Length(max=30), phone_validator],
                        render_kw={"autocomplete": "tel", "inputmode": "tel"})
    email = EmailField("E-mail", validators=[Optional(), Email("E-mail inválido."), Length(max=255)],
                       render_kw={"autocomplete": "email"})
    birth_date = DateField("Data de nascimento", validators=[Optional(), birth_validator])
    privacy_ack = BooleanField("Li o aviso de privacidade e estou ciente de como meus dados serão utilizados.",
                               validators=[InputRequired("É necessário confirmar a leitura do aviso de privacidade.")])
    whatsapp_opt_in = BooleanField("Autorizo, opcionalmente, contato pelo WhatsApp para assuntos do agendamento.")
    website = StringField("Deixe em branco", validators=[Optional(), Length(max=0)])  # honeypot anti-robô


class LookupForm(FlaskForm):
    reference = StringField("Código de referência", validators=[DataRequired(MSG_REQ), Length(min=8, max=12)])
    phone = StringField("Telefone informado no agendamento", validators=[DataRequired(MSG_REQ), phone_validator])


# ---------------------------------------------------------------- Painel: pacientes
class PatientForm(FlaskForm):
    full_name = StringField("Nome completo", validators=[DataRequired(MSG_REQ), Length(min=5, max=160), name_validator])
    phone = StringField("Telefone", validators=[DataRequired(MSG_REQ), Length(max=30), phone_validator])
    email = EmailField("E-mail", validators=[Optional(), Email("E-mail inválido."), Length(max=255)])
    birth_date = DateField("Data de nascimento", validators=[Optional(), birth_validator])
    whatsapp_opt_in = BooleanField("Paciente autorizou contato por WhatsApp")
    status = SelectField("Situação cadastral", choices=[("ativo", "Ativo"), ("inativo", "Inativo")])
    confirm_duplicate = BooleanField("Confirmo que não se trata de cadastro duplicado")


# ---------------------------------------------------------------- Painel: agenda
class AdminAppointmentForm(FlaskForm):
    patient_id = HiddenField(validators=[DataRequired("Selecione um paciente.")])
    service_id = SelectField("Tipo de atendimento", coerce=int, validators=[DataRequired(MSG_REQ)])
    date = DateField("Data", validators=[DataRequired(MSG_REQ)])
    time = TimeField("Horário", validators=[DataRequired(MSG_REQ)])
    modality = SelectField("Modalidade", choices=[("presencial", "Presencial"), ("teleconsulta", "Teleconsulta")])
    status = SelectField("Status inicial", choices=[("confirmado", "Confirmado"), ("solicitado", "Solicitado")])
    allow_outside_rules = BooleanField("Permitir horário fora da disponibilidade configurada")


class RescheduleForm(FlaskForm):
    date = DateField("Nova data", validators=[DataRequired(MSG_REQ)])
    time = TimeField("Novo horário", validators=[DataRequired(MSG_REQ)])
    allow_outside_rules = BooleanField("Permitir horário fora da disponibilidade configurada")
    notify = BooleanField("Notificar o paciente por e-mail (se configurado)", default=True)


class StatusForm(FlaskForm):
    status = SelectField("Novo status", choices=list(STATUS_LABELS.items()))
    reason = SelectField("Motivo (cancelamento)", choices=list(CANCEL_REASONS.items()))
    notify = BooleanField("Notificar o paciente por e-mail (se configurado)", default=True)


class ServiceForm(FlaskForm):
    name = StringField("Nome do atendimento", validators=[DataRequired(MSG_REQ), Length(max=120)])
    description = TextAreaField("Descrição", validators=[Optional(), Length(max=1000)])
    duration_minutes = IntegerField("Duração (minutos)", validators=[InputRequired(MSG_REQ), NumberRange(10, 480)], default=30)
    price = DecimalField("Valor (R$, opcional)", places=2, validators=[Optional(), NumberRange(min=0)])
    show_price = BooleanField("Exibir valor no site")
    active = BooleanField("Ativo (exibido no site e disponível para agendamento)")
    position = IntegerField("Ordem de exibição", validators=[Optional(), NumberRange(0, 999)], default=0)
    icon = SelectField("Ícone", choices=[
        ("heart-pulse", "Coração"), ("activity", "Atividade"), ("shield-check", "Prevenção"),
        ("flower1", "Bem-estar"), ("person-hearts", "Pessoas"), ("people", "Família"),
        ("sun", "Hábitos"), ("chat-heart", "Escuta"), ("house-heart", "Cuidado"), ("tree", "Longitudinal"),
        ("camera-video", "Teleconsulta"),
    ])


WEEKDAY_CHOICES = [(0, "Segunda-feira"), (1, "Terça-feira"), (2, "Quarta-feira"), (3, "Quinta-feira"),
                   (4, "Sexta-feira"), (5, "Sábado"), (6, "Domingo")]


class RuleForm(FlaskForm):
    weekday = SelectField("Dia da semana", coerce=int, choices=WEEKDAY_CHOICES)
    start_time = TimeField("Início", validators=[DataRequired(MSG_REQ)])
    end_time = TimeField("Término", validators=[DataRequired(MSG_REQ)])
    break_start = TimeField("Início do intervalo", validators=[Optional()])
    break_end = TimeField("Fim do intervalo", validators=[Optional()])

    def validate_end_time(self, field):
        if self.start_time.data and field.data and field.data <= self.start_time.data:
            raise ValidationError("O término deve ser posterior ao início.")

    def validate_break_end(self, field):
        bs, be = self.break_start.data, field.data
        if bool(bs) != bool(be):
            raise ValidationError("Informe início e fim do intervalo, ou deixe ambos vazios.")
        if bs and be:
            if be <= bs:
                raise ValidationError("O fim do intervalo deve ser posterior ao início.")
            if self.start_time.data and self.end_time.data and (bs < self.start_time.data or be > self.end_time.data):
                raise ValidationError("O intervalo deve estar dentro do horário de atendimento.")


class ExceptionForm(FlaskForm):
    start_date = DateField("Data inicial", validators=[DataRequired(MSG_REQ)])
    start_time = TimeField("Hora inicial (vazio = dia inteiro)", validators=[Optional()])
    end_date = DateField("Data final", validators=[DataRequired(MSG_REQ)])
    end_time = TimeField("Hora final (vazio = dia inteiro)", validators=[Optional()])
    reason = SelectField("Motivo administrativo", choices=[
        ("Férias", "Férias"), ("Feriado", "Feriado"), ("Compromisso profissional", "Compromisso profissional"),
        ("Bloqueio de horário", "Bloqueio de horário"), ("Outro", "Outro"),
    ])


# ---------------------------------------------------------------- Painel: configurações
class ContactSettingsForm(FlaskForm):
    address = StringField("Endereço profissional", validators=[Optional(), Length(max=200)])
    address_complement = StringField("Complemento", validators=[Optional(), Length(max=120)])
    city = StringField("Cidade / UF", validators=[Optional(), Length(max=80)])
    phone = StringField("Telefone", validators=[Optional(), Length(max=30), phone_validator])
    whatsapp = StringField("WhatsApp", validators=[Optional(), Length(max=30), phone_validator])
    email = EmailField("E-mail de contato", validators=[Optional(), Email("E-mail inválido."), Length(max=255)])
    hours_text = TextAreaField("Horário de atendimento (texto exibido)", validators=[Optional(), Length(max=400)])
    map_url = StringField("Link de localização (Google Maps etc.)",
                          validators=[Optional(), URL(require_tld=True, message="URL inválida."), Length(max=500)])
    modality_presencial = BooleanField("Atendimento presencial")
    modality_teleconsulta = BooleanField("Teleconsulta")

    def validate_map_url(self, field):
        if field.data and not field.data.startswith("https://"):
            raise ValidationError("Use um link iniciado por https://")


class TextsSettingsForm(FlaskForm):
    hero_title = StringField("Mensagem principal", validators=[DataRequired(MSG_REQ), Length(max=140)])
    hero_subtitle = TextAreaField("Subtítulo", validators=[DataRequired(MSG_REQ), Length(max=300)])
    about_intro = TextAreaField("Apresentação (página Sobre)", validators=[DataRequired(MSG_REQ), Length(max=1500)])
    about_approach = TextAreaField("Abordagem de cuidado", validators=[DataRequired(MSG_REQ), Length(max=1500)])
    theme = SelectField("Identidade visual", choices=[])


class BookingSettingsForm(FlaskForm):
    booking_enabled = BooleanField("Agendamento online habilitado")
    min_notice_hours = IntegerField("Antecedência mínima (horas)", validators=[InputRequired(), NumberRange(0, 720)])
    max_days_ahead = IntegerField("Agendar até quantos dias à frente", validators=[InputRequired(), NumberRange(1, 365)])
    slot_step_minutes = IntegerField("Intervalo entre horários exibidos (min)", validators=[InputRequired(), NumberRange(5, 240)])
    buffer_minutes = IntegerField("Intervalo entre consultas (min)", validators=[InputRequired(), NumberRange(0, 120)])
    require_email = BooleanField("Exigir e-mail no agendamento (para confirmação eletrônica)")
    ask_birth_date = BooleanField("Solicitar data de nascimento (somente se houver justificativa operacional)")
    patient_cancel_hours = IntegerField("Paciente pode cancelar até (horas antes)", validators=[InputRequired(), NumberRange(0, 720)])
    reminder_hours = IntegerField("Enviar lembrete (horas antes)", validators=[InputRequired(), NumberRange(1, 168)])
    whatsapp_links_enabled = BooleanField("Exibir atalhos de WhatsApp no painel (apenas para pacientes que autorizaram)")
    retention_years = IntegerField("Retenção de cadastros inativos (anos)", validators=[InputRequired(), NumberRange(1, 30)])
    privacy_version = StringField("Versão vigente do aviso de privacidade", validators=[DataRequired(), Length(max=20)])


class ProfessionalSettingsForm(FlaskForm):
    doctor_name = StringField("Nome profissional", validators=[DataRequired(MSG_REQ), Length(max=120)])
    crm = StringField("CRM", validators=[DataRequired(MSG_REQ), Length(max=30)])
    rqe = StringField("RQE (somente após registro confirmado)", validators=[Optional(), Length(max=20)])
    rqe_confirmed = BooleanField("Confirmo que o RQE informado está registrado no CRM")
    graduation = StringField("Graduação", validators=[DataRequired(MSG_REQ), Length(max=200)])
    residency = StringField("Residência", validators=[DataRequired(MSG_REQ), Length(max=200)])
    current_password = PasswordField("Sua senha (confirmação obrigatória)", validators=[DataRequired(MSG_REQ)])

    def validate_crm(self, field):
        if not re.fullmatch(r"CRM-[A-Z]{2} \d{1,3}(\.\d{3})*", field.data.strip()):
            raise ValidationError("Formato esperado: CRM-UF 000.000")

    def validate_rqe(self, field):
        if field.data and not re.fullmatch(r"\d{1,7}", field.data.strip()):
            raise ValidationError("O RQE deve conter apenas números.")

    def validate_rqe_confirmed(self, field):
        if field.data and not (self.rqe.data or "").strip():
            raise ValidationError("Informe o número do RQE antes de confirmá-lo.")


class PhotoForm(FlaskForm):
    photo = FileField("Fotografia profissional (JPG, PNG ou WebP, até 4 MB)", validators=[
        FileRequired("Selecione um arquivo."), FileAllowed(["jpg", "jpeg", "png", "webp"], "Formato não permitido."),
    ])
    authorized = BooleanField("Confirmo que o uso desta imagem foi autorizado",
                              validators=[InputRequired("Confirme a autorização de uso da imagem.")])


class NotificationTemplatesForm(FlaskForm):
    pass


def build_templates_form(values: dict):
    """Cria dinamicamente o formulário de modelos de notificação."""
    class _F(NotificationTemplatesForm):
        pass
    for kind in ("requested", "confirmed", "cancelled", "rescheduled", "reminder"):
        setattr(_F, f"tpl_{kind}_subject", StringField("Assunto", validators=[DataRequired(), Length(max=160)],
                                                       default=values.get(f"tpl_{kind}_subject")))
        setattr(_F, f"tpl_{kind}_body", TextAreaField("Mensagem", validators=[DataRequired(), Length(max=2000)],
                                                      default=values.get(f"tpl_{kind}_body")))
    return _F


# ---------------------------------------------------------------- Painel: usuários
class UserForm(FlaskForm):
    name = StringField("Nome", validators=[DataRequired(MSG_REQ), Length(max=120)])
    email = EmailField("E-mail", validators=[DataRequired(MSG_REQ), Email("E-mail inválido."), Length(max=255)])
    role = SelectField("Papel", choices=list(ROLE_LABELS.items()))
    password = PasswordField("Senha inicial", validators=[DataRequired(MSG_REQ), Length(max=200)],
                             render_kw={"autocomplete": "new-password"})


class UserEditForm(FlaskForm):
    name = StringField("Nome", validators=[DataRequired(MSG_REQ), Length(max=120)])
    role = SelectField("Papel", choices=list(ROLE_LABELS.items()))
    is_active_account = BooleanField("Conta ativa")


class BookingRequestForm(BookingForm):
    service_id = HiddenField(validators=[Optional()])
    start = HiddenField(validators=[Optional()])
    submission_key = HiddenField(validators=[DataRequired(MSG_REQ), Length(min=32, max=64)])


class BookingRequestStatusForm(FlaskForm):
    status = SelectField("Situação", choices=[("pendente", "Pendente"), ("em_contato", "Em contato"), ("concluida", "Concluída"), ("cancelada", "Cancelada")], validators=[DataRequired(MSG_REQ)])
