"""Páginas públicas, SEO, navegação e cabeçalhos de segurança."""
import pytest

from app.extensions import db
from app.services import settings as site_settings


@pytest.mark.parametrize("url", ["/", "/sobre", "/atendimentos", "/contato", "/privacidade", "/termos",
                                 "/agendar/", "/agendar/consultar"])
def test_public_pages_load(client, url):
    r = client.get(url)
    assert r.status_code == 200
    assert "text/html" in r.content_type


def test_home_content_and_professional_identification(client):
    html = client.get("/").get_data(as_text=True)
    assert "Dra. Juliana Gonçalves Rosa" in html
    assert "CRM-SP 238.870" in html
    assert "Cuidado próximo, contínuo e integral para cada fase da vida." in html
    assert "Agendar consulta" in html and "Conheça a médica" in html
    assert "UNICAMP" in html
    assert "RQE" not in html  # especialista só após confirmação do RQE


def test_navigation_links(client):
    html = client.get("/").get_data(as_text=True)
    for href in ['href="/sobre"', 'href="/atendimentos"', 'href="/contato"', 'href="/agendar/"',
                 'href="/privacidade"', 'href="/termos"']:
        assert href in html
    for url in ["/sobre", "/atendimentos", "/contato"]:
        assert client.get(url).status_code == 200


def test_rqe_only_shown_when_confirmed(client, app):
    with app.app_context():
        site_settings.set_many({"rqe": "12345", "rqe_confirmed": False})
        db.session.commit()
    assert "RQE 12345" not in client.get("/").get_data(as_text=True)
    with app.app_context():
        site_settings.set_many({"rqe_confirmed": True})
        db.session.commit()
    assert "RQE 12345" in client.get("/").get_data(as_text=True)


def test_no_fictitious_contact_data(client):
    html = client.get("/contato").get_data(as_text=True)
    assert "serão divulgados em breve" in html
    assert "tel:" not in html


def test_seo_files(client):
    robots = client.get("/robots.txt").get_data(as_text=True)
    assert "Disallow: /painel/" in robots and "Sitemap:" in robots
    sm = client.get("/sitemap.xml")
    assert sm.status_code == 200 and b"/sobre" in sm.data and b"/painel" not in sm.data
    html = client.get("/").get_data(as_text=True)
    assert 'property="og:title"' in html and 'application/ld+json' in html
    assert '"@type": "Physician"' in html
    assert "<meta name=\"description\"" in html


def test_security_headers(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "Referrer-Policy" in r.headers


def test_health_endpoint_does_not_leak(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json() == {"status": "ok"}
