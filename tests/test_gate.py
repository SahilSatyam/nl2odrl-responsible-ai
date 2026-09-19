from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml

from rai_card.cli import main
from rai_card.gates import run_governance_gate
from rai_card.io import load_bundle_spec, load_dynamic_metrics, load_static_card
from rai_card.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
FROZEN = date(2026, 9, 19)


def gate(policy, static_path, dynamic_path, bundle_path=None, *, now=FROZEN):
    raw_s, static, s_err = load_static_card(static_path)
    raw_d, dynamic, d_err = load_dynamic_metrics(dynamic_path)
    bundle = bundle_err = None
    if bundle_path:
        _, bundle, bundle_err = load_bundle_spec(bundle_path)
    return run_governance_gate(
        raw_s,
        raw_d,
        policy,
        static=static,
        dynamic=dynamic,
        static_error=s_err,
        dynamic_error=d_err,
        bundle=bundle,
        bundle_error=bundle_err,
        now=now,
    )


def test_happy_path_passes():
    policy = load_policy(ROOT / "policies" / "governance.yaml")
    report = gate(
        policy,
        ROOT / "model_card.yaml",
        ROOT / "metrics.json",
        ROOT / "compliance" / "bundle.yaml",
    )
    assert report.passed, report.errors


def test_missing_limitations_fails():
    policy = load_policy(ROOT / "policies" / "governance.yaml")
    report = gate(
        policy,
        ROOT / "model_card_BROKEN_demo.yaml",
        ROOT / "metrics.json",
        ROOT / "compliance" / "bundle.yaml",
    )
    assert not report.passed
    assert "limitations" in " ".join(report.errors).lower()


def test_over_permission_fails_even_with_pass_stamp(tmp_path):
    policy = load_policy(ROOT / "policies" / "governance.yaml")
    raw = json.loads((ROOT / "metrics.json").read_text())
    raw["fidelity"]["status"] = "PASS"
    raw["fidelity"]["over_permission_rate"] = 0.09
    path = tmp_path / "spoof.json"
    path.write_text(json.dumps(raw))
    report = gate(policy, ROOT / "model_card.yaml", path, ROOT / "compliance" / "bundle.yaml")
    assert not report.passed
    assert any(c.check_id == "eval.over_permission" and not c.passed for c in report.checks)


def test_overpermit_fixture():
    policy = load_policy(ROOT / "policies" / "governance.yaml")
    report = gate(
        policy,
        ROOT / "model_card.yaml",
        ROOT / "fixtures" / "metrics_overpermit_fail.json",
        ROOT / "compliance" / "bundle.yaml",
    )
    assert not report.passed


def test_autonomous_fails(tmp_path):
    policy = load_policy(ROOT / "policies" / "governance.yaml")
    card = yaml.safe_load((ROOT / "model_card.yaml").read_text())
    card["decision_rights"]["automation_level"] = "autonomous"
    card["decision_rights"]["sole_determinant"] = True
    card["decision_rights"]["attach_to_pep_requires_signoff"] = False
    path = tmp_path / "auto.yaml"
    path.write_text(yaml.safe_dump(card, sort_keys=False))
    report = gate(policy, path, ROOT / "metrics.json", ROOT / "compliance" / "bundle.yaml")
    ids = {c.check_id for c in report.failing_errors()}
    assert "rights.automation_cap" in ids
    assert "rights.sole_determinant" in ids


def test_expired_card_fails(tmp_path):
    policy = load_policy(ROOT / "policies" / "governance.yaml")
    card = yaml.safe_load((ROOT / "model_card.yaml").read_text())
    card["recertification_due"] = "2026-01-01"
    path = tmp_path / "stale.yaml"
    path.write_text(yaml.safe_dump(card, sort_keys=False))
    report = gate(policy, path, ROOT / "metrics.json", ROOT / "compliance" / "bundle.yaml")
    assert any(c.check_id == "lifecycle.recertification" and not c.passed for c in report.checks)


def test_cli_writes_card_and_bundle(tmp_path):
    out = tmp_path / "CARD.md"
    bundle_out = tmp_path / "BUNDLE.md"
    report = tmp_path / "gate.json"
    code = main(
        [
            "--static", str(ROOT / "model_card.yaml"),
            "--dynamic", str(ROOT / "metrics.json"),
            "--policy", str(ROOT / "policies" / "governance.yaml"),
            "--bundle", str(ROOT / "compliance" / "bundle.yaml"),
            "--output", str(out),
            "--bundle-output", str(bundle_out),
            "--report", str(report),
        ]
    )
    assert code == 0
    assert "Natural Language to ODRL" in out.read_text()
    assert "Compliance Bundle" in bundle_out.read_text()
    assert "over-permission" in out.read_text().lower()
    assert json.loads(report.read_text())["passed"] is True


def test_cli_broken_nonzero(tmp_path):
    code = main(
        [
            "--static", str(ROOT / "model_card_BROKEN_demo.yaml"),
            "--dynamic", str(ROOT / "metrics.json"),
            "--bundle", str(ROOT / "compliance" / "bundle.yaml"),
            "--output", str(tmp_path / "CARD.md"),
            "--bundle-output", str(tmp_path / "BUNDLE.md"),
            "--report", str(tmp_path / "gate.json"),
        ]
    )
    assert code == 1
    assert not (tmp_path / "CARD.md").exists()
