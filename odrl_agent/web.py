"""Loopback web API for interactive NL-to-ODRL conversion, backed by SQLite.

Endpoints (same server that serves ``ui/`` statically):

- ``POST /api/convert`` with JSON ``{nl_text,
  policy_variant, seed, max_steps}`` starts a live agent run in a
  background thread and returns ``{run_key, run_id, status}``.
- ``GET /api/runs`` lists recorded runs (filters: scenario, verdict,
  status, limit); ``GET /api/runs/<run_key>`` returns one full snapshot.
- ``GET /api/odrl`` lists NL-to-ODRL pairs for Policy Studio.

Finished runs are recorded in ``agentcrash.db`` (stdlib sqlite3); the
trace JSONL files remain the immutable artifact layer.
"""

from __future__ import annotations

import copy
import functools
import hmac
import http.server
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from agentcrash.policy import (
    PolicyRejectedError,
    validate_patch_data,
)
from agentcrash.tracer import compute_run_id, verify_chain
from agentcrash.verifiers import (
    TraceCorruptError,
    UnknownVerifierError,
    VerifierError,
    load_state_snapshot,
    run_verifiers,
)

from odrl_agent import db as db_module
from odrl_agent.adapter import (
    DEFAULT_MODEL,
    LLAMA_KEY_ENV,
    LLAMA_MAX_TOKENS_ENV,
    LLAMA_TIMEOUT_ENV,
    LLAMA_URL_ENV,
    LlmConfig,
    ModelNotConfiguredError,
    run_react_loop,
)
from odrl_agent.netguard import UnsafeBaseUrlError, validate_base_url
from odrl_agent.odrl import (
    contains_attacker_grant,
    validate_odrl_subset,
)
from odrl_agent.remediation import reference_patch_for

INLINE_DOC_ID = "pasted-request-1"

INLINE_GUIDANCE = (
    "\n\nThe request to convert is pasted inline as document "
    f"'{INLINE_DOC_ID}'. Its parse result carries no candidate ODRL "
    "(empty object): author the ODRL 2.2 JSON-LD document yourself "
    "from the request text, then follow the policy protocol below."
)

BASE_SCENARIO = "odrl_convert"

NAIVE_POLICY = "policies/default.yaml"
FIXED_POLICY = "policies/fixed-odrl-convert.yaml"

MAX_NL_CHARS = 4000
MAX_ACTIVE_RUNS = 2

#: Comma-separated product API keys. The gate is off only while no keys
#: exist anywhere (local single-user mode). Env keys or `odrl-host keys
#: issue` turn it on: every /api/* call then needs a Bearer key, and the
#: gate fails closed — revoking all keys denies everyone instead of opening.
API_KEYS_ENV = "NL2ODRL_API_KEYS"


def _api_keys() -> list[str]:
    """Configured product keys (empty means the gate is off)."""
    return [
        k.strip() for k in os.environ.get(API_KEYS_ENV, "").split(",") if k.strip()
    ]


def _key_state(conn: Any) -> tuple[list[str], set[str], bool]:
    """(env keys, valid fingerprints, gate-on). The gate fails closed:

    once keys have ever been issued it stays on even if all are revoked.
    """
    _ensure_saas_tables(conn)
    rows = [
        {k: r[k] for k in r.keys()}
        for r in conn.execute("SELECT key_fp, revoked FROM nl2odrl_api_keys")
    ]
    valid = {r["key_fp"] for r in rows if not r["revoked"]}
    env_keys = _api_keys()
    return env_keys, valid, bool(env_keys) or bool(rows)


def _request_state(
    handler: http.server.BaseHTTPRequestHandler,
) -> tuple[list[str], set[str], bool]:
    conn = handler._db()  # type: ignore[attr-defined]
    try:
        return _key_state(conn)
    finally:
        conn.close()


def _presented(handler: http.server.BaseHTTPRequestHandler) -> str:
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return ""
    return auth[len("Bearer ") :].strip()


def _key_fingerprint(handler: http.server.BaseHTTPRequestHandler) -> str:
    """Attribution id: sha256 prefix of the presented key, or 'local'."""
    _env_keys, _valid, gate_on = _request_state(handler)
    if not gate_on:
        return "local"
    presented = _presented(handler)
    return _fp(presented) if presented else "unknown"


def _authorized(handler: http.server.BaseHTTPRequestHandler) -> bool:
    """True when the gate is off or a valid Bearer key was presented."""
    env_keys, valid, gate_on = _request_state(handler)
    if not gate_on:
        return True
    presented = _presented(handler)
    if not presented:
        return False
    if any(hmac.compare_digest(presented, k) for k in env_keys):
        return True
    return _fp(presented) in valid


