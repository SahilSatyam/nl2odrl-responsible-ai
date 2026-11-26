"""Phase 3 service hardening: bounded queue, honest 429, eviction, IDs, metrics, SSE."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from odrl_agent import web
from odrl_agent.observability import METRICS, Metrics, request_id, route_template

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def fake_runs(monkeypatch, tmp_path):
    """Replace spec building and execution with gated fakes (no model, no DB)."""
    gate = threading.Event()
    started: list[str] = []

    def build(nl_text, variant, seed, max_steps, root, attempt, out_dir):
        trace = tmp_path / f"t{attempt}.jsonl"
        trace.write_text("", encoding="utf-8")
        return {
            "run_id": f"r{attempt}", "scenario_id": "inline", "variant": variant,
            "seed": seed, "nl_text": nl_text, "trace_path": str(trace),
        }  # fmt: skip

    def run(spec, run_key):
        started.append(run_key)
        assert gate.wait(10)
        return {"run_key": run_key, "status": "done",
                "events": [], "usage_total": {"prompt_tokens": 3, "completion_tokens": 2},
                "model_calls": 1, "verdict": {"verdict": "PASS"}, "saves": []}  # fmt: skip

    monkeypatch.setattr(web, "build_inline_run", build)
    monkeypatch.setattr(web, "run_and_store", run)
    return gate, started


def _start(registry, n):
    return [registry.start("Members may read the report.", "naive", 1, 8) for _ in range(n)]


def test_more_than_two_runs_execute_concurrently(fake_runs, tmp_path) -> None:
    gate, started = fake_runs
    registry = web.RunRegistry(REPO, tmp_path, workers=4, queue_depth=0)
    records = _start(registry, 4)
    assert [r["status"] for r in records] == ["running"] * 4
    deadline = time.monotonic() + 5
    while len(started) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(started) == 4 and registry.active == 4
    gate.set()
    for r in records:
        assert registry.wait(r["run_key"], 5)["status"] == "done"
    assert registry.active == 0


def test_queue_then_honest_429_with_retry_after(fake_runs, tmp_path) -> None:
    gate, _ = fake_runs
    registry = web.RunRegistry(REPO, tmp_path, workers=1, queue_depth=2)
    records = _start(registry, 3)
    assert [r["status"] for r in records] == ["running", "queued", "queued"]
    assert registry.snapshot(records[1]["run_key"])["status"] == "queued"
    with pytest.raises(web.BusyError) as busy:
        _start(registry, 1)
    assert busy.value.retry_after >= 1
    gate.set()
    for r in records:  # queued runs drain FIFO on the same worker
        assert registry.wait(r["run_key"], 5)["status"] == "done"
    assert registry.active == 0 and registry.queued == 0


def test_finished_runs_are_evicted_by_count(fake_runs, tmp_path) -> None:
    gate, _ = fake_runs
    gate.set()
    registry = web.RunRegistry(REPO, tmp_path, workers=2, queue_depth=0, max_retained=3)
    for _ in range(8):
        (r,) = _start(registry, 1)
        registry.wait(r["run_key"], 5)
    assert registry.retained() <= 3


def test_finished_runs_are_evicted_by_ttl(fake_runs, tmp_path) -> None:
    gate, _ = fake_runs
    gate.set()
    registry = web.RunRegistry(REPO, tmp_path, workers=1, queue_depth=0, ttl=0.05)
    (r,) = _start(registry, 1)
    registry.wait(r["run_key"], 5)
    time.sleep(0.1)
    assert registry.retained() == 0


def test_request_id_reuse_and_mint() -> None:
    assert request_id("abcdef1234") == "abcdef1234"
    minted = request_id("bad id with spaces\r\nX-Injected: 1")
    assert len(minted) == 32 and "\n" not in minted


def test_route_template_bounds_label_cardinality() -> None:
    assert route_template("/api/runs/abc-1") == "/api/runs/{key}"
    assert route_template("/api/runs/abc-1/events") == "/api/runs/{key}/events"
    assert route_template("/index.html") == "/static"


def test_metrics_render_prometheus_text() -> None:
    m = Metrics()
    m.describe("x_total", "counter", "X.")
    m.describe("lat_seconds", "histogram", "L.")
    m.inc("x_total", route="/a")
    m.observe("lat_seconds", 0.5)
    text = m.render()
    assert '# TYPE x_total counter' in text and 'x_total{route="/a"} 1' in text
    assert "lat_seconds_sum 0.5" in text and "lat_seconds_count 1" in text
    assert '# TYPE lat_seconds histogram' in text
    assert 'lat_seconds_bucket{le="0.5"} 1' in text and 'lat_seconds_bucket{le="0.25"}' not in text
    assert 'lat_seconds_bucket{le="+Inf"} 1' in text
    assert m.quantile("lat_seconds", 0.95) == 0.5


def test_sustained_load_bounded_memory_and_queue_wait_slo(fake_runs, tmp_path) -> None:
    """200 conversions through 4 workers: never more than 4 active, memory capped,
    every accepted run finishes, and p95 queue wait stays inside the SLO."""
    from odrl_agent.observability import QUEUE_WAIT_P95_SLO_SECONDS

    gate, _ = fake_runs
    gate.set()
    METRICS.reset()
    registry = web.RunRegistry(REPO, tmp_path, workers=4, queue_depth=16, max_retained=20)
    accepted, rejected, peak = [], 0, 0
    for _ in range(200):
        try:
            accepted.append(registry.start("Members may read the report.", "naive", 1, 8)["run_key"])
        except web.BusyError as exc:
            rejected += 1
            assert exc.retry_after >= 1
            time.sleep(0.005)
        peak = max(peak, registry.active)
        assert registry.retained() <= 20 + 4 + 16  # finished cap + running + waiting
    deadline = time.monotonic() + 30
    while (registry.active or registry.queued) and time.monotonic() < deadline:
        time.sleep(0.01)
    # Finished runs beyond the retention cap are evicted from memory (served from
    # SQLite in production), so completion is counted, not looked up.
    assert METRICS.value("nl2odrl_runs_finished_total", status="done") == len(accepted)
    assert peak <= 4 and registry.active == 0 and registry.queued == 0
    assert registry.retained() <= 20
    assert len(accepted) + rejected == 200 and len(accepted) > 2
    p95 = METRICS.quantile("nl2odrl_queue_wait_seconds", 0.95)
    assert p95 is not None and p95 <= QUEUE_WAIT_P95_SLO_SECONDS


# ---- over HTTP --------------------------------------------------------------


@pytest.fixture
def client(fake_runs, tmp_path):
    from fastapi.testclient import TestClient

    from odrl_agent.app import create_app

    registry = web.RunRegistry(REPO, tmp_path, workers=1, queue_depth=0)
    app = create_app(REPO, out_dir=tmp_path, registry=registry)
    yield TestClient(app), fake_runs[0]
    fake_runs[0].set()


BODY = {"nl_text": "Members may read the report.", "policy_variant": "naive", "seed": 1}


def test_http_429_carries_retry_after_and_request_id(client) -> None:
    http, gate = client
    first = http.post("/api/convert", json=BODY, headers={"X-Request-ID": "client-req-0001"})
    assert first.status_code == 200
    assert first.headers["X-Request-ID"] == "client-req-0001"
    busy = http.post("/api/convert", json=BODY)
    assert busy.status_code == 429
    assert int(busy.headers["Retry-After"]) >= 1
    assert busy.json()["error"] == "E_BUSY"
    assert len(busy.headers["X-Request-ID"]) == 32


def test_http_sse_streams_status_then_end(client) -> None:
    http, gate = client
    run_key = http.post("/api/convert", json=BODY).json()["run_key"]
    threading.Timer(0.3, gate.set).start()
    with http.stream("GET", f"/api/runs/{run_key}/events") as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        text = "".join(stream.iter_text())
    events = [line[len("event: "):] for line in text.splitlines() if line.startswith("event: ")]
    assert events[0] == "status" and events[-1] == "end"
    end = json.loads(text.strip().splitlines()[-1][len("data: "):])
    assert end["status"] == "done"


def test_http_sse_unknown_run_is_404(client) -> None:
    http, _ = client
    assert http.get("/api/runs/nope-1/events").status_code == 404


def test_http_metrics_endpoint(client) -> None:
    http, gate = client
    gate.set()
    assert http.post("/api/convert", json=BODY).status_code == 200
    text = http.get("/api/metrics").text
    assert "nl2odrl_http_requests_total" in text
    assert "nl2odrl_runs_active" in text
    assert METRICS.value("nl2odrl_http_requests_total", method="POST",
                         route="/api/convert", code="200") >= 1  # fmt: skip
