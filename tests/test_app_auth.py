"""Phase 3 security re-test: cookie sessions, CSRF, tenant-isolated settings, SSRF."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odrl_agent import db as db_module
from odrl_agent import web
from odrl_agent.app import create_app
from odrl_agent.auth import CSRF_HEADER, CSRF_VALUE, SESSION_COOKIE, SessionStore, fingerprint

REPO = Path(__file__).resolve().parent.parent
CSRF = {CSRF_HEADER: CSRF_VALUE}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("NL2ODRL_API_KEYS", "alpha, beta")
    monkeypatch.chdir(REPO)
    return TestClient(create_app(REPO, out_dir=tmp_path, sessions=SessionStore(ttl=60)))


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_login_sets_httponly_strict_cookie_and_never_echoes_key(client) -> None:
    resp = client.post("/api/session", json={"api_key": "alpha"})
    assert resp.status_code == 200
    assert resp.json() == {"key_fp": fingerprint("alpha"), "via": "cookie", "expires_in": 60}
    cookie = resp.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in cookie and "HttpOnly" in cookie
    assert "samesite=strict" in cookie.lower()
    assert "alpha" not in cookie
    assert client.get("/api/session").json() == {"key_fp": fingerprint("alpha"), "via": "cookie"}
    assert client.get("/api/runs").status_code == 200


def test_wrong_key_login_is_401(client) -> None:
    assert client.post("/api/session", json={"api_key": "nope"}).status_code == 401
    assert client.post("/api/session", json={}).status_code == 401


def test_cookie_post_without_csrf_header_is_refused(client) -> None:
    client.post("/api/session", json={"api_key": "alpha"})
    body = {"base_url": "https://models.example.com/v1"}
    assert client.post("/api/settings", json=body).status_code == 403
    assert client.post("/api/settings", json=body, headers=CSRF).status_code == 200


def test_bearer_post_needs_no_csrf_header(client) -> None:
    body = {"base_url": "https://models.example.com/v1"}
    assert client.post("/api/settings", json=body, headers=_bearer("alpha")).status_code == 200


def test_logout_revokes_the_session(client) -> None:
    client.post("/api/session", json={"api_key": "alpha"})
    assert client.delete("/api/session", headers=CSRF).status_code == 200
    assert client.get("/api/runs").status_code == 401


def test_revoked_key_kills_its_sessions(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("NL2ODRL_API_KEYS", raising=False)
    monkeypatch.chdir(REPO)
    http = TestClient(create_app(REPO, out_dir=tmp_path, sessions=SessionStore()))
    conn = db_module.connect(tmp_path / "agentcrash.db")
    try:
        web._ensure_saas_tables(conn)
        raw = web.issue_api_key(conn, "browser")
    finally:
        conn.close()
    assert http.post("/api/session", json={"api_key": raw}).status_code == 200
    assert http.get("/api/runs").status_code == 200
    conn = db_module.connect(tmp_path / "agentcrash.db")
    try:
        web.revoke_api_key(conn, web._fp(raw))
    finally:
        conn.close()
    assert http.get("/api/runs").status_code == 401


def test_expired_session_is_rejected() -> None:
    store = SessionStore(ttl=0)
    sid = store.create("abc")
    assert store.lookup(sid) is None


def test_cross_tenant_settings_write_is_impossible(client) -> None:
    alpha = {"base_url": "https://alpha-models.example.com/v1", "api_key": "alpha-model-key"}
    assert client.post("/api/settings", json=alpha, headers=_bearer("alpha")).status_code == 200
    mine = client.get("/api/settings", headers=_bearer("alpha")).json()
    theirs = client.get("/api/settings", headers=_bearer("beta")).json()
    assert mine["base_url"] == alpha["base_url"] and mine["api_key_set"] is True
    assert theirs["base_url"] != alpha["base_url"]
    assert "alpha-model-key" not in str(mine) + str(theirs)
    assert web.SETTINGS.config(fingerprint("beta")).base_url != alpha["base_url"]
    assert web.SETTINGS.config(fingerprint("alpha")).base_url == alpha["base_url"]


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://[fe80::1]/v1",
        "http://metadata.google.internal/v1",
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/",
    ],
)
def test_ssrf_payloads_are_rejected(client, url) -> None:
    resp = client.post("/api/settings", json={"base_url": url}, headers=_bearer("alpha"))
    assert resp.status_code == 400 and resp.json()["error"] == "E_CONFIG"
    status = client.get("/api/settings", headers=_bearer("alpha")).json()
    assert status["base_url"] != url