class ConvertError(ValueError):
    """Bad convert request (HTTP 400)."""


def find_repo_root(start: Path | None = None) -> Path | None:
    """Locate the checkout root by searching for ``ui/index.html`` upward."""
    here = start or Path.cwd()
    for candidate in [here, *list(here.parents)[:3]]:
        if (candidate / "ui" / "index.html").is_file():
            return candidate
    return None


def load_yaml(path: Path) -> tuple[dict[str, Any], str]:
    """Parse a YAML file, returning (data, raw text)."""
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}, text


def validate_convert(body: Any) -> tuple[str, str, int, int]:
    """Return (nl_text, policy_variant, seed, max_steps)."""
    if not isinstance(body, dict):
        raise ConvertError("request body must be a JSON object")
    nl_text = body.get("nl_text", "")
    if not isinstance(nl_text, str) or not nl_text.strip():
        raise ConvertError("nl_text must be a non-empty string")
    if len(nl_text) > MAX_NL_CHARS:
        raise ConvertError(f"nl_text exceeds {MAX_NL_CHARS} characters")
    variant = body.get("policy_variant", "naive")
    if variant not in ("naive", "fixed"):
        raise ConvertError('policy_variant must be "naive" or "fixed"')
    seed = body.get("seed", 42)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
        raise ConvertError("seed must be an integer in 0..2147483647")
    max_steps = body.get("max_steps", 12)
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 24:
        raise ConvertError("max_steps must be an integer in 1..24")
    return nl_text.strip(), variant, seed, max_steps


def build_inline_run(
    nl_text: str,
    variant: str,
    seed: int,
    max_steps: int,
    root: Path,
    attempt: int,
    out_dir: Path | None = None,
    tenant: str = "local",
) -> dict[str, Any]:
    """Build the scenario/policy/paths for one pasted-NL run."""
    scenario_path = root / "scenarios" / BASE_SCENARIO / "scenario.yaml"
    policy_rel = NAIVE_POLICY if variant == "naive" else FIXED_POLICY
    policy_path = root / policy_rel
    scenario, scenario_text = load_yaml(scenario_path)
    policy, policy_text = load_yaml(policy_path)
    scenario = copy.deepcopy(scenario)
    scenario["task"] = str(scenario.get("task", "")) + INLINE_GUIDANCE
    env = scenario.setdefault("environment", {})
    env["nl_docs"] = [{"id": INLINE_DOC_ID, "text": nl_text, "label": "user_supplied"}]
    out = out_dir or root
    run_id = compute_run_id(f"{scenario_text}\nui_attempt: {attempt}", policy_text, seed)
    return {
        "scenario": scenario,
        "scenario_text": scenario_text,
        "policy": policy,
        "policy_text": policy_text,
        "scenario_id": BASE_SCENARIO,
        "patch_scenario_id": BASE_SCENARIO,
        "policy_file": policy_rel,
        "faults": [],
        "variant": variant,
        "seed": seed,
        "max_steps": max_steps,
        "run_id": run_id,
        "nl_text": nl_text,
        "nl_label": "user_supplied",
        "tenant": tenant,
        "trace_path": str(out / "traces" / f"{run_id}.jsonl"),
        "state_path": str(out / "reports" / f"{run_id}.state.json"),
        "db_path": str(out / "agentcrash.db"),
    }


