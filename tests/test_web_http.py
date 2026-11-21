"""HTTP-level round trip through the real handler (scripted structured model)."""

from __future__ import annotations

import http.server
import json
import threading
import urllib.request
from pathlib import Path

from scripted_llm import BASIC_IR, install

import odrl_agent.verifiers  # noqa: F401 -- import registers product verifiers

REPO = Path(__file__).resolve().parent.parent

def _server(monkeypatch, tmp_path):
    real_urlopen = urllib.request.urlopen
    install(monkeypatch, [BASIC_IR])

    from odrl_agent import web

    registry = web.RunRegistry(REPO, out_dir=tmp_path)
    web.ConvertHandler.registry = registry
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), web.ConvertHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, real_urlopen


def test_http_convert_roundtrip(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(REPO)
    server, port, real_urlopen = _server(monkeypatch, tmp_path)
    try:
        payload = json.dumps(
            {
                "nl_text": "Allow the research team to use dataset-7.",
                "policy_variant": "naive",
                "seed": 42,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/convert", data=payload, method="POST"
        )
        with real_urlopen(request, timeout=10) as response:
            assert response.status == 200
            started = json.loads(response.read().decode("utf-8"))
        assert started["status"] == "running"

        from odrl_agent import web

        final = web.ConvertHandler.registry.wait(started["run_key"], timeout=30.0)
        assert final is not None and final["status"] == "done"
        assert final["verdict"]["verdict"] == "PASS"
        assert final["patch"] is None
        (save,) = final["saves"]
        assert save["valid"] is True

        with real_urlopen(
            f"http://127.0.0.1:{port}/api/runs/{started['run_key']}", timeout=30
        ) as response:
            via_http = json.loads(response.read().decode("utf-8"))
        assert via_http["verdict"]["verdict"] == "PASS"
        assert via_http["events"] == final["events"]

        with real_urlopen(f"http://127.0.0.1:{port}/api/runs", timeout=10) as response:
            listed = json.loads(response.read().decode("utf-8"))
        assert any(r["run_key"] == started["run_key"] for r in listed["runs"])

        with real_urlopen(f"http://127.0.0.1:{port}/api/odrl?valid=true", timeout=10) as response:
            pairs = json.loads(response.read().decode("utf-8"))
        assert any(p["run_key"] == started["run_key"] for p in pairs["pairs"])
        assert all(e.get("injector_event") is None for e in final["events"])
        assert final["tenant"] == "local"
        assert via_http["tenant"] == "local"

        with real_urlopen(f"http://127.0.0.1:{port}/api/usage", timeout=10) as response:
            usage = json.loads(response.read().decode("utf-8"))
        assert any(
            u["key_fp"] == "local" and u["runs"] >= 1 for u in usage["usage"]
        )
    finally:
        from odrl_agent import web

        server.shutdown()
        server.server_close()
        web.ConvertHandler.registry = None


def _status(monkeypatch, tmp_path, path, method="GET", data=None, key=None):
    """One HTTP call against a fresh server; returns the status code."""
    import http.client

    monkeypatch.chdir(REPO)
    server, port, _real_urlopen = _server(monkeypatch, tmp_path)
    try:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=data, headers=headers)
            return conn.getresponse().status
        finally:
            conn.close()
    finally:
        from odrl_agent import web

        server.shutdown()
        server.server_close()
        web.ConvertHandler.registry = None


def test_usage_scoped_to_caller(monkeypatch, tmp_path) -> None:
    import hashlib
    import http.client

    def fp(k: str) -> str:
        return hashlib.sha256(k.encode("utf-8")).hexdigest()[:16]

    monkeypatch.setenv("NL2ODRL_API_KEYS", "alpha, beta")
    monkeypatch.chdir(REPO)
    server, port, _real_urlopen = _server(monkeypatch, tmp_path)
    try:
        from odrl_agent import web

        def call(method, path, payload=None, key=None):
            headers = {}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read().decode("utf-8"))
            finally:
                conn.close()

        assert call("GET", "/api/usage")[0] == 401
        code, beta = call("GET", "/api/usage", key="beta")
        assert code == 200
        assert beta["usage"] == [
            {
                "key_fp": fp("beta"),
                "runs": 0,
                "model_calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        ]
        code, started = call(
            "POST",
            "/api/convert",
            {
                "nl_text": "Allow the research team to use dataset-7.",
                "policy_variant": "naive",
                "seed": 42,
            },
            key="alpha",
        )
        assert code == 200
        final = web.ConvertHandler.registry.wait(started["run_key"], timeout=30.0)
        assert final is not None and final["status"] == "done"
        assert final["tenant"] == fp("alpha")
        code, usage = call("GET", "/api/usage", key="alpha")
        assert code == 200
        (row,) = [r for r in usage["usage"] if r["key_fp"] == fp("alpha")]
        assert row["runs"] == 1
        code, usage = call("GET", "/api/usage", key="beta")
        assert code == 200
        assert all(r["key_fp"] != fp("alpha") for r in usage["usage"])
        rk = started["run_key"]
        assert call("GET", f"/api/runs/{rk}")[0] == 401
        assert call("GET", f"/api/runs/{rk}", key="beta")[0] == 404
        code, snap = call("GET", f"/api/runs/{rk}", key="alpha")
        assert code == 200 and snap["tenant"] == fp("alpha")
        code, listed = call("GET", "/api/runs", key="beta")
        assert code == 200
        assert all(r["run_key"] != rk for r in listed["runs"])
        code, listed = call("GET", "/api/runs", key="alpha")
        assert code == 200 and any(r["run_key"] == rk for r in listed["runs"])
        code, pairs = call("GET", "/api/odrl", key="beta")
        assert code == 200
        assert all(p["run_key"] != rk for p in pairs["pairs"])
        code, pairs = call("GET", "/api/odrl", key="alpha")
        assert code == 200 and any(p["run_key"] == rk for p in pairs["pairs"])
    finally:
        from odrl_agent import web

        server.shutdown()
        server.server_close()
        web.ConvertHandler.registry = None


def test_issued_key_gates_http(monkeypatch, tmp_path) -> None:
    import http.client

    monkeypatch.delenv("NL2ODRL_API_KEYS", raising=False)
    monkeypatch.chdir(REPO)
    server, port, _real_urlopen = _server(monkeypatch, tmp_path)
    try:
        from odrl_agent import db as db_module
        from odrl_agent import web

        db_conn = db_module.connect(tmp_path / "agentcrash.db")
        try:
            raw = web.issue_api_key(db_conn, "http-tenant")
        finally:
            db_conn.close()

        def get(path, key=None):
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            http_conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                http_conn.request("GET", path, headers=headers)
                return http_conn.getresponse().status
            finally:
                http_conn.close()

        assert get("/api/runs") == 401
        assert get("/api/runs", key="wrong") == 401
        assert get("/api/runs", key=raw) == 200
        db_conn = db_module.connect(tmp_path / "agentcrash.db")
        try:
            assert web.revoke_api_key(db_conn, web._fp(raw)) == 1
        finally:
            db_conn.close()
        assert get("/api/runs", key=raw) == 401
    finally:
        from odrl_agent import web

        server.shutdown()
        server.server_close()
        web.ConvertHandler.registry = None


def test_api_key_gate_open_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("NL2ODRL_API_KEYS", raising=False)
    assert _status(monkeypatch, tmp_path, "/api/settings") == 200
    assert _status(monkeypatch, tmp_path, "/api/runs") == 200


def test_api_key_gate_enforced(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NL2ODRL_API_KEYS", "tenant-one, tenant-two")
    assert _status(monkeypatch, tmp_path, "/api/settings") == 401
    assert _status(monkeypatch, tmp_path, "/api/runs") == 401
    assert _status(monkeypatch, tmp_path, "/api/convert", method="POST", data=b"{}") == 401
    assert _status(monkeypatch, tmp_path, "/api/settings", key="wrong") == 401
    assert _status(monkeypatch, tmp_path, "/api/settings", key="tenant-two") == 200
    assert _status(monkeypatch, tmp_path, "/api/runs", key=" tenant-one ") == 200
