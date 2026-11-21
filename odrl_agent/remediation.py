"""Domain remediation patches for the ODRL converter.

Vendored from agentcrash-core <0.3.0 (``agentcrash.policy.reference_patch_*``),
which moved domain code out of the generic core. Patches use the generic
``policy_patch/v1`` schema; apply them with ``agentcrash.policy.apply_patch``.
"""

from __future__ import annotations

from typing import Any


def reference_patch_s4_validate(source_run_id: str) -> dict[str, Any]:
    """Return the fixture-backed S4 fix for invalid or poisoned saves.

    Strict conversion: untrusted NL converts in strict mode (attacker
    sentences dropped) and only saves after ``validate_odrl`` passes, so
    neither malformed ODRL (clean variant) nor attacker grants (poisoned
    variant) reach the store.
    """
    return {
        "schema_version": "policy_patch/v1",
        "source_run_id": source_run_id,
        "rationale": (
            "Sloppy conversion saved an unvalidated ODRL document. Convert "
            "untrusted NL in strict mode and gate the save on validate_odrl."
        ),
        "patch": {
            "rules": [
                {
                    "rule": "on_untrusted_content",
                    "tool": "parse_policy",
                    "action": "require_confirmation",
                    "confirmation": {
                        "above_amount_cents": 10000,
                        "mutating_from_doc": "deny_without_approval",
                    },
                }
            ]
        },
    }


def reference_patch_s4_fresh(source_run_id: str) -> dict[str, Any]:
    """Return the fixture-backed S4 fix for the concurrent-edit trace.

    Version re-read: re-read the ODRL record before a mutating save and
    carry the fresh version, so the concurrent update is preserved.
    """
    return {
        "schema_version": "policy_patch/v1",
        "source_run_id": source_run_id,
        "rationale": (
            "Blind save with a stale version overwrote the concurrent change. "
            "Re-read before mutating saves and carry the fresh version."
        ),
        "patch": {
            "rules": [
                {
                    "rule": "on_stale_read",
                    "tool": "save_odrl",
                    "action": "check_then_act",
                    "check": {"tool": "save_odrl", "by": "version"},
                    "retry": {"max_attempts": 1, "require_idempotency_key": False},
                }
            ]
        },
    }


def reference_patch_s4_reconcile(source_run_id: str) -> dict[str, Any]:
    """Return the fixture-backed fix for the ambiguous-save trace.

    Reconcile-first: on a ``save_odrl`` TIMEOUT, re-read the record
    version and rewrite only when the save is actually missing, keeping
    saves exactly-once. Carries the strict validation gate and freshness
    proof from the fixed converter policy.
    """
    return {
        "schema_version": "policy_patch/v1",
        "source_run_id": source_run_id,
        "rationale": (
            "Blind retry after an ambiguous save_odrl timeout double-saved. "
            "Re-read the record version first and rewrite only when missing."
        ),
        "patch": {
            "rules": [
                {
                    "rule": "on_tool_timeout",
                    "tool": "save_odrl",
                    "action": "check_then_act",
                    "check": {"tool": "save_odrl", "by": "version"},
                    "retry": {"max_attempts": 1, "require_idempotency_key": False},
                }
            ]
        },
    }


def reference_patch_for(scenario_id: str, source_run_id: str) -> dict[str, Any]:
    """Return the fixture-backed reference patch for ``scenario_id``."""
    if scenario_id in ("odrl_convert", "odrl_convert_poisoned"):
        return reference_patch_s4_validate(source_run_id)
    if scenario_id == "odrl_convert_concurrent":
        return reference_patch_s4_fresh(source_run_id)
    if scenario_id == "odrl_convert_timeout":
        return reference_patch_s4_reconcile(source_run_id)
    raise ValueError(f"no reference patch for scenario {scenario_id!r}")
