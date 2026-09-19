from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from rai_card.gates import run_governance_gate
from rai_card.io import load_bundle_spec, load_dynamic_metrics, load_static_card
from rai_card.policy import load_policy
from rai_card.render import render_compliance_bundle, render_model_card

DEFAULTS = {
    "static": "model_card.yaml",
    "dynamic": "metrics.json",
    "policy": "policies/governance.yaml",
    "bundle": "compliance/bundle.yaml",
    "output": "MODEL_CARD.md",
    "bundle_output": "COMPLIANCE_BUNDLE.md",
    "report": "rai-gate.json",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rai-card",
        description=(
            "Gate an NL→ODRL→Cedar/Rego assistant on its model card, "
            "eval evidence, and compliance bundle. Does not convert policies."
        ),
    )
    parser.add_argument("--static", default=DEFAULTS["static"])
    parser.add_argument("--dynamic", default=DEFAULTS["dynamic"])
    parser.add_argument("--policy", default=DEFAULTS["policy"])
    parser.add_argument("--bundle", default=DEFAULTS["bundle"])
    parser.add_argument("--output", default=DEFAULTS["output"])
    parser.add_argument("--bundle-output", default=DEFAULTS["bundle_output"])
    parser.add_argument("--report", default=DEFAULTS["report"])
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--allow-partial-artifact", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print("=" * 60)
    print("  CI STEP: rai-card (NL→ODRL compliance gate)")
    print("=" * 60)

    try:
        policy = load_policy(args.policy)
        raw_static, static, static_error = load_static_card(args.static)
        raw_dynamic, dynamic, dynamic_error = load_dynamic_metrics(args.dynamic)
        raw_bundle, bundle, bundle_error = load_bundle_spec(args.bundle)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: failed to load inputs: {exc}", file=sys.stderr)
        return 2

    _ = raw_bundle
    print(f"Loaded model card         {args.static}")
    print(f"Loaded eval evidence      {args.dynamic}")
    print(f"Loaded compliance bundle  {args.bundle}")
    print(f"Loaded policy             {args.policy} ({policy.policy_id})")
    print()

    report = run_governance_gate(
        raw_static,
        raw_dynamic,
        policy,
        static=static,
        dynamic=dynamic,
        static_error=static_error,
        dynamic_error=dynamic_error,
        bundle=bundle,
        bundle_error=bundle_error,
        strict=args.strict,
    )
    Path(args.report).write_text(json.dumps(report.to_dict(), indent=2) + "\n")

    def write_artifacts() -> None:
        if static is None or dynamic is None or bundle is None:
            return
        now = datetime.now(timezone.utc)
        Path(args.output).write_text(
            render_model_card(static, dynamic, report, policy, generated_at=now)
        )
        Path(args.bundle_output).write_text(
            render_compliance_bundle(static, dynamic, bundle, report, policy, generated_at=now)
        )

    if report.passed:
        print("  ✓ Model card + bundle completeness")
        print("  ✓ Human sign-off before PEP attach / fail-closed ambiguity")
        print("  ✓ Fidelity vs gold ODRL (over-permission is the red line)")
        print("  ✓ Cedar and Rego encodings do not widen rights")
        print()
        if static is None or dynamic is None or bundle is None:
            print("ERROR: empty models after a passing gate.", file=sys.stderr)
            return 2
        write_artifacts()
        print(f"BUILD PASSED — wrote {args.output}, {args.bundle_output}, {args.report}")
        return 0

    print("BUILD FAILED — governance gate rejected this release:\n")
    for check in report.failing_errors():
        print(f"  ✗ [{check.check_id}] {check.message}")
    for check in report.failing_warnings():
        print(f"  ! [{check.check_id}] {check.message}")
    print(f"\nMachine-readable report: {args.report}")
    print("Fix the card, the bundle, the gold-set eval, or policy (with review).")
    if args.allow_partial_artifact:
        write_artifacts()
        print("Wrote partial artifacts (release still blocked).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
