"""Configurações editáveis do site, persistidas no PostgreSQL (tabela site_settings)."""
from __future__ import annotations

from flask import g

from ..extensions import db
from ..models import SiteSetting

# Somente dados oficialmente fornecidos. Campos desconhecidos ficam vazios e
# não são publicados até serem preenchidos no painel.
DEFAULTS: dict = {
    # Dados profissionais (edição restrita, com confirmação de senha e auditoria)
    "doctor_name": "Dra. Juliana Gonçalves Rosa",
    "crm": "CRM-SP 238.870",
    "rqe": "",
    "rqe_confirmed": False,
    "graduation": "Graduação em Medicina pela Universidade Estadual de Campinas (UNICAMP), 2023",
    "residency": "Residência médica concluída em Medicina de Família e Comunidade",

    # Contato e localização (vazios até a médica cadastrar)
    "address": "",
    "address_complement": "",
    "city": "",
    "phone": "",
    "whatsapp": "",
    "email": "",
    "hours_text": "",
    "map_url": "",
    "modality_presencial": True,
    "modality_teleconsulta": False,

    # Textos institucionais
    "hero_title": "Cuidado próximo, contínuo e integral para cada fase da vida.",
    "hero_subtitle": (
        "Um espaço de acolhimento, escuta e acompanhamento da sua saúde, com foco na "
        "prevenção e no cuidado centrado em cada pessoa."
    ),
    "about_intro": (
        "Sou médica formada pela Universidade Estadual de Campinas (UNICAMP) e concluí residência "
        "médica em Medicina de Família e Comunidade. Acredito em um cuidado que começa pela escuta: "
        "conhecer a pessoa, sua história, sua rotina e aquilo que é importante para ela."
    ),
    "about_approach": (
        "Meu trabalho é orientado pelo cuidado centrado na pessoa, pela continuidade do acompanhamento "
        "ao longo do tempo e pela coordenação do cuidado com outros profissionais sempre que necessário. "
        "Cada consulta é um momento de construir, juntos, decisões compartilhadas sobre a saúde."
    ),

    # Identidade visual básica
    "theme": "salvia",  # salvia | petroleo | areia

    # Parâmetros de agendamento
    "booking_enabled": True,
    "min_notice_hours": 12,
    "max_days_ahead": 60,
    "slot_step_minutes": 30,
    "buffer_minutes": 0,
    "require_email": False,
    "ask_birth_date": False,
    "patient_cancel_hours": 24,
    "reminder_hours": 24,
    "whatsapp_links_enabled": False,

    # Privacidade
    "privacy_version": "1.0",
    "retention_years": 5,

    # Modelos de notificação (sem informações clínicas).
    # Variáveis: {nome}, {data}, {hora}, {servico}, {codigo}, {link}, {medica}
    "tpl_requested_subject": "Recebemos sua solicitação de consulta — {codigo}",
    "tpl_requested_body": (
        "Olá, {nome}.\n\nRecebemos sua solicitação de consulta para {data} às {hora} ({servico}).\n"
        "Ela será analisada e você receberá a confirmação em breve.\n\n"
        "Código de referência: {codigo}\nAcompanhe ou cancele: {link}\n\n{medica}"
    ),
    "tpl_confirmed_subject": "Consulta confirmada — {data} às {hora}",
    "tpl_confirmed_body": (
        "Olá, {nome}.\n\nSua consulta está confirmada para {data} às {hora} ({servico}).\n\n"
        "Código de referência: {codigo}\nAcompanhe ou cancele: {link}\n\n{medica}"
    ),
    "tpl_cancelled_subject": "Consulta cancelada — {codigo}",
    "tpl_cancelled_body": (
        "Olá, {nome}.\n\nA consulta de {data} às {hora} foi cancelada.\n"
        "Se desejar, você pode solicitar um novo horário pelo site.\n\n{medica}"
    ),
    "tpl_rescheduled_subject": "Consulta reagendada — {data} às {hora}",
    "tpl_rescheduled_body": (
        "Olá, {nome}.\n\nSua consulta foi reagendada para {data} às {hora} ({servico}).\n\n"
        "Código de referência: {codigo}\nAcompanhe ou cancele: {link}\n\n{medica}"
    ),
    "tpl_reminder_subject": "Lembrete: consulta em {data} às {hora}",
    "tpl_reminder_body": (
        "Olá, {nome}.\n\nLembramos que sua consulta está marcada para {data} às {hora} ({servico}).\n"
        "Caso não possa comparecer, cancele pelo link: {link}\n\n{medica}"
    ),
}

PROFESSIONAL_KEYS = {"doctor_name", "crm", "rqe", "rqe_confirmed", "graduation", "residency"}
THEMES = {"salvia": "Verde-sálvia", "petroleo": "Azul-petróleo", "areia": "Neutro areia"}


def get_all() -> dict:
    """Configurações efetivas (padrões + valores salvos), com cache por requisição."""
    cached = getattr(g, "_site_settings", None) if g else None
    if cached is not None:
        return cached
    data = dict(DEFAULTS)
    for row in db.session.execute(db.select(SiteSetting)).scalars():
        data[row.key] = row.value
    try:
        g._site_settings = data
    except RuntimeError:
        pass
    return data


def get(key: str, default=None):
    return get_all().get(key, DEFAULTS.get(key, default))


def set_many(values: dict, user_id=None) -> list[str]:
    """Grava valores alterados e retorna as chaves efetivamente modificadas."""
    current = get_all()
    changed = []
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        if current.get(key) == value:
            continue
        row = db.session.get(SiteSetting, key)
        if row is None:
            row = SiteSetting(key=key)
            db.session.add(row)
        row.value = value
        row.updated_by_id = user_id
        changed.append(key)
    try:
        g.pop("_site_settings", None)
    except RuntimeError:
        pass
    return changed


def public_title() -> str:
    """Identificação profissional conforme regras de publicidade médica."""
    s = get_all()
    title = f"{s['doctor_name']} · {s['crm']}"
    if s.get("rqe") and s.get("rqe_confirmed"):
        title += f" · RQE {s['rqe']}"
    return title
