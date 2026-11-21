"""FastAPI service for NL2ODRL Studio (Phase 3; served by Uvicorn).

Endpoints (all JSON unless noted; every ``/api/*`` route except
``/api/healthz`` and ``POST /api/session`` needs a caller once any API key
exists — Bearer key or session cookie, see :mod:`odrl_agent.auth`):

- ``POST /api/session`` / ``GET`` / ``DELETE`` — cookie login, whoami, logout.
- ``POST /api/convert`` — queue a conversion; ``429 + Retry-After`` when full.
- ``GET /api/runs``, ``GET /api/runs/{key}``, ``GET /api/runs/{key}/events``
  (Server-Sent Events: ``status`` / ``trace`` / ``end``).
- ``GET /api/drafts``, ``POST /api/runs/{key}/publish`` — HITL review queue
  and Approve & Publish / edit / reject, one decision per run.
- ``GET /api/odrl``, ``GET /api/usage`` — published policies, per-key usage.
- ``GET|POST /api/settings`` — this tenant's model endpoint (never echoed key).
- ``GET /api/metrics`` — Prometheus text; ``GET /api/healthz`` — liveness.
- ``/`` — the built React studio (``ui/``), when present.

Every response carries ``X-Request-ID``; each request emits one JSON log
line, and the id is written into the conversion trace's ``run_start``.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from odrl_agent import publish as publish_module
from odrl_agent import web
from odrl_agent.auth import (
    SESSION_COOKIE,
    SESSIONS,
    Principal,
    SessionStore,
    authenticate,
    csrf_ok,
    fingerprint,
    key_is_valid,
)
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
    sessions: SessionStore | None = None,
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
    # SessionStore defines __len__, so an empty store is falsy: test identity, not truth.
    app.state.sessions = sessions if sessions is not None else SESSIONS

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
            principal = getattr(request.state, "principal", None)
            log_event(
                "http_request", request_id=rid, method=request.method, route=template,
                status=status, ms=round(seconds * 1000, 2),
                tenant=(principal.fp[:12] if principal else "-"),
            )  # fmt: skip
        response.headers[REQUEST_ID_HEADER] = rid
        return response

    # ---- caller identity --------------------------------------------------------
    def principal(request: Request) -> Principal:
        conn = db()
        try:
            state = web.key_state(conn)
        finally:
            conn.close()
        who = authenticate(request.headers, request.cookies, state, app.state.sessions)
        if who is None:
            raise ApiError(401, "E_AUTH", "valid Bearer API key or session required")
        if not csrf_ok(request.method, who, request.headers):
            raise ApiError(403, "E_CSRF", "cookie sessions must send X-Requested-With: nl2odrl")
        request.state.principal = who
        return who

    async def json_body(request: Request) -> Any:
        raw = await request.body()
        if not raw or len(raw) > MAX_BODY:
            raise ApiError(400, "E_CONFIG", f"body must be 1..{MAX_BODY} bytes")
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "E_CONFIG", "body must be JSON") from exc

    def own_snapshot(run_key: str, who: Principal) -> dict[str, Any]:
        snap = app.state.registry.snapshot(run_key)
        if snap is None or (who.scope is not None and snap.get("tenant") != who.fp):
            raise ApiError(404, "E_CONFIG", "unknown run_key")
        return snap

    # ---- health + session ---------------------------------------------------------
    @app.get("/api/healthz")
    def healthz() -> dict[str, Any]:
        reg = app.state.registry
        return {"ok": True, "active": reg.active, "queued": reg.queued, "workers": reg.workers}

    @app.post("/api/session")
    async def login(request: Request) -> JSONResponse:
        body = await json_body(request)
        key = body.get("api_key") if isinstance(body, dict) else None
        conn = db()
        try:
            env_keys, valid, gate_on = web.key_state(conn)
        finally:
            conn.close()
        if not gate_on:
            return JSONResponse({"key_fp": "local", "via": "local", "expires_in": None})
        if not isinstance(key, str) or not key_is_valid(key.strip(), env_keys, valid):
            raise ApiError(401, "E_AUTH", "invalid API key")
        fp = fingerprint(key.strip())
        sessions: SessionStore = app.state.sessions
        response = JSONResponse({"key_fp": fp, "via": "cookie", "expires_in": sessions.ttl})
        response.set_cookie(
            SESSION_COOKIE, sessions.create(fp), max_age=sessions.ttl, path="/", httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https" or os.environ.get("NL2ODRL_COOKIE_SECURE") == "1",
        )  # fmt: skip
        log_event("session_created", request_id=request.state.request_id, tenant=fp[:12])
        return response

    @app.get("/api/session")
    def whoami(who: Principal = Depends(principal)) -> dict[str, Any]:
        return {"key_fp": who.fp, "via": who.via}

    @app.delete("/api/session")
    def logout(request: Request, who: Principal = Depends(principal)) -> JSONResponse:
        del who
        app.state.sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # ---- settings -----------------------------------------------------------------
    @app.get("/api/settings")
    def get_settings(who: Principal = Depends(principal)) -> dict[str, Any]:
        return web.model_status(who.tenant)

    @app.post("/api/settings")
    async def post_settings(request: Request, who: Principal = Depends(principal)) -> Any:
        body = await json_body(request)
        try:
            return web.apply_model_settings(body, who.tenant)
        except web.ConvertError as exc:
            raise ApiError(400, "E_CONFIG", str(exc)) from exc

    # ---- conversions ----------------------------------------------------------------
    @app.post("/api/convert")
    async def convert(request: Request, who: Principal = Depends(principal)) -> Any:
        body = await json_body(request)
        try:
            nl_text, variant, seed, max_steps = web.validate_convert(body)
        except web.ConvertError as exc:
            raise ApiError(400, "E_CONFIG", str(exc)) from exc
        try:
            return await run_in_threadpool(
                app.state.registry.start, nl_text, variant, seed, max_steps,
                tenant=who.tenant, request_id=request.state.request_id,
            )  # fmt: skip
        except web.BusyError as exc:
            raise ApiError(
                429, "E_BUSY", str(exc), headers={"Retry-After": str(exc.retry_after)},
                detail={"retry_after": exc.retry_after},
            ) from exc  # fmt: skip

    @app.get("/api/runs")
    def runs(request: Request, who: Principal = Depends(principal)) -> dict[str, Any]:
        flags = web._query_flags(_query(request))
        conn = db()
        try:
            return {"runs": web._list_runs(conn, flags, who.scope)}
        finally:
            conn.close()

    @app.get("/api/runs/{run_key}")
    def run(run_key: str, who: Principal = Depends(principal)) -> dict[str, Any]:
        snap = dict(own_snapshot(run_key, who))
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
    async def run_events(
        request: Request, run_key: str, who: Principal = Depends(principal)
    ) -> StreamingResponse:
        await run_in_threadpool(own_snapshot, run_key, who)
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
    def drafts(request: Request, who: Principal = Depends(principal)) -> dict[str, Any]:
        q = request.query_params
        include = (q.get("include_decided") or "").lower() in ("1", "true", "yes")
        try:
            limit = int(q.get("limit") or 100)
        except ValueError:
            limit = 100
        conn = db()
        try:
            rows = publish_module.list_drafts(conn, who.scope, include_decided=include,
                                              limit=limit)  # fmt: skip
        finally:
            conn.close()
        return {"drafts": rows}

    @app.post("/api/runs/{run_key}/publish")
    async def publish(
        request: Request, run_key: str, who: Principal = Depends(principal)
    ) -> dict[str, Any]:
        body = await json_body(request)
        if not isinstance(body, dict):
            raise ApiError(400, "E_CONFIG", "request body must be a JSON object")
        snap = await run_in_threadpool(own_snapshot, run_key, who)
        if snap.get("status") in ("running", "queued"):
            raise ApiError(409, "E_STATE", "run is still converting")

        def _decide() -> dict[str, Any]:
            conn = db()
            try:
                return publish_module.decide(
                    conn, run_key=run_key, events=snap.get("events") or [], reviewer_fp=who.fp,
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
                  decision=result.get("decision"), tenant=who.fp[:12],
                  edited=result.get("edited"))  # fmt: skip
        return result

    # ---- policies, usage, metrics ---------------------------------------------------
    @app.get("/api/odrl")
    def odrl(request: Request, who: Principal = Depends(principal)) -> dict[str, Any]:
        flags = web._query_flags(_query(request))
        conn = db()
        try:
            return {"pairs": web._list_pairs(conn, flags, who.scope)}
        finally:
            conn.close()

    @app.get("/api/usage")
    def usage(who: Principal = Depends(principal)) -> dict[str, Any]:
        conn = db()
        try:
            return {"usage": web._usage_rows(conn, who.scope)}
        finally:
            conn.close()

    @app.get("/api/metrics")
    def metrics(who: Principal = Depends(principal)) -> PlainTextResponse:
        del who
        METRICS.set("nl2odrl_runs_retained", app.state.registry.retained())
        return PlainTextResponse(METRICS.render(),
                                 media_type="text/plain; version=0.0.4; charset=utf-8")  # fmt: skip

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def unknown(rest: str, who: Principal = Depends(principal)) -> None:
        del rest, who
        raise ApiError(404, "E_CONFIG", "unknown endpoint")

    if ui_dir is not None and ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")
    return app


def _query(request: Request) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        out.setdefault(key, []).append(value)
    return out
