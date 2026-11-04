"""ODRL host command-line entry point: ``odrl-host``.

Host-owned commands that drive the NL-to-ODRL engine (``odrl_agent``).
The framework CLI (``agentcrash``: check/serve) stays generic and never
imports this module; diagnose/replay live here because they drive the
host's scenario loop and policy patches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from agentcrash.cli import (
    EXIT_CORRUPT,
    EXIT_OK,
    EXIT_POLICY_REJECTED,
    EXIT_RUNABORTED,
    EXIT_USAGE,
    EXIT_VERDICT_FAIL,
    ConfigError,
    load_yaml,
    resolve_policy,
    resolve_scenario,
    validate_run_inputs,
)
from agentcrash.injector import UnknownFaultError
from agentcrash.policy import (
    PolicyError,
    PolicyRejectedError,
    apply_patch,
    diff_policies,
    fetch_live_patch,
    load_patch_doc,
    load_policy,
    reference_patch_for,
    validate_patch_data,
)
from agentcrash.tracer import compute_run_id
from agentcrash.verifiers import (
    TraceCorruptError,
    UnknownVerifierError,
    VerifierError,
    load_state_snapshot,
    load_trace_events,
    run_verifiers,
)

import odrl_agent.verifiers  # noqa: F401 -- registers ODRL invariants
from odrl_agent import __version__
from odrl_agent.adapter import (
    DEFAULT_MODEL,
    ModelNotConfiguredError,
    run_react_loop,
)
from odrl_agent.dspy_opt import PackRejectedError, load_pack
from odrl_agent.tools import PARSE_POLICY, SAVE_ODRL, render_untrusted


def cmd_run(argv: list[str]) -> int:
    """Execute `odrl-host run` (exit 0 completed, 2 usage/config, 5 runaway)."""
    parser = argparse.ArgumentParser(prog="odrl-host run")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--policy", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument(
        "--instruction-pack",
        default=None,
        help="adopt a versioned prompt pack (prompts/pack-vN.json) as the "
        "conversion guidance for this run; verification is unchanged",
    )
    parser.add_argument("--trace-out", default=None)
    parser.add_argument("--state-out", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    def fail(code: str, hint: str, exit_code: int) -> int:
        payload = {"code": code, "exit": exit_code, "hint": hint, "command": "run"}
        if args.json:
            print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        else:
            print(f"odrl-host run: {code}: {hint}", file=sys.stderr)
        return exit_code

    try:
        scenario_path = resolve_scenario(args.scenario)
        scenario, scenario_text = load_yaml(scenario_path)
        policy_path = resolve_policy(args.policy, scenario_path, scenario)
        policy, policy_text = load_yaml(policy_path)
        file_seed, file_steps = validate_run_inputs(scenario, policy)
    except ConfigError as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)
    seed = args.seed if args.seed is not None else file_seed
    max_steps = args.max_steps if args.max_steps is not None else file_steps
    pack_instruction: str | None = None
    run_id_input = scenario_text
    if args.instruction_pack is not None:
        try:
            pack = load_pack(args.instruction_pack)
        except PackRejectedError as exc:
            return fail("E_CONFIG", str(exc), EXIT_USAGE)
        if not pack["trainset_match"]:
            print(
                "odrl-host run: warning: prompt pack trainset_hash "
                f"{pack['trainset_hash']!r} does not match the frozen trainset; "
                "proceeding with explicit adoption",
                file=sys.stderr,
            )
        pack_instruction = pack["instruction"]
        digest = hashlib.sha256(pack_instruction.encode("utf-8")).hexdigest()[:16]
        run_id_input = f"{scenario_text}\ninstruction_sha256: {digest}"
    run_id = compute_run_id(run_id_input, policy_text, seed)
    trace_out = args.trace_out or str(Path("traces") / f"{run_id}.jsonl")
    state_out = args.state_out or str(Path("reports") / f"{run_id}.state.json")

    if args.dry_run:
        plan = {
            "run_id": run_id,
            "scenario": scenario.get("scenario"),
            "task": scenario.get("task"),
            "seed": seed,
            "max_steps": max_steps,
            "model": args.model,
            "adapter": "live (LLAMA_BASE_URL)",
            "tools": [t.get("name") for t in scenario.get("tools", [])],
            "faults": scenario.get("faults", []),
            "policy": str(policy_path),
            "instruction_pack": args.instruction_pack,
            "trace_out": trace_out,
        }
        if args.json:
            print(json.dumps(plan, sort_keys=True, indent=2))
        else:
            print(f"run_id={run_id} scenario={plan['scenario']} seed={seed} steps={max_steps}")
            print(f"task: {plan['task']}")
            print(f"tools: {', '.join(str(t) for t in plan['tools'])}")
            print(f"faults: {plan['faults']}")
            print(f"policy: {plan['policy']} model: {args.model} (live via LLAMA_BASE_URL)")
            print(f"trace: {trace_out} (dry-run: nothing written)")
        return EXIT_OK

    try:
        Path(trace_out).parent.mkdir(parents=True, exist_ok=True)
        Path(state_out).parent.mkdir(parents=True, exist_ok=True)
        summary = run_react_loop(
            scenario=scenario,
            policy=policy,
            seed=seed,
            trace_path=trace_out,
            state_path=state_out,
            scenario_text=scenario_text,
            policy_text=policy_text,
            max_steps=max_steps,
            model=args.model,
            convert_instruction=pack_instruction,
        )
    except UnknownFaultError as exc:
        return fail("E_SCHEMA", str(exc), EXIT_USAGE)
    except ModelNotConfiguredError as exc:
        return fail("E_MODEL_API", str(exc), EXIT_RUNABORTED)
    except (ValueError, OSError) as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)

    if args.json:
        print(
            json.dumps(
                {
                    "api_version": "v1",
                    "trace": summary["trace_path"],
                    "status": summary["status"],
                    "steps": summary["steps"],
                    "scenario": summary["scenario"],
                    "run_id": summary["run_id"],
                    "ledger_hash": summary["ledger_hash"],
                },
                sort_keys=True,
            )
        )
    else:
        print(
            f"run_id={summary['run_id']} scenario={summary['scenario']} "
            f"steps={summary['steps']} trace={summary['trace_path']}"
        )
        print(
            f"status={summary['status']} "
            f"ledger_hash={summary['ledger_hash']} saves={summary['saves']}"
        )
        for span in _retrieved_doc_spans(trace_out):
            print(
                f"nl {span['doc_id']} ({span['bytes']} bytes, "
                f"untrusted_source=true, {span['source']}):"
            )
            print(render_untrusted(span["text"], span["source"]))
    return EXIT_RUNABORTED if summary["status"] == "runaway" else EXIT_OK


def _retrieved_doc_spans(trace_path: str) -> list[dict[str, Any]]:
    """Collect parsed-NL spans for delimiter rendering (doc 11 S3)."""
    try:
        events = load_trace_events(trace_path)
    except TraceCorruptError:
        return []
    spans: list[dict[str, Any]] = []
    for event in events:
        if event.get("tool") != PARSE_POLICY:
            continue
        if event.get("detail", {}).get("phase") != "tool_result":
            continue
        data = event.get("detail", {}).get("data", {}) or {}
        if "text" not in data:
            continue
        spans.append(
            {
                "doc_id": data.get("doc_id", "?"),
                "text": str(data.get("text", "")),
                "bytes": data.get("bytes", 0),
                "source": str(data.get("fixture", "")),
            }
        )
    return spans


def cmd_seed(argv: list[str]) -> int:
    """Seed the studio database with scripted fixture-backed runs (offline)."""
    parser = argparse.ArgumentParser(prog="odrl-host seed")
    parser.parse_args(argv)

    from odrl_agent.seed import seed_database
    from odrl_agent.web import find_repo_root

    root = find_repo_root()
    if root is None:
        print(
            "odrl-host seed: E_CONFIG: scenarios/ not found (run from the repo root)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    keys = seed_database(root)
    print(f"seeded {len(keys)} runs into {root / 'agentcrash.db'}")
    return EXIT_OK


def cmd_optimize_prompts(argv: list[str]) -> int:
    """Run the budgeted DSPy conversion-instruction probe (live model)."""
    parser = argparse.ArgumentParser(prog="odrl-host optimize-prompts")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-live-calls", type=int, default=24)
    parser.add_argument("--val-size", type=int, default=3)
    parser.add_argument("--compile-rows", type=int, default=4)
    parser.add_argument("--bsize", type=int, default=2)
    parser.add_argument("--num-candidates", type=int, default=2)
    parser.add_argument("--max-steps", type=int, default=1)
    parser.add_argument("--max-demos", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default="prompts/pack-v1.json")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.max_live_calls < 1:
        parser.error("--max-live-calls must be >= 1")

    def fail(code: str, hint: str, exit_code: int) -> int:
        payload = {"code": code, "exit": exit_code, "hint": hint, "command": "optimize-prompts"}
        if args.json:
            print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        else:
            print(f"odrl-host optimize-prompts: {code}: {hint}", file=sys.stderr)
        return exit_code

    from odrl_agent.adapter import LLAMA_KEY_ENV, LLAMA_URL_ENV, live_timeout_s
    from odrl_agent.dspy_opt import (
        ProbeBudgetExceeded,
        run_probe,
        write_pack,
    )

    model = args.model or DEFAULT_MODEL
    api_key = os.environ.get(LLAMA_KEY_ENV, "").strip()
    api_base = os.environ.get(LLAMA_URL_ENV, "").strip()
    if not api_key or not api_base:
        return fail(
            "E_MODEL_API",
            f"live model {model!r} requires {LLAMA_KEY_ENV} and {LLAMA_URL_ENV}; "
            "refusing live probe",
            EXIT_RUNABORTED,
        )
    try:
        report = run_probe(
            model=model,
            api_base=api_base,
            api_key=api_key,
            timeout_s=live_timeout_s(),
            val_size=args.val_size,
            compile_rows=args.compile_rows,
            bsize=args.bsize,
            num_candidates=args.num_candidates,
            max_steps=args.max_steps,
            max_demos=args.max_demos,
            max_live_calls=args.max_live_calls,
            seed=args.seed,
        )
    except ProbeBudgetExceeded as exc:
        return fail("E_BUDGET", str(exc), EXIT_RUNABORTED)
    print(
        json.dumps(
            {k: report[k] for k in ("model", "baseline_val", "optimized_val", "live_calls")},
            indent=2,
            sort_keys=True,
        )
    )
    try:
        pack_path = write_pack(report, args.out, force=args.force)
    except FileExistsError as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)
    if pack_path is None:
        print("no improvement over baseline; no prompt pack written")
    else:
        print(f"wrote prompt pack {pack_path}")
    return EXIT_OK


def cmd_serve(argv: list[str]) -> int:
    """Serve the ODRL/Policy Studio (reads ui/, local only)."""
    import argparse

    from odrl_agent.web import find_repo_root, serve_ui

    parser = argparse.ArgumentParser(prog="odrl-host serve")
    parser.add_argument("--port", type=int, default=8901)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args(argv)

    root = find_repo_root()
    if root is None:
        print(
            "odrl-host serve: E_CONFIG: ui/index.html not found (run from the repo root)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return serve_ui(args.port, args.bind, root / "ui")


def _infer_scenario(trace_scenario: str) -> Path:
    """Find the scenario.yaml whose id matches the trace scenario."""
    for candidate in sorted(Path("scenarios").glob("*/scenario.yaml")):
        try:
            data, _ = load_yaml(candidate)
        except ConfigError:
            continue
        if data.get("scenario") == trace_scenario:
            return candidate
    raise ConfigError(
        f"cannot infer --scenario for trace scenario {trace_scenario!r}; pass --scenario"
    )


def _trace_seed(events: list[dict[str, Any]], fallback: int) -> int:
    """Read the run seed from the run_start event, else use ``fallback``."""
    for event in events:
        detail = event.get("detail") or {}
        if detail.get("phase") == "run_start" and type(detail.get("seed")) is int:
            return detail["seed"]
    return fallback


def cmd_diagnose(argv: list[str]) -> int:
    """Execute `odrl-host diagnose` (exit 0 patch, 2 usage, 4 rejected).

    Fixture-backed by default: a failing ODRL trace maps to the
    scenario's reference patch. Passing traces are refused (exit 2).
    When LLAMA_BASE_URL is set the live OpenAI-compatible path is tried
    first (opt-in, slow); transport failures fall back to the fixture,
    but schema-invalid model output is rejected (exit 4). Read-only:
    input trace/state/policy files are never written.
    """
    parser = argparse.ArgumentParser(prog="odrl-host diagnose")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-rules", type=int, default=1)
    parser.add_argument("--state", default=None)
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--policy", default=None)
    parser.add_argument("--verdict", default=None)
    args = parser.parse_args(argv)

    def fail(code: str, hint: str, exit_code: int) -> int:
        print(f"odrl-host diagnose: {code}: {hint}", file=sys.stderr)
        return exit_code

    if args.max_rules is not None and args.max_rules < 1:
        return fail("E_POLICY_REJECTED", "--max-rules must be >= 1", EXIT_POLICY_REJECTED)
    trace_path = Path(args.trace)
    if not trace_path.is_file():
        return fail("E_CONFIG", f"trace file not found: {args.trace!r}", EXIT_USAGE)
    try:
        events = load_trace_events(str(trace_path))
    except TraceCorruptError as exc:
        print(f"trace {trace_path} is corrupt: {exc}")
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    run_id = str(events[0].get("run_id", ""))
    try:
        scenario_path = (
            Path(args.scenario) if args.scenario else _infer_scenario(events[0].get("scenario", ""))
        )
        scenario, _ = load_yaml(scenario_path)
        if args.policy:
            base_policy, _ = load_policy(args.policy)
            policy_path = Path(args.policy)
        else:
            policy_ref = resolve_policy(None, scenario_path, scenario)
            base_policy, _ = load_policy(policy_ref)
            policy_path = policy_ref
    except (ConfigError, PolicyError) as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)
    state_ref = args.state or str(Path("reports") / f"{run_id}.state.json")
    if not Path(state_ref).is_file():
        return fail("E_CONFIG", f"state file not found: {state_ref!r}", EXIT_USAGE)
    try:
        state = load_state_snapshot(state_ref)
    except TraceCorruptError as exc:
        print(f"state {state_ref} is corrupt: {exc}")
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    policy_mtime = os.path.getmtime(policy_path)
    state_digest = hashlib.sha256(Path(state_ref).read_bytes()).hexdigest()

    if args.verdict:
        try:
            verdict_doc = json.loads(Path(args.verdict).read_text(encoding="utf-8"))
            results = verdict_doc["results"]
            overall = verdict_doc.get("verdict", "FAIL")
            if not isinstance(results, list):
                raise KeyError("results")
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            return fail("E_CONFIG", f"cannot use --verdict {args.verdict!r}: {exc}", EXIT_USAGE)
    else:
        invariants = scenario.get("invariants", [])
        if not invariants:
            return fail("E_CONFIG", f"scenario {scenario_path} declares no invariants", EXIT_USAGE)
        try:
            outcome = run_verifiers(events, state, invariants)
        except UnknownVerifierError as exc:
            return fail("E_CONFIG", str(exc), EXIT_USAGE)
        except VerifierError as exc:
            print(f"trace {trace_path} cannot be judged: {exc}")
            return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)
        results, overall = outcome["results"], outcome["verdict"]

    if overall == "PASS":
        return fail(
            "E_CONFIG",
            f"No invariant violations to diagnose in trace {trace_path} (verdict PASS)",
            EXIT_USAGE,
        )
    failed = sorted(row["id"] for row in results if not row.get("passed", True))

    patch_doc: dict[str, Any] | None = None
    if os.environ.get("LLAMA_BASE_URL", "").strip():
        summary = f"scenario={scenario.get('scenario')} failed={failed}"
        try:
            patch_doc = fetch_live_patch(
                trace_summary=summary,
                verdict_summary=json.dumps(results, sort_keys=True),
                base_policy=base_policy,
                model=args.model,
                source_run_id=run_id,
            )
        except PolicyRejectedError as exc:
            return fail("E_POLICY_REJECTED", str(exc), EXIT_POLICY_REJECTED)
        if patch_doc is None:
            print(
                "odrl-host diagnose: live model unreachable; "
                "using fixture-backed reference patch",
                file=sys.stderr,
            )
    if patch_doc is None:
        try:
            patch_doc = reference_patch_for(str(scenario.get("scenario", "")), run_id)
        except ValueError as exc:
            return fail("E_CONFIG", str(exc), EXIT_USAGE)
    if isinstance(patch_doc, dict):
        patch_doc.setdefault("source_run_id", run_id)
    try:
        validate_patch_data(patch_doc)
    except PolicyRejectedError as exc:
        return fail("E_POLICY_REJECTED", str(exc), EXIT_POLICY_REJECTED)
    if len(patch_doc["patch"]["rules"]) > args.max_rules:
        return fail(
            "E_POLICY_REJECTED",
            f"patch proposes {len(patch_doc['patch']['rules'])} rules "
            f"(exceeds --max-rules {args.max_rules})",
            EXIT_POLICY_REJECTED,
        )
    try:
        new_policy, _ = apply_patch(base_policy, patch_doc)
    except PolicyRejectedError as exc:
        return fail("E_POLICY_REJECTED", str(exc), EXIT_POLICY_REJECTED)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(patch_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    if os.path.getmtime(policy_path) != policy_mtime or (
        hashlib.sha256(Path(state_ref).read_bytes()).hexdigest() != state_digest
    ):
        return fail("E_CONFIG", "read-only violation: inputs changed during diagnose", EXIT_USAGE)

    rule = patch_doc["patch"]["rules"][0]
    scenario_id = str(scenario.get("scenario", ""))
    if scenario_id == "poisoned_document":
        print("diagnosis: agent acted on untrusted doc instruction without confirmation")
        print(
            f"proposed: {rule.get('rule')}/{rule.get('tool')} -> {rule.get('action')}"
            "(require confirmation above $100, summarize instead of acting)"
        )
    elif scenario_id == "concurrent_update":
        print("diagnosis: blind write with a stale version overwrote the rival update")
        print(
            f"proposed: {rule.get('rule')}/{rule.get('tool')} -> {rule.get('action')}"
            "(re-read key_value_store by version before mutating writes)"
        )
    elif scenario_id in ("odrl_convert", "odrl_convert_poisoned"):
        print("diagnosis: sloppy conversion saved an unvalidated ODRL document")
        print(
            f"proposed: {rule.get('rule')}/{rule.get('tool')} -> {rule.get('action')}"
            "(strict parse of untrusted NL, save gated on validate_odrl)"
        )
    elif scenario_id == "odrl_convert_concurrent":
        print("diagnosis: blind save with a stale version overwrote the rival update")
        print(
            f"proposed: {rule.get('rule')}/{rule.get('tool')} -> {rule.get('action')}"
            "(re-read save_odrl by version before mutating saves)"
        )
    else:
        print(
            "diagnosis: blind retry_same on ambiguous save_odrl timeout caused a duplicate save"
        )
        print(
            f"proposed: {rule.get('rule')}/{rule.get('tool')} -> {rule.get('action')}"
            "(re-read save_odrl by version, rewrite only when missing)"
        )
    print(f"patch={out_path} (policy_patch/v1, {len(patch_doc['patch']['rules'])} rule)")
    print(diff_policies(base_policy, new_policy))
    print(
        f"read-only: ledger {state.get('ledger_hash')} unchanged, "
        f"policy {policy_path} mtime unchanged"
    )
    return EXIT_OK


def cmd_replay(argv: list[str]) -> int:
    """Execute `odrl-host replay` (exit 0 PASS, 1 VERDICT_FAIL, 4 rejected).

    Re-executes the source trace's scenario + seed under ``--policy`` (or
    ``--patch`` applied onto the scenario's base policy), judges the new
    run with the deterministic verifiers, and optionally re-runs every
    golden trace under the new policy as a regression suite. Invalid
    patches fail closed before any run (exit 4, no trace written).
    """
    parser = argparse.ArgumentParser(prog="odrl-host replay")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--policy", default=None)
    parser.add_argument("--patch", default=None)
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--state", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--trace-out", default=None)
    parser.add_argument("--state-out", default=None)
    parser.add_argument("--regression", default=str(Path("traces") / "golden"))
    parser.add_argument("--no-regression", action="store_true")
    parser.add_argument("--report-out", default=None)
    args = parser.parse_args(argv)

    def fail(code: str, hint: str, exit_code: int) -> int:
        print(f"odrl-host replay: {code}: {hint}", file=sys.stderr)
        return exit_code

    if bool(args.policy) == bool(args.patch):
        return fail("E_CONFIG", "pass exactly one of --policy or --patch", EXIT_USAGE)
    trace_path = Path(args.trace)
    if not trace_path.is_file():
        return fail("E_CONFIG", f"trace file not found: {args.trace!r}", EXIT_USAGE)
    try:
        events = load_trace_events(str(trace_path))
    except TraceCorruptError as exc:
        print(f"trace {trace_path} is corrupt: {exc}")
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)
    try:
        scenario_path = (
            Path(args.scenario) if args.scenario else _infer_scenario(events[0].get("scenario", ""))
        )
        scenario, scenario_text = load_yaml(scenario_path)
        base_ref = resolve_policy(None, scenario_path, scenario)
        base_policy, _ = load_policy(base_ref)
    except (ConfigError, PolicyError) as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)

    if args.policy:
        try:
            new_policy, new_policy_text = load_policy(args.policy)
        except PolicyError as exc:
            return fail("E_SCHEMA", str(exc), EXIT_USAGE)
        patch_ref: str | None = None
    else:
        try:
            patch_doc = load_patch_doc(args.patch)
            new_policy, new_policy_text = apply_patch(base_policy, patch_doc)
            if args.patch is not None:
                patch_ref = args.patch
            else:
                patch_ref = None
        except PolicyRejectedError as exc:
            return fail("E_POLICY_REJECTED", str(exc), EXIT_POLICY_REJECTED)

    seed = _trace_seed(events, scenario.get("seed", 42))
    max_steps = args.max_steps if args.max_steps is not None else scenario.get("max_steps", 12)
    source_run_id = str(events[0].get("run_id", ""))
    source_state_ref = args.state or str(Path("reports") / f"{source_run_id}.state.json")
    if not Path(source_state_ref).is_file():
        return fail("E_CONFIG", f"state file not found: {source_state_ref!r}", EXIT_USAGE)
    try:
        source_state = load_state_snapshot(source_state_ref)
    except TraceCorruptError as exc:
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)
    invariants = scenario.get("invariants", [])
    if not invariants:
        return fail("E_CONFIG", f"scenario {scenario_path} declares no invariants", EXIT_USAGE)
    try:
        before = run_verifiers(events, source_state, invariants)
    except UnknownVerifierError as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)
    except VerifierError as exc:
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    replay_id = compute_run_id(scenario_text, new_policy_text, seed)
    replay_trace = args.trace_out or str(Path("traces") / f"{replay_id}.jsonl")
    replay_state = args.state_out or str(Path("reports") / f"{replay_id}.state.json")
    Path(replay_trace).parent.mkdir(parents=True, exist_ok=True)
    Path(replay_state).parent.mkdir(parents=True, exist_ok=True)
    try:
        summary = run_react_loop(
            scenario=scenario,
            policy=new_policy,
            seed=seed,
            trace_path=replay_trace,
            state_path=replay_state,
            scenario_text=scenario_text,
            policy_text=new_policy_text,
            max_steps=max_steps,
            model=args.model,
        )
    except (ValueError, OSError) as exc:
        return fail("E_CONFIG", str(exc), EXIT_USAGE)
    new_events = load_trace_events(replay_trace)
    new_state = load_state_snapshot(replay_state)
    try:
        after = run_verifiers(new_events, new_state, invariants)
    except VerifierError as exc:
        return fail("E_TRACE_CORRUPT", str(exc), EXIT_CORRUPT)

    before_failed = {row["id"] for row in before["results"] if not row["passed"]}
    after_rows = {row["id"]: row for row in after["results"]}
    fixed = sorted(i for i in before_failed if after_rows.get(i, {}).get("passed"))
    broken = sorted(
        row["id"]
        for row in before["results"]
        if row["passed"] and not after_rows.get(row["id"], {}).get("passed")
    )
    saves = [
        event
        for event in new_events
        if isinstance(event, dict)
        and event.get("tool") == SAVE_ODRL
        and (event.get("args") or {}).get("op") == "write"
        and event.get("outcome") != "error"
        and (event.get("detail") or {}).get("phase") == "tool_result"
    ]
    task_status = f"{len(saves)} save(s)"

    regression: list[dict[str, Any]] = []
    if not args.no_regression:
        reg_dir = Path(args.regression)
        if reg_dir.is_dir():
            for golden in sorted(reg_dir.glob("*.jsonl")):
                regression.append(
                    _regress_golden(
                        golden, new_policy, new_policy_text, model=args.model, max_steps=max_steps
                    )
                )

    report = {
        "api_version": "v1",
        "broken": broken,
        "fixed": fixed,
        "golden": {
            "passed": sum(1 for case in regression if case["verdict"] == "PASS"),
            "total": len(regression),
            "cases": regression,
        },
        "replay_id": replay_id,
        "scenario": scenario.get("scenario"),
        "seed": seed,
        "source_run_id": source_run_id,
        "patch": patch_ref,
        "verdict": after["verdict"],
    }
    report_out = args.report_out or str(Path("reports") / f"{replay_id}.replay.json")
    Path(report_out).parent.mkdir(parents=True, exist_ok=True)
    Path(report_out).write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    print(
        f"replay_id={replay_id} scenario={scenario.get('scenario')} "
        f"seed={seed} verdict={after['verdict']}"
    )
    if before["verdict"] == "FAIL" and after["verdict"] == "PASS":
        fixed_bits = ", ".join(f"{i} PASS" for i in fixed) or "invariants PASS"
        print(f"fix: VERIFIED ({fixed_bits}, task {task_status})")
    elif after["verdict"] == "PASS":
        print("fix: NOTHING_TO_FIX (source already PASS)")
    else:
        print(f"fix: NOT_FIXED (verdict {after['verdict']}, task {task_status})")
    if args.no_regression or not Path(args.regression).is_dir():
        print("regression: skipped (no golden dir)")
    else:
        passed = report["golden"]["passed"]
        total = report["golden"]["total"]
        print(f"regression: {passed}/{total} golden traces still PASS")
        for case in regression:
            print(f"  [{case['verdict']}] {case['trace']}")
    print(f"report={report_out}")
    _ = summary
    regress_ok = all(case["verdict"] == "PASS" for case in regression)
    return EXIT_OK if after["verdict"] == "PASS" and regress_ok else EXIT_VERDICT_FAIL


def _regress_golden(
    golden: Path, policy: dict[str, Any], policy_text: str, *, model: str, max_steps: int
) -> dict[str, Any]:
    """Re-run one golden trace's scenario + seed under ``policy``."""
    try:
        events = load_trace_events(str(golden))
        scenario_path = _infer_scenario(events[0].get("scenario", ""))
        scenario, scenario_text = load_yaml(scenario_path)
        seed = _trace_seed(events, scenario.get("seed", 42))
    except (TraceCorruptError, ConfigError) as exc:
        return {"trace": golden.name, "verdict": "ERROR", "evidence": str(exc)}
    with tempfile.TemporaryDirectory(prefix="odrl-host-regress-") as tmp:
        trace_out = str(Path(tmp) / "trace.jsonl")
        state_out = str(Path(tmp) / "state.json")
        try:
            run_react_loop(
                scenario=scenario,
                policy=policy,
                seed=seed,
                trace_path=trace_out,
                state_path=state_out,
                scenario_text=scenario_text,
                policy_text=policy_text,
                max_steps=max_steps,
                model=model,
            )
            new_events = load_trace_events(trace_out)
            new_state = load_state_snapshot(state_out)
            outcome = run_verifiers(new_events, new_state, scenario.get("invariants", []))
        except (ValueError, OSError, VerifierError, UnknownVerifierError) as exc:
            return {"trace": golden.name, "verdict": "ERROR", "evidence": str(exc)}
    return {"trace": golden.name, "verdict": outcome["verdict"]}


