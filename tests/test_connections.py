"""
Smoke tests: database engine connectivity, FastAPI app, and CORS middleware.

Run from project root: pytest tests/ -v
Requires DATABASE_URL and SECRET_KEY in the environment (e.g. via .env).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.fixture(scope="module")
def client():
    """Loads app (runs startup: create_tables)."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_database_engine_select_one():
    from app.core.database import engine

    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1


def test_engine_is_postgresql_with_psycopg2_when_url_is_postgres():
    from app.core.database import DATABASE_URL, engine

    if not DATABASE_URL.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not PostgreSQL in this environment")
    assert engine.url.drivername == "postgresql+psycopg2"


def test_openapi_schema(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    body = r.json()
    assert "openapi" in body
    assert "paths" in body


def test_root_or_static_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "").lower()


def test_root_portal_scripts_alias_served(client):
    """Backward-compat root URLs (optional); HTML prefers /static/*."""
    for path in ("/config.js", "/api-runtime.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "javascript" in r.headers.get("content-type", "").lower()


def test_static_portal_scripts_served(client):
    """Portal HTML loads config from StaticFiles mount — works without root aliases."""
    for path in ("/static/config.js", "/static/api-runtime.js"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_portal_features_deploy_marker(client):
    r = client.get("/api/portal-features")
    assert r.status_code == 200
    body = r.json()
    assert body.get("marker") == "portal-shell-2026-05-09"


def test_cors_preflight_get_docs(client):
    r = client.options(
        "/api/docs",
        headers={
            "Origin": "http://localhost:8000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code in (200, 204)
    allow = {k.lower(): v for k, v in r.headers.items()}
    assert "access-control-allow-origin" in allow
