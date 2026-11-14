"""Páginas públicas, SEO e mídia institucional."""
from __future__ import annotations

import sqlalchemy as sa
from flask import Blueprint, Response, abort, render_template, request, url_for

from ..extensions import db
from ..models import MediaAsset, Service
from ..services import settings as site_settings

bp = Blueprint("public", __name__)

# Conteúdo educativo sobre a Medicina de Família e Comunidade (não constitui oferta de serviço).
CARE_AREAS = [
    ("heart-pulse", "Consultas em Medicina de Família e Comunidade",
     "Atenção à saúde da pessoa como um todo, considerando sua história, seu contexto familiar e social."),
    ("activity", "Acompanhamento de condições crônicas",
     "Seguimento contínuo de condições de longa duração, com planos de cuidado construídos em conjunto."),
    ("shield-check", "Cuidados preventivos",
     "Rastreamentos e orientações indicados para cada fase da vida, com base em evidências."),
    ("flower1", "Promoção da saúde",
     "Apoio para fortalecer hábitos e escolhas que favoreçam o bem-estar no dia a dia."),
    ("person-hearts", "Saúde da mulher e do homem",
     "Cuidados específicos de cada pessoa, com escuta respeitosa e informação clara."),
    ("people", "Acompanhamento de adultos e idosos",
     "Atenção às necessidades que mudam ao longo da vida, com foco em autonomia e qualidade de vida."),
    ("sun", "Orientações sobre hábitos saudáveis",
     "Alimentação, sono, atividade física e saúde emocional discutidos de forma prática e realista."),
    ("tree", "Cuidado integral e longitudinal",
     "Um vínculo que se mantém ao longo do tempo e coordena o cuidado com outros profissionais."),
]

FAQ = [
    ("O que é Medicina de Família e Comunidade?",
     "É a área da medicina dedicada ao cuidado integral e contínuo das pessoas, em todas as fases da vida. "
     "Aborda a maior parte das necessidades de saúde e coordena o cuidado com outros profissionais quando necessário."),
    ("Como funciona o agendamento online?",
     "Você escolhe o tipo de atendimento, a data e um horário disponível e informa alguns dados de contato. "
     "A solicitação é registrada e, em seguida, confirmada pela equipe."),
    ("Preciso informar sintomas ou exames no agendamento?",
     "Não. O formulário de agendamento solicita apenas dados de contato. Informações de saúde são tratadas "
     "exclusivamente durante a consulta."),
    ("Posso cancelar ou consultar meu agendamento?",
     "Sim. Guarde o código de referência exibido ao final da solicitação. Com ele e o telefone informado, "
     "você pode consultar ou cancelar o agendamento dentro do prazo indicado."),
    ("Como meus dados são protegidos?",
     "Os dados são utilizados apenas para a organização do atendimento, com acesso restrito à equipe autorizada. "
     "Veja os detalhes na Política de Privacidade."),
]


@bp.route("/")
def index():
    services = db.session.execute(
        sa.select(Service).where(Service.active.is_(True)).order_by(Service.position, Service.name).limit(6)
    ).scalars().all()
    return render_template("public/index.html", care_areas=CARE_AREAS[:6], faq=FAQ, services=services)


@bp.route("/sobre")
def about():
    return render_template("public/about.html")


@bp.route("/atendimentos")
def services():
    items = db.session.execute(
        sa.select(Service).where(Service.active.is_(True)).order_by(Service.position, Service.name)
    ).scalars().all()
    return render_template("public/services.html", services=items, care_areas=CARE_AREAS)


@bp.route("/contato")
def contact():
    return render_template("public/contact.html")


@bp.route("/privacidade")
def privacy():
    return render_template("public/privacy.html")


@bp.route("/termos")
def terms():
    return render_template("public/terms.html")


@bp.route("/media/foto-medica")
def doctor_photo():
    asset = db.session.execute(sa.select(MediaAsset).where(MediaAsset.key == "doctor_photo")).scalar_one_or_none()
    if not asset:
        abort(404)
    etag = asset.sha256[:32]
    if request.if_none_match and etag in request.if_none_match:
        return Response(status=304)
    resp = Response(asset.data, mimetype=asset.content_type)
    resp.set_etag(etag)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@bp.route("/robots.txt")
def robots():
    lines = [
        "User-agent: *",
        "Disallow: /painel/",
        "Disallow: /acesso/",
        "Disallow: /agendar/api/",
        "Disallow: /agendar/agendamento/",
        "Disallow: /agendar/consultar",
        f"Sitemap: {url_for('public.sitemap', _external=True)}",
    ]
    return Response("\n".join(lines) + "\n", mimetype="text/plain")


@bp.route("/sitemap.xml")
def sitemap():
    pages = ["public.index", "public.about", "public.services", "public.contact", "booking.start",
             "public.privacy", "public.terms"]
    urls = "".join(f"<url><loc>{url_for(p, _external=True)}</loc></url>" for p in pages)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, mimetype="application/xml")


def structured_data() -> dict:
    """JSON-LD com apenas atributos confirmados (sem inventar especialidade, endereço ou telefone)."""
    s = site_settings.get_all()
    data = {
        "@context": "https://schema.org",
        "@type": "Physician",
        "name": s["doctor_name"],
        "url": url_for("public.index", _external=True),
        "identifier": s["crm"],
    }
    if s.get("address"):
        data["address"] = {"@type": "PostalAddress", "streetAddress": s["address"],
                           **({"addressLocality": s["city"]} if s.get("city") else {})}
    if s.get("phone"):
        data["telephone"] = s["phone"]
    return data