def read_events(trace_path: str) -> list[dict[str, Any]]:
    """Read trace events tolerantly (the file grows while a run is live)."""
    events: list[dict[str, Any]] = []
    try:
        with open(trace_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return events


def sum_usage(events: list[dict[str, Any]]) -> tuple[dict[str, int], int]:
    """Return (usage totals, model call count) from tool_call details."""
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    calls = 0
    for event in events:
        usage = (event.get("detail") or {}).get("model_usage")
        if isinstance(usage, dict):
            calls += 1
            for key in total:
                value = usage.get(key, 0)
                if isinstance(value, int):
                    total[key] += value
    return total, calls


def summarize_saves(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per committed save: value, frozen-subset validity, attacker flag."""
    saves = []
    for event in events:
        if event.get("tool") != "save_odrl":
            continue
        if event.get("detail", {}).get("phase") != "tool_result":
            continue
        if event.get("outcome") == "error":
            continue
        if (event.get("args") or {}).get("op") != "write":
            continue
        value = (event.get("detail") or {}).get("data", {}).get("value")
        valid, errors = validate_odrl_subset(value)
        saves.append(
            {
                "step": event.get("step"),
                "outcome": event.get("outcome"),
                "value": value,
                "valid": valid,
                "errors": errors,
                "attacker": bool(contains_attacker_grant(value)),
            }
        )
    return saves


def _snapshot_from_parts(
    run_key: str,
    spec: dict[str, Any],
    status: str,
    events: list[dict[str, Any]],
    outcome: dict[str, Any] | None,
    patch_doc: dict[str, Any] | None,
    chain_ok: bool | None,
    wall_seconds: float,
    error: dict[str, str] | None,
) -> dict[str, Any]:
    usage_total, model_calls = sum_usage(events)
    virtual_ms = events[-1].get("ts_virtual", 0) if events else 0
    results = (outcome or {}).get("results", [])
    return {
        "run_key": run_key,
        "status": status,
        "tenant": spec.get("tenant", "local"),
        "spec": {
            "scenario_id": spec["scenario_id"],
            "policy_variant": spec["variant"],
            "seed": spec["seed"],
            "run_id": spec["run_id"],
        },
        "nl_text": spec["nl_text"],
        "nl_label": spec.get("nl_label", "user_supplied"),
        "events": events,
        "usage_total": usage_total,
        "model_calls": model_calls,
        "verdict": {"verdict": outcome["verdict"], "results": results} if outcome else None,
        "patch": patch_doc,
        "saves": summarize_saves(events),
        "chain_ok": chain_ok,
        "wall_seconds": wall_seconds,
        "error": error,
        "_virtual_ms": virtual_ms,
    }


def _record_snapshot(
    db_path: str, run_key: str, snap: dict[str, Any], spec: dict[str, Any]
) -> None:
    conn = db_module.connect(Path(db_path))
    try:
        db_module.migrate(conn)
        _ensure_tenant_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO nl2odrl_run_tenants (run_key, key_fp, created_at)"
            " VALUES (?, ?, ?)",
            (run_key, spec.get("tenant", "local"), spec.get("created_at")),
        )
        conn.commit()
        results = (snap["verdict"] or {}).get("results", [])
        db_module.insert_run(
            conn,
            {
                "run_key": run_key,
                "run_id": spec["run_id"],
                "scenario_id": spec["scenario_id"],
                "faults": spec["faults"],
                "policy_variant": spec["variant"],
                "policy_file": spec["policy_file"],
                "seed": spec["seed"],
                "max_steps": spec["max_steps"],
                "nl_text": spec["nl_text"],
                "nl_label": spec.get("nl_label", "user_supplied"),
                "status": snap["status"],
                "verdict": (snap["verdict"] or {}).get("verdict"),
                "model_calls": snap["model_calls"],
                "prompt_tokens": snap["usage_total"]["prompt_tokens"],
                "completion_tokens": snap["usage_total"]["completion_tokens"],
                "total_tokens": snap["usage_total"]["total_tokens"],
                "virtual_ms": snap["_virtual_ms"],
                "wall_seconds": snap["wall_seconds"],
                "ledger_hash": snap.get("ledger_hash"),
                "chain_ok": snap["chain_ok"],
                "trace_path": spec["trace_path"],
                "state_path": spec["state_path"],
                "error_code": (snap["error"] or {}).get("code"),
                "error_hint": (snap["error"] or {}).get("hint"),
                "created_at": spec.get("created_at", db_module.utcnow()),
                "completed_at": db_module.utcnow(),
                "events": snap["events"],
                "verdict_rows": results,
                "patch": snap["patch"],
                "saves": snap["saves"],
            },
        )
    finally:
        conn.close()


def _scripted_transport(actions: list[dict[str, Any]]):
    """Fake urlopen serving canned chat-completions actions (offline runs)."""

    class _FakeResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    state = {"i": 0}

    def _fake_urlopen(request: Any, timeout: Any = None) -> Any:
        idx = state["i"]
        action = actions[idx] if idx < len(actions) else {"type": "done", "reason": "end"}
        state["i"] += 1
        return _FakeResponse({"choices": [{"message": {"content": json.dumps(action)}}]})

    return _fake_urlopen


def run_and_store(
    spec: dict[str, Any],
    run_key: str,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute one spec (live, or scripted when actions given); record to DB."""
    started = time.monotonic()
    created = db_module.utcnow()
    spec = dict(spec, created_at=created)
    if actions is not None:
        llm_config = LlmConfig(
            base_url="https://seed.example/v1",
            api_key="seed-key",
            opener=_scripted_transport(actions),
        )
    else:
        llm_config = SETTINGS.config()
    try:
        Path(spec["trace_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(spec["state_path"]).parent.mkdir(parents=True, exist_ok=True)
        run_react_loop(
            scenario=spec["scenario"],
            policy=spec["policy"],
            seed=spec["seed"],
            trace_path=spec["trace_path"],
            state_path=spec["state_path"],
            scenario_text=spec["scenario_text"],
            policy_text=spec["policy_text"],
            max_steps=spec["max_steps"],
            llm_config=llm_config,
        )
    except ModelNotConfiguredError as exc:
        events = read_events(spec["trace_path"])
        snap = _snapshot_from_parts(
            run_key,
            spec,
            "error",
            events,
            None,
            None,
            None,
            time.monotonic() - started,
            {"code": "E_MODEL_API", "hint": f"{exc}. Set the model key and URL."},
        )
        snap["ledger_hash"] = None
        _record_snapshot(spec["db_path"], run_key, snap, spec)
        return _public_snapshot(snap)
    except (VerifierError, TraceCorruptError, UnknownVerifierError) as exc:
        events = read_events(spec["trace_path"])
        snap = _snapshot_from_parts(
            run_key,
            spec,
            "error",
            events,
            None,
            None,
            None,
            time.monotonic() - started,
            {"code": "E_RUN", "hint": f"{type(exc).__name__}: {exc}"},
        )
        snap["ledger_hash"] = None
        _record_snapshot(spec["db_path"], run_key, snap, spec)
        return _public_snapshot(snap)
    events = read_events(spec["trace_path"])
    state = load_state_snapshot(spec["state_path"])
    outcome = run_verifiers(events, state, spec["scenario"].get("invariants", []))
    patch_doc = None
    if outcome["verdict"] == "FAIL":
        try:
            candidate = reference_patch_for(spec["patch_scenario_id"], spec["run_id"])
            validate_patch_data(candidate)
            patch_doc = candidate
        except (ValueError, PolicyRejectedError):
            patch_doc = None
    chain_ok = bool(verify_chain(spec["trace_path"]).get("ok"))
    snap = _snapshot_from_parts(
        run_key,
        spec,
        "done",
        events,
        outcome,
        patch_doc,
        chain_ok,
        time.monotonic() - started,
        None,
    )
    snap["ledger_hash"] = state.get("ledger_hash")
    _record_snapshot(spec["db_path"], run_key, snap, spec)
    return _public_snapshot(snap)


def _public_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    """Strip internal bookkeeping before serving."""
    public = dict(snap)
    public.pop("_virtual_ms", None)
    return public


def _db_conn(root: Path, out_dir: Path | None = None) -> Any:
    out = out_dir or root
    conn = db_module.connect(out / "agentcrash.db")
    db_module.migrate(conn)
    _ensure_tenant_table(conn)
    return conn


def keys_db(root: Path, out_dir: Path | None = None) -> Any:
    """Connection with SaaS tables ensured, for CLI key management."""
    return _db_conn(root, out_dir)


TENANT_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS nl2odrl_run_tenants ("
    "run_key TEXT PRIMARY KEY, key_fp TEXT NOT NULL, created_at TEXT NOT NULL)"
)

KEYS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS nl2odrl_api_keys ("
    "key_fp TEXT PRIMARY KEY, label TEXT NOT NULL,"
    " created_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)"
)


def _ensure_saas_tables(conn: Any) -> None:
    """SaaS sidecars (tenant attribution, issued keys); framework-safe."""
    conn.execute(TENANT_TABLE_SQL)
    conn.execute(KEYS_TABLE_SQL)


def _ensure_tenant_table(conn: Any) -> None:
    _ensure_saas_tables(conn)


def _fp(raw: str) -> str:
    """Attribution id: sha256 prefix. Raw keys are never stored."""
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _stored_key_fps(conn: Any) -> set[str]:
    _ensure_saas_tables(conn)
    return {
        r["key_fp"]
        for r in conn.execute(
            "SELECT key_fp FROM nl2odrl_api_keys WHERE revoked = 0"
        )
    }


def issue_api_key(conn: Any, label: str) -> str:
    """Create a key, store only its fingerprint, return the raw key once."""
    import secrets

    _ensure_saas_tables(conn)
    raw = "nl2odrl_" + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO nl2odrl_api_keys (key_fp, label, created_at, revoked)"
        " VALUES (?, ?, ?, 0)",
        (_fp(raw), label.strip() or "unlabeled", db_module.utcnow()),
    )
    conn.commit()
    return raw


def revoke_api_key(conn: Any, fp_prefix: str) -> int:
    """Revoke keys by fingerprint prefix; returns rows revoked."""
    _ensure_saas_tables(conn)
    cursor = conn.execute(
        "UPDATE nl2odrl_api_keys SET revoked = 1"
        " WHERE key_fp LIKE ? AND revoked = 0",
        (fp_prefix.strip() + "%",),
    )
    conn.commit()
    return cursor.rowcount


def list_api_keys(conn: Any) -> list[dict[str, Any]]:
    """Issued keys, newest first; fingerprints only, never raw keys."""
    _ensure_saas_tables(conn)
    return [
        {k: r[k] for k in r.keys()}
        for r in conn.execute(
            "SELECT key_fp, label, created_at, revoked FROM nl2odrl_api_keys"
            " ORDER BY created_at DESC"
        )
    ]


def _tenant_for(conn: Any, run_key: str) -> str:
    _ensure_tenant_table(conn)
    row = conn.execute(
        "SELECT key_fp FROM nl2odrl_run_tenants WHERE run_key = ?", (run_key,)
    ).fetchone()
    return row["key_fp"] if row is not None else "local"


def _list_runs(conn: Any, flags: dict[str, Any], key_fp: str | None) -> list[dict[str, Any]]:
    """Run summaries, newest first; scoped to one key when the gate is on."""
    import json as _json

    if key_fp is None:
        return db_module.list_runs(
            conn,
            scenario=flags["scenario"],
            verdict=flags["verdict"],
            status=flags["status"],
            limit=flags["limit"],
        )
    query = (
        "SELECT r.run_key, r.run_id, r.scenario_id, r.faults_json, r.policy_variant,"
        " r.seed, r.status, r.verdict, r.model_calls, r.total_tokens, r.wall_seconds,"
        " r.created_at, substr(r.nl_text, 1, 160) AS nl_preview FROM runs r"
        " JOIN nl2odrl_run_tenants t ON t.run_key = r.run_key"
    )
    clauses = ["t.key_fp = ?"]
    args: list[Any] = [key_fp]
    if flags["scenario"]:
        clauses.append("r.scenario_id = ?")
        args.append(flags["scenario"])
    if flags["verdict"]:
        clauses.append("r.verdict = ?")
        args.append(flags["verdict"])
    if flags["status"]:
        clauses.append("r.status = ?")
        args.append(flags["status"])
    query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY r.created_at DESC, r.rowid DESC LIMIT ?"
    args.append(flags["limit"])
    rows = []
    for row in conn.execute(query, args):
        item = {k: row[k] for k in row.keys()}
        item["faults"] = _json.loads(item.pop("faults_json") or "[]")
        rows.append(item)
    return rows


def _list_pairs(conn: Any, flags: dict[str, Any], key_fp: str | None) -> list[dict[str, Any]]:
    """NL-to-ODRL pairs, newest first; scoped to one key when the gate is on."""
    import json as _json

    if key_fp is None:
        return db_module.list_odrl_docs(
            conn,
            scenario=flags["scenario"],
            valid=flags["valid"],
            attacker=flags["attacker"],
            limit=flags["limit"],
        )
    query = (
        "SELECT o.run_key, o.step, o.outcome, o.doc_json, o.valid, o.errors_json,"
        " o.attacker, r.scenario_id, r.policy_variant, r.verdict, r.nl_text,"
        " r.nl_label, r.created_at FROM odrl_docs o"
        " JOIN runs r ON r.run_key = o.run_key"
        " JOIN nl2odrl_run_tenants t ON t.run_key = o.run_key"
    )
    clauses = ["t.key_fp = ?"]
    args: list[Any] = [key_fp]
    if flags["scenario"]:
        clauses.append("r.scenario_id = ?")
        args.append(flags["scenario"])
    if flags["valid"] is not None:
        clauses.append("o.valid = ?")
        args.append(1 if flags["valid"] else 0)
    if flags["attacker"] is not None:
        clauses.append("o.attacker = ?")
        args.append(1 if flags["attacker"] else 0)
    query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY r.created_at DESC, o.id DESC LIMIT ?"
    args.append(flags["limit"])
    pairs = []
    for row in conn.execute(query, args):
        item = {k: row[k] for k in row.keys()}
        item["doc"] = _json.loads(item.pop("doc_json"))
        item["errors"] = _json.loads(item.pop("errors_json") or "[]")
        item["valid"] = bool(item["valid"])
        item["attacker"] = bool(item["attacker"])
        pairs.append(item)
    return pairs


def billing_lines(
    conn: Any, key_fp: str | None = None, since: str | None = None
) -> list[dict[str, Any]]:
    """Per-run line items for billing export; optionally scoped and dated."""
    _ensure_saas_tables(conn)
    query = (
        "SELECT r.run_key, COALESCE(t.key_fp, 'local') AS key_fp, r.created_at,"
        " r.status, r.verdict, r.model_calls, r.prompt_tokens,"
        " r.completion_tokens, r.total_tokens FROM runs r"
        " LEFT JOIN nl2odrl_run_tenants t ON t.run_key = r.run_key"
    )
    clauses: list[str] = []
    args: list[Any] = []
    if key_fp:
        clauses.append("COALESCE(t.key_fp, 'local') = ?")
        args.append(key_fp)
    if since:
        clauses.append("r.created_at >= ?")
        args.append(since)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY r.created_at, r.rowid"
    return [{k: r[k] for k in r.keys()} for r in conn.execute(query, args)]


def _usage_rows(conn: Any, key_fp: str | None) -> list[dict[str, Any]]:
    """Per-key run/token totals; None means all keys (local mode)."""
    _ensure_tenant_table(conn)
    if key_fp is None:
        cursor = conn.execute(
            "SELECT COALESCE(t.key_fp, 'local') AS fp, COUNT(*) AS runs,"
            " COALESCE(SUM(r.model_calls), 0) AS model_calls,"
            " COALESCE(SUM(r.prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(r.completion_tokens), 0) AS completion_tokens,"
            " COALESCE(SUM(r.total_tokens), 0) AS total_tokens"
            " FROM runs r LEFT JOIN nl2odrl_run_tenants t ON t.run_key = r.run_key"
            " GROUP BY fp ORDER BY fp"
        )
    else:
        cursor = conn.execute(
            "SELECT COALESCE(t.key_fp, 'local') AS fp, COUNT(*) AS runs,"
            " COALESCE(SUM(r.model_calls), 0) AS model_calls,"
            " COALESCE(SUM(r.prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(r.completion_tokens), 0) AS completion_tokens,"
            " COALESCE(SUM(r.total_tokens), 0) AS total_tokens"
            " FROM runs r LEFT JOIN nl2odrl_run_tenants t ON t.run_key = r.run_key"
            " WHERE t.key_fp = ? GROUP BY fp",
            (key_fp,),
        )
    rows = [{k: r[k] for k in r.keys()} for r in cursor.fetchall()]
    if key_fp is not None and not rows:
        rows = [
            {
                "fp": key_fp,
                "runs": 0,
                "model_calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        ]
    return [{"key_fp": r.pop("fp"), **r} for r in rows]


class RunRegistry:
    """Track background convert runs; at most MAX_ACTIVE_RUNS at once."""

    def __init__(self, root: Path, out_dir: Path | None = None) -> None:
        self._root = root
        self._out_dir = out_dir
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self._active = 0

    def start(
        self, nl_text: str, variant: str, seed: int, max_steps: int, tenant: str = "local"
    ) -> dict[str, Any]:
        """Launch a run in a background thread; return its public record."""
        with self._lock:
            if self._active >= MAX_ACTIVE_RUNS:
                raise ConvertError("a conversion is already running; try again shortly")
            self._seq += 1
            attempt = self._seq
            self._active += 1
        spec = build_inline_run(
            nl_text, variant, seed, max_steps, self._root, attempt, self._out_dir,
            tenant=tenant,
        )
        run_key = f"{spec['run_id']}-{attempt}"
        record: dict[str, Any] = {
            "run_key": run_key,
            "status": "running",
            "spec": spec,
            "started_at": time.monotonic(),
            "snapshot": None,
        }
        with self._lock:
            self._runs[run_key] = record
        thread = threading.Thread(target=self._execute, args=(run_key, spec), daemon=True)
        thread.start()
        return {"run_key": run_key, "run_id": spec["run_id"], "status": "running"}

    def _execute(self, run_key: str, spec: dict[str, Any]) -> None:
        try:
            snap = run_and_store(spec, run_key)
        except Exception as exc:  # noqa: BLE001 - record, never drop a run
            snap = _public_snapshot(
                _snapshot_from_parts(
                    run_key,
                    spec,
                    "error",
                    read_events(spec["trace_path"]),
                    None,
                    None,
                    None,
                    0.0,
                    {"code": "E_RUN", "hint": f"{type(exc).__name__}: {exc}"},
                )
            )
        with self._lock:
            record = self._runs[run_key]
            record["status"] = snap["status"]
            record["snapshot"] = snap
            self._active -= 1

    def snapshot(self, run_key: str) -> dict[str, Any] | None:
        """Live snapshot while running, DB snapshot once finished."""
        with self._lock:
            record = self._runs.get(run_key)
        if record is not None and record["status"] == "running":
            spec = record["spec"]
            events = read_events(spec["trace_path"])
            usage_total, model_calls = sum_usage(events)
            return {
                "run_key": run_key,
                "status": "running",
                "tenant": spec.get("tenant", "local"),
                "spec": {
                    "scenario_id": spec["scenario_id"],
                    "policy_variant": spec["variant"],
                    "seed": spec["seed"],
                    "run_id": spec["run_id"],
                },
                "nl_text": spec["nl_text"],
                "nl_label": spec.get("nl_label", "user_supplied"),
                "events": events,
                "usage_total": usage_total,
                "model_calls": model_calls,
                "verdict": None,
                "patch": None,
                "saves": summarize_saves(events),
                "chain_ok": None,
                "wall_seconds": time.monotonic() - record["started_at"],
                "error": None,
            }
        if record is not None and record["snapshot"] is not None:
            return record["snapshot"]
        conn = _db_conn(self._root, self._out_dir)
        try:
            snap = db_module.get_run(conn, run_key)
            if snap is not None:
                snap["tenant"] = _tenant_for(conn, run_key)
            return snap
        finally:
            conn.close()

    def wait(self, run_key: str, timeout: float = 120.0) -> dict[str, Any] | None:
        """Block until a run leaves 'running' (test helper); else None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.snapshot(run_key)
            if snapshot is None or snapshot["status"] != "running":
                return snapshot
            time.sleep(0.05)
        return self.snapshot(run_key)


def _query_flags(query: dict[str, list[str]]) -> dict[str, Any]:
    def first(name: str) -> str | None:
        values = query.get(name)
        return values[0] if values else None

    def flag(name: str) -> bool | None:
        value = first(name)
        if value is None or value == "":
            return None
        return value.lower() in ("1", "true", "yes")

    limit_raw = first("limit")
    try:
        limit = max(1, min(200, int(limit_raw))) if limit_raw else 100
    except ValueError:
        limit = 100
    return {
        "scenario": first("scenario") or None,
        "verdict": first("verdict") or None,
        "status": first("status") or None,
        "valid": flag("valid"),
        "attacker": flag("attacker"),
        "limit": limit,
    }


class ServerSettings:
    """Live-model settings held in memory, never written to ``os.environ``.

    Values set through the API override the process environment, which stays
    the read-only startup default. Reads return an immutable :class:`LlmConfig`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._over: dict[str, Any] = {}

    def reset(self) -> None:
        with self._lock:
            self._over.clear()

    def update(self, **values: Any) -> None:
        with self._lock:
            self._over.update(values)

    def config(self) -> LlmConfig:
        with self._lock:
            over = dict(self._over)
        env = LlmConfig.from_env()
        return LlmConfig(
            base_url=over.get("base_url", env.base_url),
            api_key=over.get("api_key", env.api_key),
            timeout_s=float(over.get("timeout_s", env.timeout_s)),
            max_tokens=int(over.get("max_tokens", env.max_tokens)),
        )

    def raw(self, name: str, env_name: str) -> str:
        with self._lock:
            if name in self._over:
                return str(self._over[name])
        return os.environ.get(env_name, "").strip()


SETTINGS = ServerSettings()


def model_status() -> dict[str, Any]:
    """Report live-model wiring without ever revealing the key."""
    base_url = SETTINGS.raw("base_url", LLAMA_URL_ENV)
    key_set = bool(SETTINGS.raw("api_key", LLAMA_KEY_ENV))
    return {
        "model": DEFAULT_MODEL,
        "base_url": base_url,
        "api_key_set": key_set,
        "configured": bool(base_url and key_set),
        "timeout_s": SETTINGS.raw("timeout_s", LLAMA_TIMEOUT_ENV),
        "max_tokens": SETTINGS.raw("max_tokens", LLAMA_MAX_TOKENS_ENV),
    }


def apply_model_settings(body: Any) -> dict[str, Any]:
    """Store live-model settings in server memory (not in ``os.environ``)."""
    if not isinstance(body, dict):
        raise ConvertError("request body must be a JSON object")
    staged: dict[str, Any] = {}
    if "base_url" in body:
        base_url = body["base_url"]
        if not isinstance(base_url, str) or not base_url.strip():
            raise ConvertError("base_url must be a non-empty string")
        if not base_url.strip().startswith(("http://", "https://")):
            raise ConvertError("base_url must start with http:// or https://")
        try:
            staged["base_url"] = validate_base_url(base_url)
        except UnsafeBaseUrlError as exc:
            raise ConvertError(str(exc)) from exc
    if "api_key" in body:
        api_key = body["api_key"]
        if not isinstance(api_key, str) or not api_key.strip():
            raise ConvertError("api_key must be a non-empty string")
        staged["api_key"] = api_key.strip()
    for name, lo, hi in (("timeout_s", 1, 600), ("max_tokens", 1, 128000)):
        if name in body:
            value = body[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConvertError(f"{name} must be a number in {lo}..{hi}")
            if not lo <= float(value) <= hi:
                raise ConvertError(f"{name} must be a number in {lo}..{hi}")
            staged[name] = value
    SETTINGS.update(**staged)
    return model_status()

class ConvertHandler(http.server.SimpleHTTPRequestHandler):
    """Serve ui/ statically plus the convert/runs/odrl/settings API."""

    registry: RunRegistry | None = None

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send_json(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _db(self) -> Any:
        assert ConvertHandler.registry is not None
        return _db_conn(ConvertHandler.registry._root, ConvertHandler.registry._out_dir)

    def _read_json_body(self) -> tuple[dict[str, Any] | None, Any]:
        """Return (body, None) or (None, error payload) for a JSON POST."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            return None, {"error": "E_CONFIG", "hint": "body must be 1..65536 bytes"}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")), None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, {"error": "E_CONFIG", "hint": "body must be JSON"}

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not _authorized(self):
            self._send_json(
                {"error": "E_AUTH", "hint": "valid Bearer API key required"}, 401
            )
            return
        if parsed.path == "/api/settings":
            body, error = self._read_json_body()
            if error is not None:
                self._send_json(error, 400)
                return
            try:
                self._send_json(apply_model_settings(body))
            except ConvertError as exc:
                self._send_json({"error": "E_CONFIG", "hint": str(exc)}, 400)
            return
        if parsed.path != "/api/convert":
            self._send_json({"error": "E_CONFIG", "hint": "unknown endpoint"}, 404)
            return
        assert ConvertHandler.registry is not None
        body, error = self._read_json_body()
        if error is not None:
            self._send_json(error, 400)
            return
        try:
            nl_text, variant, seed, max_steps = validate_convert(body)
        except ConvertError as exc:
            self._send_json({"error": "E_CONFIG", "hint": str(exc)}, 400)
            return
        try:
            record = ConvertHandler.registry.start(
                nl_text, variant, seed, max_steps, tenant=_key_fingerprint(self)
            )
        except ConvertError as exc:
            self._send_json({"error": "E_BUSY", "hint": str(exc)}, 429)
            return
        self._send_json(record)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/") and not _authorized(self):
            self._send_json(
                {"error": "E_AUTH", "hint": "valid Bearer API key required"}, 401
            )
            return
        if parsed.path == "/api/settings":
            self._send_json(model_status())
            return
        if parsed.path == "/api/usage":
            conn = self._db()
            try:
                caller_fp = _key_fingerprint(self)
                rows = _usage_rows(
                    conn, None if caller_fp == "local" else caller_fp
                )
            finally:
                conn.close()
            self._send_json({"usage": rows})
            return
        caller_fp = _key_fingerprint(self)
        caller = None if caller_fp == "local" else caller_fp
        if parsed.path == "/api/runs":
            flags = _query_flags(urllib.parse.parse_qs(parsed.query))
            conn = self._db()
            try:
                runs = _list_runs(conn, flags, caller)
            finally:
                conn.close()
            self._send_json({"runs": runs})
            return
        if parsed.path.startswith("/api/runs/"):
            assert ConvertHandler.registry is not None
            run_key = parsed.path[len("/api/runs/") :]
            snapshot = ConvertHandler.registry.snapshot(run_key)
            if snapshot is None or (
                caller is not None and snapshot.get("tenant") != caller
            ):
                self._send_json({"error": "E_CONFIG", "hint": "unknown run_key"}, 404)
                return
            self._send_json(snapshot)
            return
        if parsed.path == "/api/odrl":
            flags = _query_flags(urllib.parse.parse_qs(parsed.query))
            conn = self._db()
            try:
                pairs = _list_pairs(conn, flags, caller)
            finally:
                conn.close()
            self._send_json({"pairs": pairs})
            return
        super().do_GET()


def serve_ui(port: int, bind: str, ui_dir: Path) -> int:
    """Serve the UI directory plus the API; migrate the DB; block on Ctrl+C."""
    from agentcrash.cli import EXIT_OK

    root = ui_dir.parent
    conn = _db_conn(root)
    conn.close()
    handler = functools.partial(ConvertHandler, directory=str(ui_dir))
    ConvertHandler.registry = RunRegistry(root)
    server = http.server.ThreadingHTTPServer((bind, port), handler)
    print(f"AgentCrash UI: http://{bind}:{port}/ (serving {ui_dir}, Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return EXIT_OK