def main(argv: list[str] | None = None) -> int:
    """Dispatch subcommands: run, diagnose, replay, seed, optimize-prompts, serve."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--version" in raw:
        print(f"odrl-host {__version__} (framework checks: agentcrash check/serve)")
        return EXIT_OK
    if not raw or raw[0] in ("-h", "--help"):
        print(
            "usage: odrl-host [--version] {run, diagnose, replay, seed, optimize-prompts, serve} ...\n"
            "  run: execute the live NL-to-ODRL agent against a chaos scenario\n"
            "  diagnose: propose a policy patch for a failing trace (read-only)\n"
            "  replay: re-run a trace scenario under a patched policy + regression\n"
            "  seed: replay scripted fixture runs into agentcrash.db (offline, no model)\n"
            "  optimize-prompts: budgeted DSPy probe of the NL-to-ODRL instruction (live model)\n"
            "  serve: browse the ODRL/Policy Studio (reads ui/, local only)\n"
            "framework checks (any trace): agentcrash check/serve"
        )
        return EXIT_OK if raw else EXIT_USAGE
    command, rest = raw[0], raw[1:]
    if command == "run":
        return cmd_run(rest)
    if command == "diagnose":
        return cmd_diagnose(rest)
    if command == "replay":
        return cmd_replay(rest)
    if command == "seed":
        return cmd_seed(rest)
    if command == "optimize-prompts":
        return cmd_optimize_prompts(rest)
    if command == "serve":
        return cmd_serve(rest)
    print(f"odrl-host: unknown command {command!r}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
