"""Load the release envelope the auditor API returns.

Always recomputes the gate from files. Never returns a hardcoded PASS.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from rai_card.cli import DEFAULTS
from rai_card.gates import GateReport, run_governance_gate
from rai_card.io import load_bundle_spec, load_dynamic_metrics, load_static_card
from rai_card.models import ComplianceBundleSpec, DynamicMetrics, StaticCard
from rai_card.policy import GovernancePolicy, load_policy
from rai_card.render import render_compliance_bundle, render_model_card

EXHIBIT_PARTS = {
    "nl": ".nl.txt",
    "odrl": ".odrl.jsonld",
    "cedar": ".cedar",
    "rego": ".rego",
}


@dataclass
class ArtifactCatalog:
    root: Path
    policy: GovernancePolicy
    static: StaticCard | None
    dynamic: DynamicMetrics | None
    bundle: ComplianceBundleSpec | None
    report: GateReport
    static_raw: dict[str, Any]
    dynamic_raw: dict[str, Any]
    bundle_raw: dict[str, Any]


def load_catalog(root: Path, *, now: date | None = None) -> ArtifactCatalog:
    root = root.resolve()
    policy = load_policy(root / DEFAULTS["policy"])
    static_raw, static, static_error = load_static_card(root / DEFAULTS["static"])
    dynamic_raw, dynamic, dynamic_error = load_dynamic_metrics(root / DEFAULTS["dynamic"])
    bundle_raw, bundle, bundle_error = load_bundle_spec(root / DEFAULTS["bundle"])
    report = run_governance_gate(
        static_raw,
        dynamic_raw,
        policy,
        static=static,
        dynamic=dynamic,
        static_error=static_error,
        dynamic_error=dynamic_error,
        bundle=bundle,
        bundle_error=bundle_error,
        now=now,
    )
    return ArtifactCatalog(
        root=root,
        policy=policy,
        static=static,
        dynamic=dynamic,
        bundle=bundle,
        report=report,
        static_raw=static_raw,
        dynamic_raw=dynamic_raw,
        bundle_raw=bundle_raw,
    )


def card_payload(cat: ArtifactCatalog) -> dict[str, Any]:
    markdown = None
    if cat.static and cat.dynamic:
        markdown = render_model_card(cat.static, cat.dynamic, cat.report, cat.policy)
    return {
        "system_id": cat.static_raw.get("model_id"),
        "source": "recomputed_from_git_artifacts",
        "gate_passed": cat.report.passed,
        "card": cat.static.model_dump(mode="json") if cat.static else None,
        "markdown": markdown,
        "schema_error": None if cat.static else "static card failed validation",
    }


def bundle_payload(cat: ArtifactCatalog) -> dict[str, Any]:
    markdown = None
    if cat.static and cat.dynamic and cat.bundle:
        markdown = render_compliance_bundle(
            cat.static, cat.dynamic, cat.bundle, cat.report, cat.policy
        )
    return {
        "bundle_id": cat.bundle_raw.get("bundle_id"),
        "system_id": cat.bundle_raw.get("system_id"),
        "source": "recomputed_from_git_artifacts",
        "gate_passed": cat.report.passed,
        "spec": cat.bundle.model_dump(mode="json") if cat.bundle else None,
        "markdown": markdown,
    }


def list_exhibits(root: Path) -> list[dict[str, Any]]:
    gold = root / "exhibits" / "gold_set"
    if not gold.is_dir():
        return []
    ids = sorted(
        {p.name.split(".", 1)[0] for p in gold.iterdir() if p.name[:1].isdigit()}
    )
    items = []
    for item_id in ids:
        parts = {
            name: (gold / f"{item_id}{suffix}").exists()
            for name, suffix in EXHIBIT_PARTS.items()
        }
        items.append({"id": item_id, "parts": parts, "complete": all(parts.values())})
    return items


def exhibit_trace(root: Path, item_id: str) -> dict[str, Any] | None:
    if not item_id.isalnum() or len(item_id) > 8:
        return None
    gold = root / "exhibits" / "gold_set"
    files: dict[str, str] = {}
    missing = []
    for name, suffix in EXHIBIT_PARTS.items():
        path = gold / f"{item_id}{suffix}"
        if not path.is_file():
            missing.append(name)
            continue
        files[name] = path.read_text()
    if not files:
        return None
    return {
        "id": item_id,
        "kind": "gold_set_trace",
        "note": (
            "Frozen exhibit of NL → ODRL → Cedar/Rego. "
            "This API does not compile; it retrieves the evaluation object."
        ),
        "parts": files,
        "missing_parts": missing,
    }
