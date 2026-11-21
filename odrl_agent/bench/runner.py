"""Run the pipeline over the benchmark and build the report."""
from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from odrl_agent.bench import gates, generate, metrics, pins
from odrl_agent.extractor import Extractor, ModelRoute
from odrl_agent.grounder import Grounder
from odrl_agent.llm import LlmError, StructuredLlm
from odrl_agent.pipeline import PolicyDraft, convert

LlmFactory = Callable[[], StructuredLlm]


def load_cases(directory: Path = generate.BENCH_DIR) -> list[dict[str, Any]]:
    """Read ``cases.json`` and refuse a file that does not match its manifest."""
    doc = json.loads((directory / "cases.json").read_text("utf-8"))
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    cases = doc["cases"]
    if generate.cases_sha(cases) != manifest["cases_sha"]:
        raise ValueError("benchmarks/v1/cases.json does not match manifest.json cases_sha; "
                         "regenerate with `python -m odrl_agent.bench generate`")
    return cases


def run_case(case: dict[str, Any], factory: LlmFactory, model: str | None) -> dict[str, Any]:
    llm = factory()
    route = ModelRoute(model, model) if model else ModelRoute()
    started = time.monotonic()
    error = None
    try:
        # trusted_parties=[]: a party counts as grounded only if the text names it.
        draft, _doc = convert(case["nl"], Extractor(llm, route), grounder=Grounder(
            trusted_parties=[]))
    except LlmError as exc:
        error = f"{type(exc).__name__}: {exc}"
        draft = PolicyDraft("failed", None, None, False, [error], [], [])
    wall = time.monotonic() - started
    return {"case": case, "draft": asdict(draft), "calls": llm.usage.calls,
            "tokens": llm.usage.as_dict().get("total_tokens", 0),
            "wall_s": round(wall, 3), "error": error}


def run_all(cases: list[dict[str, Any]], factory: LlmFactory, *, model: str | None = None,
            workers: int = 1) -> list[dict[str, Any]]:
    if workers <= 1:
        return [run_case(c, factory, model) for c in cases]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda c: run_case(c, factory, model), cases))


def _git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                             cwd=generate.ROOT, timeout=10, check=False)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


ENFORCE_MODES = ("all", "regression")


def report(results: list[dict[str, Any]], *, mode: str, model: str,
           baseline: dict[str, Any] | None = None, enforce: str = "all") -> dict[str, Any]:
    """Metrics, gate rows and an identity block that makes the run reproducible.

    ``enforce="all"`` fails on any gate. ``enforce="regression"`` (plan section 4.3,
    per-PR live check) fails only on the baseline comparison; the absolute gates are
    still computed and listed with ``enforced: False`` so the report stays honest.
    """
    if enforce not in ENFORCE_MODES:
        raise ValueError(f"enforce must be one of {ENFORCE_MODES}")
    if enforce == "regression" and baseline is None:
        raise ValueError("enforce='regression' needs a baseline to compare against")
    live = mode == "live"
    overall = metrics.compute(results)
    rows = gates.evaluate(overall, live=live)
    for row in rows:
        row["enforced"] = enforce == "all"
    if baseline is not None:
        regress = gates.regressions(overall, baseline["metrics"])
        for row in regress:
            row["enforced"] = True
        rows += regress
    manifest = json.loads((generate.BENCH_DIR / "manifest.json").read_text("utf-8"))
    return {
        "benchmark_version": manifest["version"],
        "cases_sha": manifest["cases_sha"],
        "mode": mode,
        "model": model,
        "commit_sha": _git_sha(),
        "pins": pins.current(),
        "metrics": overall,
        "tiers": metrics.by_tier(results),
        "gates": rows,
        "enforce": enforce,
        "passed": not gates.failed([r for r in rows if r["enforced"]]),
        "total_tokens": sum(r["tokens"] for r in results),
        "failures": [
            {"id": r["case"]["id"], "tier": r["case"]["tier"], "status": r["draft"]["status"],
             "false_permissions": [list(x) for x in metrics.false_permissions(r)],
             "error": r["error"]}
            for r in results
            if metrics.false_permissions(r) or r["error"] or r["draft"]["status"] == "failed"
        ],
    }
