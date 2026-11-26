"""HTTP-level round trip through the FastAPI app (scripted structured model)."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from scripted_llm import BASIC_IR, install

import odrl_agent.verifiers  # noqa: F401 -- import registers product verifiers
from odrl_agent.app import create_app

REPO = Path(__file__).resolve().parent.parent
CONVERT = {"nl_text": "Allow the research team to use dataset-7.", "policy_variant": "naive",
           "seed": 42}  # fmt: skip


def _client(monkeypatch, tmp_path, replies=None) -> TestClient:
    install(monkeypatch, replies or [BASIC_IR])
    monkeypatch.chdir(REPO)
    app = create_app(REPO, out_dir=tmp_path)
    return TestClient(app)


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
    usage = client.get("/api/usage").json()
    assert sum(u["runs"] for u in usage["usage"]) >= 1


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


def test_endpoints_are_open_without_keys(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/healthz").status_code == 200
    assert client.get("/api/nope").status_code == 404


def test_bad_bodies_are_400(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    assert client.post("/api/convert", content=b"").json()["error"] == "E_CONFIG"
    assert client.post("/api/convert", content=b"not json").status_code == 400
    assert client.post("/api/convert", content=b"x" * 70000).status_code == 400
    assert client.post("/api/convert", json={"nl_text": ""}).status_code == 400


def test_openapi_schema_is_generated(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/api/convert", "/api/runs/{run_key}/publish", "/api/drafts", "/api/settings"):
        assert path in paths
