"""Baseline v0: run the current inline pipeline over the frozen case file.

Usage (key via env, never written to disk):
    $env:LLAMA_API_KEY=...; $env:LLAMA_BASE_URL=...
    python scripts/baseline_v0.py [--limit N] [--out benchmarks/baseline_v0_results.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path

from odrl_agent.adapter import DEFAULT_MODEL
from odrl_agent.web import SETTINGS, build_inline_run, run_and_store

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--variant", default="fixed")
    ap.add_argument("--out", default="benchmarks/baseline_v0_results.json")
    args = ap.parse_args()
    cases_doc = json.loads((ROOT / "benchmarks/baseline_v0_cases.json").read_text("utf-8"))
    cases = cases_doc["cases"][: args.limit or None]
    cfg = SETTINGS.config()
    if not (cfg.base_url and cfg.api_key):
        print("set LLAMA_BASE_URL and LLAMA_API_KEY")
        return 2
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    pack = (ROOT / "prompts/pack-probe-v1.json").read_bytes()
    partial = ROOT / (args.out + ".partial")
    results = json.loads(partial.read_text("utf-8")) if partial.exists() else []
    seen = {r["id"] for r in results}
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        for i, case in enumerate(cases, 1):
            if case["id"] in seen:
                continue
            spec = build_inline_run(case["nl"], args.variant, 42, 12, ROOT, i, out_dir)
            t0 = time.monotonic()
            snap = run_and_store(spec, f"base-{i:03d}")
            wall = time.monotonic() - t0
            verdict = snap.get("verdict") or {}
            failed = [r["id"] for r in verdict.get("results", []) if not r["passed"]]
            row = {
                "id": case["id"],
                "tier": case["tier"],
                "status": snap["status"],
                "verdict": verdict.get("verdict"),
                "failed_invariants": failed,
                "model_calls": snap.get("model_calls"),
                "usage": snap.get("usage_total"),
                "wall_s": round(wall, 2),
                "error": snap.get("error"),
            }
            results.append(row)
            partial.write_text(json.dumps(results), "utf-8")
            print(i, row["id"], row["status"], row["verdict"], row["model_calls"], row["wall_s"])
    done = [r for r in results if r["status"] == "done"]
    walls = sorted(r["wall_s"] for r in results)
    summary = {
        "n": len(results),
        "done": len(done),
        "errors": len(results) - len(done),
        "pass": sum(1 for r in done if r["verdict"] == "PASS"),
        "median_model_calls": sorted(r["model_calls"] or 0 for r in results)[len(results) // 2],
        "p50_wall_s": walls[len(walls) // 2],
        "p95_wall_s": walls[min(len(walls) - 1, int(len(walls) * 0.95))],
        "total_tokens": sum(
            (r["usage"] or {}).get("total_tokens", 0) if isinstance(r["usage"], dict) else 0
            for r in results
        ),
    }
    report = {
        "baseline": "v0",
        "model": DEFAULT_MODEL,
        "base_url_host": cfg.base_url.split("//")[-1].split("/")[0],
        "commit_sha": sha,
        "prompt_pack_sha256": hashlib.sha256(pack).hexdigest()[:16],
        "cases_sha": cases_doc["sha"],
        "variant": args.variant,
        "summary": summary,
        "results": results,
    }
    Path(ROOT / args.out).write_text(json.dumps(report, indent=1) + "\n", "utf-8")
    partial.unlink(missing_ok=True)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
