"""Reference host: independent NL-to-ODRL policy engine.

Owns its agentic loop, tools, and persistence. AgentCrash is an optional
dependency: with ``AGENTCRASH_DISABLED=1`` (or no ``agentcrash`` import)
every business path still works. With the SDK enabled, the same functions
are observed via ``@ac.workflow`` / ``run.step`` without transferring
workflow ownership.
"""

from __future__ import annotations

import os
from typing import Any

try:
    from agentcrash import AgentCrash, WorkflowConfig

    _SDK_AVAILABLE = True
except ImportError:  # host works without the framework installed
    AgentCrash = WorkflowConfig = None  # type: ignore
    _SDK_AVAILABLE = False


def _sdk() -> Any | None:
    if not _SDK_AVAILABLE or os.environ.get("AGENTCRASH_DISABLED") == "1":
        return None
    return AgentCrash(
        WorkflowConfig(project="odrl-policy-engine", environment="development")
    )


def parse_policy(policy_text: str) -> dict[str, Any]:
    """Host-owned NL understanding (fixture-free toy parser)."""
    text = policy_text.strip()
    return {
        "action": "read" if "read" in text.lower() else "use",
        "target": "dataset-1",
        "raw_chars": len(text),
    }


def generate_odrl(parsed: dict[str, Any]) -> dict[str, Any]:
    """Host-owned ODRL generation (no AgentCrash imports)."""
    return {
        "@context": "http://www.w3.org/ns/odrl.jsonld",
        "uid": "urn:odrl:policy-1",
        "type": "Set",
        "permission": [{"action": parsed["action"], "target": parsed["target"]}],
    }


def validate_odrl(odrl: dict[str, Any]) -> dict[str, Any]:
    """Host-owned validation gate (structural; full SHACL lives in host ext)."""
    perm = odrl.get("permission")
    ok = (
        isinstance(perm, list)
        and bool(perm)
        and all(isinstance(p, dict) and p.get("action") and p.get("target") for p in perm)
    )
    return {"valid": ok, "errors": [] if ok else ["permission malformed"]}


_PERSISTED: list[dict[str, Any]] = []


def persist_policy(odrl: dict[str, Any]) -> dict[str, Any]:
    """Host-owned persistence (in-memory stand-in for the real store)."""
    _PERSISTED.append(odrl)
    return {"record": "odrl-policy-1", "version": len(_PERSISTED)}


def convert_policy(policy_text: str, *, ac: Any | None = None) -> dict[str, Any]:
    """Existing host workflow. AgentCrash observes; it never orchestrates."""
    sdk = ac if ac is not None else _sdk()
    if sdk is None:  # disconnected path — identical business behavior
        parsed = parse_policy(policy_text)
        odrl = generate_odrl(parsed)
        validated = validate_odrl(odrl)
        stored = persist_policy(odrl) if validated["valid"] else None
        return {"odrl": odrl, "validated": validated, "stored": stored}
    with sdk.run(name="nl-to-odrl") as run:
        with run.step("parse-policy"):
            parsed = parse_policy(policy_text)
        with run.step("generate-odrl"):
            odrl = generate_odrl(parsed)
        with run.step("validate-odrl"):
            validated = validate_odrl(odrl)
        with run.step("persist"):
            stored = persist_policy(odrl) if validated["valid"] else None
    return {
        "odrl": odrl,
        "validated": validated,
        "stored": stored,
        "run_id": run.run_id,
    }
