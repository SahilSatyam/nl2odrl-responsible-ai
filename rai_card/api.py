"""Auditor-scoped Transparency Portal (Day 060 pattern).

Returns the model card, compliance bundle, computed gate, and gold-set traces
as JSON. Does not convert NL to ODRL and does not authorize live requests.

The gate is recomputed on every call from files on disk. There is no
hardcoded "PASS".
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from rai_card.catalog import (
    bundle_payload,
    card_payload,
    exhibit_trace,
    list_exhibits,
    load_catalog,
)

DEFAULT_TOKEN = "auditor-demo-token"


def _root() -> Path:
    return Path(os.environ.get("RAI_ROOT", Path.cwd())).resolve()


def _expected_token() -> str:
    return os.environ.get("AUDIT_API_TOKEN", DEFAULT_TOKEN)


def require_auditor(
    authorization: str | None = Header(default=None),
    x_audit_role: str | None = Header(default=None, alias="X-Audit-Role"),
) -> None:
    expected = _expected_token()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Auditor routes require Authorization: Bearer <token>.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="Invalid auditor token.")
    if x_audit_role and x_audit_role.lower() not in {"auditor", "compliance_officer"}:
        raise HTTPException(
            status_code=403,
            detail="X-Audit-Role must be auditor or compliance_officer when sent.",
        )


def create_app(root: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="NL2ODRL Auditor Portal",
        description=(
            "Read-only transparency API for the NL→ODRL→Cedar/Rego assistant. "
            "Not an inference API. Not a PEP."
        ),
        version="1.0.0",
    )
    resolved = (root or _root()).resolve()

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "system": "nl2odrl-auditor-portal",
            "predict": False,
            "converts_nl": False,
            "utc": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/v1/audit", dependencies=[Depends(require_auditor)])
    def audit_index() -> dict[str, Any]:
        return {
            "role": "auditor",
            "note": "Machine-parseable system of record. Gate is recomputed, not cached PASS.",
            "endpoints": {
                "card": "/v1/audit/card",
                "bundle": "/v1/audit/bundle",
                "gate": "/v1/audit/gate",
                "metrics": "/v1/audit/metrics",
                "policy": "/v1/audit/policy",
                "exhibits": "/v1/audit/exhibits",
                "trace": "/v1/audit/exhibits/{id}",
                "check": "/v1/audit/checks/{check_id}",
            },
        }

    @app.get("/v1/audit/card", dependencies=[Depends(require_auditor)])
    def audit_card() -> dict[str, Any]:
        return card_payload(load_catalog(resolved))

    @app.get("/v1/audit/bundle", dependencies=[Depends(require_auditor)])
    def audit_bundle() -> dict[str, Any]:
        return bundle_payload(load_catalog(resolved))

    @app.get("/v1/audit/gate", dependencies=[Depends(require_auditor)])
    def audit_gate() -> dict[str, Any]:
        cat = load_catalog(resolved)
        payload = cat.report.to_dict()
        payload["source"] = "recomputed_on_read"
        return payload

    @app.get("/v1/audit/metrics", dependencies=[Depends(require_auditor)])
    def audit_metrics() -> dict[str, Any]:
        cat = load_catalog(resolved)
        return {
            "note": "Evidence only. Ignore any status field. Use /v1/audit/gate for the verdict.",
            "metrics": cat.dynamic_raw,
            "gate_passed": cat.report.passed,
        }

    @app.get("/v1/audit/policy", dependencies=[Depends(require_auditor)])
    def audit_policy() -> dict[str, Any]:
        cat = load_catalog(resolved)
        return {
            "note": "Thresholds owned by Responsible AI, not by the eval job.",
            "policy": cat.policy.model_dump(mode="json"),
        }

    @app.get("/v1/audit/exhibits", dependencies=[Depends(require_auditor)])
    def audit_exhibits() -> dict[str, Any]:
        return {"items": list_exhibits(resolved)}

    @app.get("/v1/audit/exhibits/{item_id}", dependencies=[Depends(require_auditor)])
    def audit_trace(item_id: str) -> dict[str, Any]:
        trace = exhibit_trace(resolved, item_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"No gold exhibit '{item_id}'.")
        return trace

    @app.get("/v1/audit/checks/{check_id}", dependencies=[Depends(require_auditor)])
    def audit_check(check_id: str) -> dict[str, Any]:
        cat = load_catalog(resolved)
        for check in cat.report.checks:
            if check.check_id == check_id:
                return {
                    "check_id": check.check_id,
                    "title": check.title,
                    "passed": check.passed,
                    "severity": check.severity,
                    "message": check.message,
                    "control_mapping": check.control_mapping,
                    "evidence": check.evidence,
                }
        raise HTTPException(status_code=404, detail=f"Unknown check '{check_id}'.")

    @app.api_route("/v1/predict", methods=["GET", "POST"])
    def no_predict() -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_an_inference_api",
                "detail": (
                    "This portal does not convert NL or authorize requests. "
                    "Use /v1/audit/* with an auditor bearer token."
                ),
            },
        )

    @app.exception_handler(HTTPException)
    async def http_exc(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "http_error", "detail": exc.detail},
            headers=exc.headers,
        )

    return app


app = create_app()


def serve() -> None:
    import os

    import uvicorn

    host = os.environ.get("AUDIT_API_HOST", "127.0.0.1")
    port = int(os.environ.get("AUDIT_API_PORT", "8080"))
    uvicorn.run(app, host=host, port=port)
