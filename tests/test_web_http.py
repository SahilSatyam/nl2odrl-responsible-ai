"""HTTP-level round trip through the FastAPI app (scripted structured model)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
from scripted_llm import BASIC_IR, install

import odrl_agent.verifiers  # noqa: F401 -- import registers product verifiers
from odrl_agent import web
from odrl_agent.app import create_app
from odrl_agent.auth import SessionStore

REPO = Path(__file__).resolve().parent.parent
CONVERT = {"nl_text": "Allow the research team to use dataset-7.", "policy_variant": "naive",
           "seed": 42}  # fmt: skip


def fp(k: str) -> str:
    return hashlib.sha256(k.encode("utf-8")).hexdigest()[:16]


def _client(monkeypatch, tmp_path, replies=None) -> TestClient:
    install(monkeypatch, replies or [BASIC_IR])
    monkeypatch.chdir(REPO)
    app = create_app(REPO, out_dir=tmp_path, sessions=SessionStore())
    return TestClient(app)


def _auth(key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


def test_http_convert_roundtrip(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    response = client.post("/api/convert", json=CONVERT)
    assert response.status_code == 200
    started = response.json()
    assert started["status"] == "running"
    final = client.app.state.registry.wait(started["run_key"], timeout=30.0)
    assert final is not None and final["status"] == "done"
    assert final["verdict"]["verdict"] == "PASS"
    assert final["patch"] is None
    (save,) = final["saves"]
    assert save["valid"] is True
    via_http = client.get(f"/api/runs/{started['run_key']}").json()
    assert via_http["verdict"]["verdict"] == "PASS"
    assert via_http["events"] == final["events"]
    assert via_http["draft"]["auto_publish"] is True
    assert via_http["decision"] is None
    listed = client.get("/api/runs").json()
    assert any(r["run_key"] == started["run_key"] for r in listed["runs"])
    pairs = client.get("/api/odrl?valid=true").json()
    assert any(p["run_key"] == started["run_key"] for p in pairs["pairs"])
    assert all(e.get("injector_event") is None for e in final["events"])
    assert final["tenant"] == "local" and via_http["tenant"] == "local"
    usage = client.get("/api/usage").json()
    assert any(u["key_fp"] == "local" and u["runs"] >= 1 for u in usage["usage"])


def test_request_id_reaches_the_trace(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    response = client.post("/api/convert", json=CONVERT, headers={"X-Request-ID": "trace-me-0001"})
    assert response.headers["X-Request-ID"] == "trace-me-0001"
    final = client.app.state.registry.wait(response.json()["run_key"], timeout=30.0)
    start = next(e for e in final["events"] if e["detail"].get("phase") == "run_start")
    assert start["detail"]["request_id"] == "trace-me-0001"
    from odrl_agent.instrument import load_sidecar

    trace_path = next(p for p in (tmp_path / "traces").glob("*.jsonl") if ".sdk." not in p.name)
    link = next(e for e in load_sidecar(str(trace_path)) if e.get("tool") == "trace")
    assert link["inputs"]["request_id"] == "trace-me-0001"


def test_usage_scoped_to_caller(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NL2ODRL_API_KEYS", "alpha, beta")
    client = _client(monkeypatch, tmp_path)
    assert client.get("/api/usage").status_code == 401
    beta = client.get("/api/usage", headers=_auth("beta"))
    assert beta.status_code == 200
    assert beta.json()["usage"] == [{
        "key_fp": fp("beta"), "runs": 0, "model_calls": 0, "prompt_tokens": 0,
        "completion_tokens": 0, "total_tokens": 0,
    }]  # fmt: skip
    started = client.post("/api/convert", json=CONVERT, headers=_auth("alpha"))
    assert started.status_code == 200
    rk = started.json()["run_key"]
    final = client.app.state.registry.wait(rk, timeout=30.0)
    assert final is not None and final["status"] == "done"
    assert final["tenant"] == fp("alpha")
    (row,) = [r for r in client.get("/api/usage", headers=_auth("alpha")).json()["usage"]
              if r["key_fp"] == fp("alpha")]  # fmt: skip
    assert row["runs"] == 1
    usage = client.get("/api/usage", headers=_auth("beta")).json()["usage"]
    assert all(r["key_fp"] != fp("alpha") for r in usage)
    assert client.get(f"/api/runs/{rk}").status_code == 401
    assert client.get(f"/api/runs/{rk}", headers=_auth("beta")).status_code == 404
    assert client.get(f"/api/runs/{rk}/events", headers=_auth("beta")).status_code == 404
    snap = client.get(f"/api/runs/{rk}", headers=_auth("alpha"))
    assert snap.status_code == 200 and snap.json()["tenant"] == fp("alpha")
    listed = client.get("/api/runs", headers=_auth("beta")).json()["runs"]
    assert all(r["run_key"] != rk for r in listed)
    listed = client.get("/api/runs", headers=_auth("alpha")).json()["runs"]
    assert any(r["run_key"] == rk for r in listed)
    pairs = client.get("/api/odrl", headers=_auth("beta")).json()["pairs"]
    assert all(p["run_key"] != rk for p in pairs)
    pairs = client.get("/api/odrl", headers=_auth("alpha")).json()["pairs"]
    assert any(p["run_key"] == rk for p in pairs)


def test_issued_key_gates_http(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("NL2ODRL_API_KEYS", raising=False)
    client = _client(monkeypatch, tmp_path)
    from odrl_agent import db as db_module

    db_conn = db_module.connect(tmp_path / "agentcrash.db")
    try:
        raw = web.issue_api_key(db_conn, "http-tenant")
    finally:
        db_conn.close()
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/runs", headers=_auth("wrong")).status_code == 401
    assert client.get("/api/runs", headers=_auth(raw)).status_code == 200
    db_conn = db_module.connect(tmp_path / "agentcrash.db")
    try:
        assert web.revoke_api_key(db_conn, web._fp(raw)) == 1
    finally:
        db_conn.close()
    assert client.get("/api/runs", headers=_auth(raw)).status_code == 401


def test_api_key_gate_open_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("NL2ODRL_API_KEYS", raising=False)
    client = _client(monkeypatch, tmp_path)
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/runs").status_code == 200


def test_api_key_gate_enforced(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NL2ODRL_API_KEYS", "tenant-one, tenant-two")
    client = _client(monkeypatch, tmp_path)
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/runs").status_code == 401
    assert client.post("/api/convert", content=b"{}").status_code == 401
    assert client.get("/api/settings", headers=_auth("wrong")).status_code == 401
    assert client.get("/api/settings", headers=_auth("tenant-two")).status_code == 200
    assert client.get("/api/runs", headers=_auth(" tenant-one ")).status_code == 200
    assert client.get("/api/healthz").status_code == 200
    assert client.get("/api/nope", headers=_auth("tenant-one")).status_code == 404


def test_bad_bodies_are_400(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    assert client.post("/api/convert", content=b"").json()["error"] == "E_CONFIG"
    assert client.post("/api/convert", content=b"not json").status_code == 400
    assert client.post("/api/convert", content=b"x" * 70000).status_code == 400
    assert client.post("/api/convert", json={"nl_text": ""}).status_code == 400


def test_openapi_schema_is_generated(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/api/convert", "/api/runs/{run_key}/publish", "/api/drafts", "/api/session"):
        assert path in paths
