"""Service core for NL2ODRL Studio: run queue, storage, model settings.

The HTTP layer is :mod:`odrl_agent.app` (FastAPI on Uvicorn); this module
holds what it calls: :class:`RunRegistry` (bounded worker pool + queue with
honest ``Retry-After``, TTL/size-bounded run memory), ``run_and_store``
(executes one conversion and records it in ``agentcrash.db``), process-local
:class:`ServerSettings`, and the run/policy list queries. Finished runs are
recorded in SQLite; the trace JSONL files remain the immutable artifact layer.
"""

from __future__ import annotations

import collections
import copy
import json
import math
import os
import threading
import time
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

import odrl_agent.verifiers  # noqa: F401 -- registers ODRL invariants
from odrl_agent import db as db_module
from odrl_agent.adapter import (
    DEFAULT_MODEL,
    LLAMA_KEY_ENV,
    LLAMA_MAX_TOKENS_ENV,
    LLAMA_TIMEOUT_ENV,
    LLAMA_URL_ENV,
    LlmConfig,
    ModelNotConfiguredError,
)
from odrl_agent.netguard import UnsafeBaseUrlError, validate_base_url
from odrl_agent.observability import (
    METRICS,
    configure_logging,
    log_event,
)
from odrl_agent.odrl import (
    contains_attacker_grant,
    validate_odrl,
)
from odrl_agent.pipeline import run_conversion
from odrl_agent.remediation import reference_patch_for

INLINE_DOC_ID = "pasted-request-1"

#: Retired with the ReAct loop; the pipeline needs no task guidance.
INLINE_GUIDANCE = ""

BASE_SCENARIO = "odrl_convert"

NAIVE_POLICY = "policies/default.yaml"
FIXED_POLICY = "policies/fixed-odrl-convert.yaml"

MAX_NL_CHARS = 4000
#: Run-queue defaults; each is overridable by env (NL2ODRL_WORKERS,
#: NL2ODRL_QUEUE_DEPTH, NL2ODRL_RUN_TTL seconds, NL2ODRL_MAX_RETAINED).
DEFAULT_WORKERS = 4
DEFAULT_QUEUE_DEPTH = 16
DEFAULT_RUN_TTL = 900
DEFAULT_MAX_RETAINED = 256
#: SSE stream limits: poll interval, heartbeat and hard cap per stream.
SSE_POLL_SECONDS = 0.25
SSE_HEARTBEAT_SECONDS = 15.0
SSE_MAX_SECONDS = 600.0

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
        valid, errors = validate_odrl(value)
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
        "draft": _draft_from(events),
        "request_id": spec.get("request_id"),
        "_virtual_ms": virtual_ms,
    }


