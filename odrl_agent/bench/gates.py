"""Release gates (Phase 4.2 "production gates in bold") and regression checks."""
from __future__ import annotations

from typing import Any

#: metric -> (comparison, threshold, live_only)
GATES: dict[str, tuple[str, float, bool]] = {
    "shacl_pass_rate": ("==", 1.0, False),
    "valid_term_rate": ("==", 1.0, False),
    "checkpoint_f1": (">=", 0.92, False),
    "constraint_f1": (">=", 0.90, False),
    "false_permission_rate": ("<", 0.01, False),
    "tier5_false_permission_rate": ("==", 0.0, False),
    # Tracked in the plan; gated here because a dropped prohibition is an over-permission.
    "missed_prohibition_rate": ("<=", 0.02, False),
    "behavioral_agreement": (">=", 0.95, False),
    "duty_recorded": (">=", 0.95, False),
    "normal_form_equivalence": (">=", 0.95, False),
    "conflict_escape_rate": ("==", 0.0, False),
    "ambiguity_recall": (">=", 0.90, False),
    "unsupported_assumption_rate": ("<", 0.02, False),
    "span_attribution": (">=", 0.95, False),
    "median_llm_calls": ("<=", 2.0, False),
    "p95_simple_s": ("<", 4.0, True),
    "p95_complex_s": ("<", 10.0, True),
}

#: A candidate may not exceed the baseline FPR by more than this (absolute).
MAX_FPR_INCREASE = 0.005
#: A candidate may not drop more than this (absolute) below the baseline F1.
MAX_F1_DROP = 0.01
REGRESSION_F1 = ("checkpoint_f1", "constraint_f1")

_OPS = {
    "==": lambda a, b: a == b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
}


def evaluate(metrics: dict[str, Any], *, live: bool) -> list[dict[str, Any]]:
    """One row per gate: ``{metric, rule, value, passed}``. Missing data fails."""
    rows = []
    for name, (op, limit, live_only) in GATES.items():
        if live_only and not live:
            continue
        value = metrics.get(name)
        passed = value is not None and _OPS[op](value, limit)
        rows.append({"metric": name, "rule": f"{op} {limit}", "value": value, "passed": passed})
    return rows


def regressions(metrics: dict[str, Any], baseline: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows for FPR increase and checkpoint-F1 drop against a frozen baseline."""
    rows = []
    for name in ("false_permission_rate", "tier5_false_permission_rate", "missed_prohibition_rate"):
        new, old = metrics.get(name), baseline.get(name)
        passed = new is not None and old is not None and new <= old + MAX_FPR_INCREASE
        rows.append({"metric": f"{name} vs baseline", "rule": f"<= baseline + {MAX_FPR_INCREASE}",
                     "value": new, "baseline": old, "passed": passed})
    for name in REGRESSION_F1:
        new, old = metrics.get(name), baseline.get(name)
        passed = new is not None and old is not None and new >= old - MAX_F1_DROP
        rows.append({"metric": f"{name} vs baseline", "rule": f">= baseline - {MAX_F1_DROP}",
                     "value": new, "baseline": old, "passed": passed})
    return rows


def failed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if not row["passed"]]
