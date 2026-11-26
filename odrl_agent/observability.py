"""Request IDs, structured logs and Prometheus metrics for the HTTP service.

Stdlib only, so the current ``http.server`` front end and a later
FastAPI/Uvicorn one can share it. Nothing here ever logs a request body, an
model keys. Nothing sensitive is ever logged.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import defaultdict
from typing import Any

LOGGER_NAME = "nl2odrl.http"
REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_RUN_PATH = re.compile(r"^/api/runs/[^/]+(/events)?$")

log = logging.getLogger(LOGGER_NAME)

#: Histogram buckets (seconds). Wide enough for queue wait and run time.
LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 10.0, 30.0, 60.0, 120.0)
#: Service-level objective for queue wait, checked with
#: ``histogram_quantile(0.95, sum(rate(nl2odrl_queue_wait_seconds_bucket[5m])) by (le))``.
QUEUE_WAIT_P95_SLO_SECONDS = 5.0


def request_id(incoming: str | None) -> str:
    """Reuse a caller's well-formed request id, else mint one."""
    if incoming and _SAFE_REQUEST_ID.match(incoming):
        return incoming
    return uuid.uuid4().hex


def route_template(path: str) -> str:
    """Collapse per-run paths so metric labels stay low-cardinality."""
    if _RUN_PATH.match(path):
        return "/api/runs/{key}/events" if path.endswith("/events") else "/api/runs/{key}"
    if path.startswith("/api/"):
        return path
    return "/static"


def log_event(event: str, **fields: Any) -> None:
    """Emit one JSON log line (``event`` plus fields, sorted keys)."""
    if log.isEnabledFor(logging.INFO):
        log.info(json.dumps({"event": event, "ts": round(time.time(), 3), **fields},
                            sort_keys=True, default=str))  # fmt: skip


def configure_logging(level: int = logging.INFO) -> None:
    """Attach a plain stderr handler that prints the JSON lines as-is."""
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    body = ",".join(
        f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        for k, v in sorted(labels.items())
    )
    return "{" + body + "}"


class Metrics:
    """Thread-safe counters, sums and gauges rendered in Prometheus text format."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple], float] = {}
        self._help: dict[str, tuple[str, str]] = {}

    def _key(self, name: str, labels: dict[str, str] | None) -> tuple[str, tuple]:
        return name, tuple(sorted((labels or {}).items()))

    def describe(self, name: str, kind: str, text: str) -> None:
        self._help[name] = (kind, text)

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += value

    def set(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = value

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        """Record ``<name>_sum``/``_count`` plus cumulative ``_bucket{le=...}`` counts."""
        with self._lock:
            self._counters[self._key(f"{name}_sum", labels)] += seconds
            self._counters[self._key(f"{name}_count", labels)] += 1
            for bound in LATENCY_BUCKETS:
                if seconds <= bound:
                    self._counters[self._key(f"{name}_bucket", {**labels, "le": f"{bound:g}"})] += 1
            self._counters[self._key(f"{name}_bucket", {**labels, "le": "+Inf"})] += 1

    def quantile(self, name: str, q: float, **labels: str) -> float | None:
        """Bucket upper bound below which a fraction ``q`` of observations fall."""
        total = self.value(f"{name}_count", **labels)
        if not total:
            return None
        for bound in LATENCY_BUCKETS:
            if self.value(f"{name}_bucket", **labels, le=f"{bound:g}") >= q * total:
                return bound
        return float("inf")

    def value(self, name: str, **labels: str) -> float:
        key = self._key(name, labels)
        with self._lock:
            return self._counters.get(key, self._gauges.get(key, 0.0))

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()

    def render(self) -> str:
        with self._lock:
            rows = sorted(
                [(n, lbl, v) for (n, lbl), v in self._counters.items()]
                + [(n, lbl, v) for (n, lbl), v in self._gauges.items()]
            )
        out: list[str] = []
        seen: set[str] = set()
        for name, labels, value in rows:
            base = re.sub(r"_(sum|count|bucket)$", "", name)
            if base not in seen and base in self._help:
                kind, text = self._help[base]
                out.append(f"# HELP {base} {text}")
                out.append(f"# TYPE {base} {kind}")
                seen.add(base)
            out.append(f"{name}{_labels(dict(labels))} {value:g}")
        return "\n".join(out) + "\n"


METRICS = Metrics()
for _name, _kind, _text in (
    ("nl2odrl_http_requests_total", "counter", "HTTP requests by method, route and status."),
    ("nl2odrl_http_request_duration_seconds", "histogram", "HTTP request latency."),
    ("nl2odrl_runs_active", "gauge", "Conversions executing now."),
    ("nl2odrl_runs_queued", "gauge", "Conversions waiting for a worker."),
    ("nl2odrl_runs_retained", "gauge", "Run records held in memory."),
    ("nl2odrl_runs_rejected_total", "counter", "Conversions refused with 429."),
    ("nl2odrl_runs_finished_total", "counter", "Conversions finished, by status."),
    ("nl2odrl_runs_evicted_total", "counter", "Finished run records evicted from memory."),
    ("nl2odrl_run_duration_seconds", "histogram", "Conversion execution time."),
    ("nl2odrl_queue_wait_seconds", "histogram",
     f"Time a conversion waited for a worker (SLO: p95 <= {QUEUE_WAIT_P95_SLO_SECONDS:g}s)."),
    ("nl2odrl_model_tokens_total", "counter", "Model tokens consumed, by kind."),
    ("nl2odrl_model_calls_total", "counter", "Model calls made by conversions."),
):
    METRICS.describe(_name, _kind, _text)
