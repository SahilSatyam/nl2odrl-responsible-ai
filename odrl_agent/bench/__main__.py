"""``python -m odrl_agent.bench <generate|verify|pins|run>``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from odrl_agent.bench import generate, oracle, pins, runner


def _print_gates(rep: dict) -> None:
    for row in rep["gates"]:
        if not row.get("enforced", True):
            mark = "info" if row["passed"] else "MISS"  # reported, not blocking
        else:
            mark = "ok  " if row["passed"] else "FAIL"
        base = f" (baseline {row['baseline']})" if "baseline" in row else ""
        print(f"{mark} {row['metric']:<42} {row['value']!s:<10} {row['rule']}{base}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="odrl_agent.bench")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="rewrite benchmarks/v1 cases + manifest")
    sub.add_parser("verify", help="check cases.json against manifest.json")
    p_pins = sub.add_parser("pins", help="check or rewrite the model/prompt pins")
    p_pins.add_argument("--write", action="store_true")
    run = sub.add_parser("run", help="run the benchmark and enforce the gates")
    run.add_argument("--mode", choices=("offline", "live"), default="offline")
    run.add_argument("--mutant", choices=oracle.MUTANTS, help="offline only: break the oracle")
    run.add_argument("--split", choices=("dev", "test"))
    run.add_argument("--tier", type=int, action="append")
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--baseline", help="frozen baseline report to compare against")
    run.add_argument("--enforce", choices=runner.ENFORCE_MODES, default="all",
                     help="'regression' fails only on the baseline comparison (needs --baseline)")
    run.add_argument("--out", help="write the JSON report here")
    run.add_argument("--freeze", help="live only: write this run as the frozen baseline")
    args = ap.parse_args(argv)

    if args.cmd == "generate":
        manifest = generate.write()
        print(f"wrote {manifest['total']} cases, sha {manifest['cases_sha']}")
        return 0
    if args.cmd == "verify":
        cases = runner.load_cases()
        print(f"ok: {len(cases)} cases match the manifest")
        return 0
    if args.cmd == "pins":
        if args.write:
            print(json.dumps(pins.write(), indent=1))
            return 0
        moved = pins.drift()
        for key, (old, new) in moved.items():
            print(f"pin moved: {key}: {old} -> {new}")
        return 1 if moved else 0

    cases = runner.load_cases()
    if args.split:
        cases = [c for c in cases if c["split"] == args.split]
    if args.tier:
        cases = [c for c in cases if c["tier"] in args.tier]
    if args.limit:
        cases = cases[: args.limit]
    if args.mode == "offline":
        model = "oracle" + (f"+{args.mutant}" if args.mutant else "")
        everything = runner.load_cases()

        def factory():
            return oracle.oracle_llm(everything, args.mutant)

        results = runner.run_all(cases, factory, workers=args.workers)
    else:
        from odrl_agent.adapter import LlmConfig
        from odrl_agent.extractor import DEFAULT_MODEL
        from odrl_agent.llm import StructuredLlm

        config = LlmConfig.from_env()
        if not (config.base_url and config.api_key):
            print("live mode needs LLAMA_BASE_URL and LLAMA_API_KEY", file=sys.stderr)
            return 2
        model = DEFAULT_MODEL
        results = runner.run_all(cases, lambda: StructuredLlm.from_config(config), model=model,
                                 workers=args.workers)
    baseline = json.loads(Path(args.baseline).read_text("utf-8")) if args.baseline else None
    if args.enforce == "regression" and baseline is None:
        print("--enforce regression needs --baseline", file=sys.stderr)
        return 2
    rep = runner.report(results, mode=args.mode, model=model, baseline=baseline,
                        enforce=args.enforce)
    _print_gates(rep)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(rep, indent=1) + "\n", "utf-8")
    if args.freeze and args.mode == "live":
        Path(args.freeze).write_text(json.dumps(rep, indent=1) + "\n", "utf-8")
        print(f"froze baseline -> {args.freeze}")
    print("PASS" if rep["passed"] else "FAIL")
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
