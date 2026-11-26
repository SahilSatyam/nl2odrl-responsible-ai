"""FastAPI service for NL2ODRL Studio (Phase 3; served by Uvicorn).

Local single-user server: every ``/api/*`` route is open (no login, no
API keys). The model endpoint key is configured via ``/api/settings`` and
lives in server memory only.

Endpoints (all JSON unless noted):

- ``POST /api/convert`` — queue a conversion; ``429 + Retry-After`` when full.
- ``GET /api/runs``, ``GET /api/runs/{key}``, ``GET /api/runs/{key}/events``
  (Server-Sent Events: ``status`` / ``trace`` / ``end``).
- ``GET /api/drafts``, ``POST /api/runs/{key}/publish`` — HITL review queue
  and Approve & Publish / edit / reject, one decision per run.
- ``GET /api/odrl``, ``GET /api/usage`` — published policies, run/token totals.
- ``GET|POST /api/settings`` — the model endpoint (never echoes the key).
- ``GET /api/metrics`` — Prometheus text; ``GET /api/healthz`` — liveness.
- ``/`` — the built React studio (``ui/``), when present.

Every response carries ``X-Request-ID``; each request emits one JSON log
line, and the id is written into the conversion trace's ``run_start``.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from odrl_agent import publish as publish_module
from odrl_agent import web
from odrl_agent.observability import (
    METRICS,
    REQUEST_ID_HEADER,
    log_event,
    route_template,
)
from odrl_agent.observability import (
    request_id as make_request_id,
)

MAX_BODY = 65536
METRICS.describe("nl2odrl_drafts_decided_total", "counter", "Reviewer decisions, by decision.")


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, hint: str, *, headers: dict[str, str] | None = None,
        detail: Any = None,
    ) -> None:  # fmt: skip
        super().__init__(hint)
        self.status = status
        self.code = code
        self.hint = hint
        self.headers = headers or {}
        self.detail = detail


def create_app(
    root: Path,
    *,
    ui_dir: Path | None = None,
    out_dir: Path | None = None,
    registry: web.RunRegistry | None = None,
) -> FastAPI:
    """Build the app. ``root`` is the checkout (scenarios/, policies/, schemas/)."""
    app = FastAPI(
        title="NL2ODRL Studio API",
        version="2.0.0",
        description="Natural-language access rules to W3C ODRL 2.2 with a human publish gate.",
    )
    app.state.root = root
    app.state.out_dir = out_dir
    app.state.registry = registry if registry is not None else web.RunRegistry(root, out_dir)

    def db() -> Any:
        return web._db_conn(root, out_dir)

    # ---- errors, request ids, logs, metrics -----------------------------------
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        body: dict[str, Any] = {"error": exc.code, "hint": exc.hint}
        if exc.detail is not None:
            body["detail"] = exc.detail
        return JSONResponse(body, status_code=exc.status, headers=exc.headers)

    @app.middleware("http")
    async def _observe(request: Request, call_next: Any) -> Response:
        rid = make_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = rid
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", None) or route_template(request.url.path)
            seconds = time.perf_counter() - started
            METRICS.inc("nl2odrl_http_requests_total", method=request.method,
                        route=template, code=str(status))  # fmt: skip
            METRICS.observe("nl2odrl_http_request_duration_seconds", seconds, route=template)
            log_event(
                "http_request", request_id=rid, method=request.method, route=template,
                status=status, ms=round(seconds * 1000, 2),
            )  # fmt: skip
        response.headers[REQUEST_ID_HEADER] = rid
        return response

    async def json_body(request: Request) -> Any:
        raw = await request.body()
        if not raw or len(raw) > MAX_BODY:
            raise ApiError(400, "E_CONFIG", f"body must be 1..{MAX_BODY} bytes")
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "E_CONFIG", "body must be JSON") from exc

    def own_snapshot(run_key: str) -> dict[str, Any]:
        snap = app.state.registry.snapshot(run_key)
        if snap is None:
            raise ApiError(404, "E_CONFIG", "unknown run_key")
        return snap

    # ---- health -----------------------------------------------------------------
    @app.get("/api/healthz")
    def healthz() -> dict[str, Any]:
        reg = app.state.registry
        return {"ok": True, "active": reg.active, "queued": reg.queued, "workers": reg.workers}

    # ---- settings -----------------------------------------------------------------
    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return web.model_status()

    @app.post("/api/settings")
    async def post_settings(request: Request) -> Any:
        body = await json_body(request)
        try:
            return web.apply_model_settings(body)
        except web.ConvertError as exc:
            raise ApiError(400, "E_CONFIG", str(exc)) from exc

    # ---- conversions ----------------------------------------------------------------
    @app.post("/api/convert")
    async def convert(request: Request) -> Any:
        body = await json_body(request)
        try:
            nl_text, variant, seed, max_steps = web.validate_convert(body)
        except web.ConvertError as exc:
            raise ApiError(400, "E_CONFIG", str(exc)) from exc
        try:
            return await run_in_threadpool(
                app.state.registry.start, nl_text, variant, seed, max_steps,
                request_id=request.state.request_id,
            )  # fmt: skip
        except web.BusyError as exc:
            raise ApiError(
                429, "E_BUSY", str(exc), headers={"Retry-After": str(exc.retry_after)},
                detail={"retry_after": exc.retry_after},
            ) from exc  # fmt: skip

    @app.get("/api/runs")
    def runs(request: Request) -> dict[str, Any]:
        flags = web._query_flags(_query(request))
        conn = db()
        try:
            return {"runs": web._list_runs(conn, flags)}
        finally:
            conn.close()

    @app.get("/api/runs/{run_key}")
    def run(run_key: str) -> dict[str, Any]:
        snap = dict(own_snapshot(run_key))
        if snap.get("draft") is None:
            snap["draft"] = web._draft_from(snap.get("events") or [])
        if snap.get("status") not in ("running", "queued"):
            conn = db()
            try:
                snap["decision"] = publish_module.decision_for(conn, run_key)
            finally:
                conn.close()
        return snap

    @app.get("/api/runs/{run_key}/events")
    async def run_events(request: Request, run_key: str) -> StreamingResponse:
        await run_in_threadpool(own_snapshot, run_key)
        registry = app.state.registry

        def frame(event: str, data: Any) -> str:
            return f"event: {event}\ndata: {json.dumps(data, sort_keys=True, default=str)}\n\n"

        async def stream() -> Any:
            sent, status = 0, None
            began = last_beat = time.monotonic()
            while True:
                snap = await run_in_threadpool(registry.snapshot, run_key) or {}
                if snap.get("status") != status:
                    status = snap.get("status")
                    yield frame("status", {"run_key": run_key, "status": status})
                events = snap.get("events") or []
                for event in events[sent:]:
                    yield frame("trace", event)
                sent = max(sent, len(events))
                if status not in ("running", "queued"):
                    yield frame("end", {
                        "run_key": run_key, "status": status, "verdict": snap.get("verdict"),
                        "saves": snap.get("saves"),
                        "draft": snap.get("draft") or web._draft_from(events),
                    })  # fmt: skip
                    return
                now = time.monotonic()
                if now - began > web.SSE_MAX_SECONDS:
                    yield frame("timeout", {"run_key": run_key, "status": status})
                    return
                if now - last_beat > web.SSE_HEARTBEAT_SECONDS:
                    yield ": keep-alive\n\n"
                    last_beat = now
                if await request.is_disconnected():
                    return
                await asyncio.sleep(web.SSE_POLL_SECONDS)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )  # fmt: skip

    # ---- review queue + publish -----------------------------------------------------
    @app.get("/api/drafts")
    def drafts(request: Request) -> dict[str, Any]:
        q = request.query_params
        include = (q.get("include_decided") or "").lower() in ("1", "true", "yes")
        try:
            limit = int(q.get("limit") or 100)
        except ValueError:
            limit = 100
        conn = db()
        try:
            rows = publish_module.list_drafts(conn, include_decided=include,
                                              limit=limit)  # fmt: skip
        finally:
            conn.close()
        return {"drafts": rows}

    @app.post("/api/runs/{run_key}/publish")
    async def publish(request: Request, run_key: str) -> dict[str, Any]:
        body = await json_body(request)
        if not isinstance(body, dict):
            raise ApiError(400, "E_CONFIG", "request body must be a JSON object")
        snap = await run_in_threadpool(own_snapshot, run_key)
        if snap.get("status") in ("running", "queued"):
            raise ApiError(409, "E_STATE", "run is still converting")

        def _decide() -> dict[str, Any]:
            conn = db()
            try:
                return publish_module.decide(
                    conn, run_key=run_key, events=snap.get("events") or [],
                    decision=str(body.get("decision", "")), reason=body.get("reason", ""),
                    edited_ir=body.get("ir"),
                )  # fmt: skip
            finally:
                conn.close()

        try:
            result = await run_in_threadpool(_decide)
        except publish_module.PublishError as exc:
            raise ApiError(exc.status, exc.code, exc.hint, detail=exc.detail) from exc
        METRICS.inc("nl2odrl_drafts_decided_total", decision=result.get("decision", "?"))
        log_event("draft_decision", request_id=request.state.request_id, run_key=run_key,
                  decision=result.get("decision"), edited=result.get("edited"))  # fmt: skip
        return result

    # ---- policies, usage, metrics ---------------------------------------------------
    @app.get("/api/odrl")
    def odrl(request: Request) -> dict[str, Any]:
        flags = web._query_flags(_query(request))
        conn = db()
        try:
            return {"pairs": web._list_pairs(conn, flags)}
        finally:
            conn.close()

    @app.get("/api/usage")
    def usage() -> dict[str, Any]:
        conn = db()
        try:
            return {"usage": web._usage_rows(conn)}
        finally:
            conn.close()

    @app.get("/api/metrics")
    def metrics() -> PlainTextResponse:
        METRICS.set("nl2odrl_runs_retained", app.state.registry.retained())
        return PlainTextResponse(METRICS.render(),
                                 media_type="text/plain; version=0.0.4; charset=utf-8")  # fmt: skip

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def unknown(rest: str) -> None:
        del rest
        raise ApiError(404, "E_CONFIG", "unknown endpoint")

    if ui_dir is not None and ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")
    return app


def _query(request: Request) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        out.setdefault(key, []).append(value)
    return out