def _draft_from(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        detail = event.get("detail") or {}
        if detail.get("phase") == "run_end":
            draft = detail.get("draft")
            return draft if isinstance(draft, dict) else None
    return None


def _record_snapshot(
    db_path: str, run_key: str, snap: dict[str, Any], spec: dict[str, Any]
) -> None:
    conn = db_module.connect(Path(db_path))
    try:
        db_module.migrate(conn)
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


def _scripted_transport(replies: list[dict[str, Any]]) -> Any:
    """httpx transport serving canned structured replies (offline seed runs only).

    Each reply is a ``PolicyIR``-shaped object returned as one chat
    completion. Nothing global is patched: the transport is injected into
    this run's ``LlmConfig``.
    """
    import httpx

    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        if not queue:
            return httpx.Response(400, text="scripted replies exhausted")
        content = json.dumps(queue.pop(0))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        )

    return httpx.MockTransport(handler)


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
            transport=_scripted_transport(actions),
        )
    else:
        llm_config = SETTINGS.config()
    try:
        Path(spec["trace_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(spec["state_path"]).parent.mkdir(parents=True, exist_ok=True)
        run_conversion(
            scenario=spec["scenario"],
            policy=spec["policy"],
            seed=spec["seed"],
            trace_path=spec["trace_path"],
            state_path=spec["state_path"],
            scenario_text=spec["scenario_text"],
            policy_text=spec["policy_text"],
            llm_config=llm_config,
            request_id=spec.get("request_id"),
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
    return conn


def _list_runs(conn: Any, flags: dict[str, Any]) -> list[dict[str, Any]]:
    """Run summaries, newest first."""
    return db_module.list_runs(
        conn,
        scenario=flags["scenario"],
        verdict=flags["verdict"],
        status=flags["status"],
        limit=flags["limit"],
    )


def _list_pairs(conn: Any, flags: dict[str, Any]) -> list[dict[str, Any]]:
    """NL-to-ODRL pairs, newest first."""
    return db_module.list_odrl_docs(
        conn,
        scenario=flags["scenario"],
        valid=flags["valid"],
        attacker=flags["attacker"],
        limit=flags["limit"],
    )


def _usage_rows(conn: Any) -> list[dict[str, Any]]:
    """Local run/token totals, one row."""
    row = conn.execute(
        "SELECT COUNT(*) AS runs,"
        " COALESCE(SUM(model_calls), 0) AS model_calls,"
        " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
        " COALESCE(SUM(total_tokens), 0) AS total_tokens"
        " FROM runs"
    ).fetchone()
    return [{"key_fp": "local", **{k: row[k] for k in row.keys()}}]


class BusyError(ConvertError):
    """Run queue is full; ``retry_after`` is an honest estimate in seconds."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(low, min(high, value))


class RunRegistry:
    """Bounded worker pool + queue for background convert runs.

    ``workers`` runs execute at once; up to ``queue_depth`` more wait in FIFO
    order. Beyond that ``start`` raises :class:`BusyError` with a
    ``Retry-After`` estimate from the mean run time. Finished records stay in
    memory for ``ttl`` seconds (at most ``max_retained`` of them); after that
    :meth:`snapshot` serves the run from the database, so memory stays bounded
    however many runs the server has seen.
    """

    def __init__(
        self,
        root: Path,
        out_dir: Path | None = None,
        *,
        workers: int | None = None,
        queue_depth: int | None = None,
        ttl: float | None = None,
        max_retained: int | None = None,
    ) -> None:
        self._root = root
        self._out_dir = out_dir
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._queue: collections.deque[str] = collections.deque()
        self._seq = 0
        self._active = 0
        self.workers = workers or _env_int("NL2ODRL_WORKERS", DEFAULT_WORKERS, 1, 64)
        self.queue_depth = (
            queue_depth if queue_depth is not None
            else _env_int("NL2ODRL_QUEUE_DEPTH", DEFAULT_QUEUE_DEPTH, 0, 1024)
        )  # fmt: skip
        self.ttl = ttl if ttl is not None else float(
            _env_int("NL2ODRL_RUN_TTL", DEFAULT_RUN_TTL, 1, 86400)
        )
        self.max_retained = max_retained or _env_int(
            "NL2ODRL_MAX_RETAINED", DEFAULT_MAX_RETAINED, 1, 100000
        )
        self._mean_seconds = 10.0  # EWMA of run time; seeds Retry-After

    # ---- bookkeeping (call with the lock held) ---------------------------
    def _gauges(self) -> None:
        METRICS.set("nl2odrl_runs_active", self._active)
        METRICS.set("nl2odrl_runs_queued", len(self._queue))
        METRICS.set("nl2odrl_runs_retained", len(self._runs))

    def _evict(self) -> None:
        now = time.monotonic()
        finished = [
            (rec["finished_at"], key)
            for key, rec in self._runs.items()
            if rec.get("finished_at") is not None
        ]
        finished.sort()
        expired = [key for at, key in finished if now - at > self.ttl]
        overflow = len(finished) - len(expired) - self.max_retained
        if overflow > 0:
            keep = [key for _at, key in finished if key not in set(expired)]
            expired.extend(keep[:overflow])
        for key in expired:
            self._runs.pop(key, None)
        if expired:
            METRICS.inc("nl2odrl_runs_evicted_total", len(expired))

    def _retry_after(self) -> int:
        waiting = len(self._queue) + 1
        return max(1, math.ceil(self._mean_seconds * waiting / self.workers))

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    @property
    def queued(self) -> int:
        with self._lock:
            return len(self._queue)

    def retained(self) -> int:
        with self._lock:
            self._evict()
            return len(self._runs)

    # ---- lifecycle -------------------------------------------------------
    def start(
        self,
        nl_text: str,
        variant: str,
        seed: int,
        max_steps: int,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Run now if a worker is free, else queue; return the public record."""

        def full() -> bool:
            return self._active >= self.workers and len(self._queue) >= self.queue_depth

        def reject() -> BusyError:
            METRICS.inc("nl2odrl_runs_rejected_total")
            retry = self._retry_after()
            return BusyError(
                f"{self._active} conversions running and {len(self._queue)} queued; "
                f"retry in about {retry}s",
                retry,
            )

        with self._lock:
            self._evict()
            if full():
                raise reject()
            self._seq += 1
            attempt = self._seq
        spec = build_inline_run(
            nl_text, variant, seed, max_steps, self._root, attempt, self._out_dir,
        )
        spec["request_id"] = request_id
        run_key = f"{spec['run_id']}-{attempt}"
        now = time.monotonic()
        with self._lock:
            # Re-check: another start may have taken the last seat meanwhile.
            if full():
                raise reject()
            run_now = self._active < self.workers
            status = "running" if run_now else "queued"
            self._runs[run_key] = {
                "run_key": run_key,
                "status": status,
                "spec": spec,
                "request_id": request_id,
                "queued_at": now,
                "started_at": now if run_now else None,
                "finished_at": None,
                "snapshot": None,
            }
            if run_now:
                self._active += 1
            else:
                self._queue.append(run_key)
            self._gauges()
        log_event("run_" + status, run_key=run_key, request_id=request_id,
                  trace_path=spec["trace_path"])  # fmt: skip
        if run_now:
            METRICS.observe("nl2odrl_queue_wait_seconds", 0.0)
            threading.Thread(target=self._worker, args=(run_key,), daemon=True).start()
        return {"run_key": run_key, "run_id": spec["run_id"], "status": status}

    def _worker(self, run_key: str) -> None:
        """Execute ``run_key``, then keep draining the queue on this thread."""
        current: str | None = run_key
        while current is not None:
            self._execute(current)
            with self._lock:
                current = None
                while self._queue:
                    candidate = self._queue.popleft()
                    record = self._runs.get(candidate)
                    if record is not None:
                        current = candidate
                        record["status"] = "running"
                        record["started_at"] = time.monotonic()
                        METRICS.observe(
                            "nl2odrl_queue_wait_seconds",
                            record["started_at"] - record["queued_at"],
                        )
                        break
                if current is None:
                    self._active -= 1
                self._gauges()

    def _execute(self, run_key: str) -> None:
        with self._lock:
            record = self._runs[run_key]
            spec = record["spec"]
            started = record["started_at"] or time.monotonic()
        try:
            snap = run_and_store(spec, run_key)
        except Exception as exc:
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
        elapsed = time.monotonic() - started
        usage = snap.get("usage_total") or {}
        for kind in ("prompt_tokens", "completion_tokens"):
            if usage.get(kind):
                METRICS.inc("nl2odrl_model_tokens_total", float(usage[kind]), kind=kind)
        if snap.get("model_calls"):
            METRICS.inc("nl2odrl_model_calls_total", float(snap["model_calls"]))
        METRICS.observe("nl2odrl_run_duration_seconds", elapsed)
        METRICS.inc("nl2odrl_runs_finished_total", status=str(snap.get("status")))
        with self._lock:
            record["status"] = snap["status"]
            record["snapshot"] = snap
            record["finished_at"] = time.monotonic()
            self._mean_seconds = 0.8 * self._mean_seconds + 0.2 * max(elapsed, 0.1)
        log_event("run_finished", run_key=run_key, request_id=record.get("request_id"),
                  status=snap.get("status"), seconds=round(elapsed, 3))  # fmt: skip

    def snapshot(self, run_key: str) -> dict[str, Any] | None:
        """Live snapshot while queued/running, retained or DB snapshot once finished."""
        with self._lock:
            self._evict()
            record = self._runs.get(run_key)
        if record is not None and record["status"] in ("running", "queued"):
            spec = record["spec"]
            events = read_events(spec["trace_path"]) if record["status"] == "running" else []
            usage_total, model_calls = sum_usage(events)
            begun = record["started_at"] or record["queued_at"]
            return {
                "run_key": run_key,
                "status": record["status"],
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
                "wall_seconds": time.monotonic() - begun,
                "error": None,
            }
        if record is not None and record["snapshot"] is not None:
            return record["snapshot"]
        conn = _db_conn(self._root, self._out_dir)
        try:
            return db_module.get_run(conn, run_key)
        finally:
            conn.close()

    def wait(self, run_key: str, timeout: float = 120.0) -> dict[str, Any] | None:
        """Block until a run leaves 'queued'/'running' (test helper); else None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.snapshot(run_key)
            if snapshot is None or snapshot["status"] not in ("running", "queued"):
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
    """Live-model settings held in server memory, never in ``os.environ``.

    A single local process owns one endpoint override set. The process
    environment is the read-only deployment default.
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
    """Report the live-model wiring without ever revealing the key."""
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
    """Store live-model settings in server memory (not ``os.environ``)."""
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

def serve_ui(port: int, bind: str, ui_dir: Path) -> int:
    """Serve the UI and the API with Uvicorn (FastAPI app); block on Ctrl+C."""
    import uvicorn
    from agentcrash.cli import EXIT_OK

    from odrl_agent.app import create_app

    root = ui_dir.parent
    conn = _db_conn(root)
    conn.close()
    configure_logging()
    app = create_app(root, ui_dir=ui_dir)
    print(f"NL2ODRL Studio: http://{bind}:{port}/ (serving {ui_dir}, Ctrl+C to stop)")
    uvicorn.run(app, host=bind, port=port, log_level="warning", access_log=False,
                proxy_headers=False)  # fmt: skip
    return EXIT_OK
