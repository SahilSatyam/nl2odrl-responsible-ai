"""UFO-L position report, behaviour='open', strong permissions, and Offer refusal (Phase 2, Task 2.3)."""
from __future__ import annotations

import json

from scripted_llm import ir, rule, scripted_llm

from odrl_agent.bench.evaluator import agreement, decide
from odrl_agent.compiler import canonical_json, compile_doc
from odrl_agent.extractor import Extractor
from odrl_agent.ir import PolicyIR
from odrl_agent.odrl import validate_odrl
from odrl_agent.pipeline import convert
from odrl_agent.positions import OPEN_BEHAVIOUR_REASON, report
from odrl_agent.verifiers import get_verifier


def _policy(*rules: dict, **extra) -> PolicyIR:
    return PolicyIR.model_validate({"rules": list(rules), **extra})


def test_positions_report_snapshot_with_two_assigner_remedies() -> None:
    agreement_ir = _policy(
        {
            "modality": "permission",
            "action": "read",
            "target": "showtimes",
            "assignee": "KulturPortal",
            "assigner": "BerlinerEnsemble",
        },
        {
            "modality": "prohibition",
            "action": "distribute",
            "target": "showtimes",
            "assignee": "KulturPortal",
            "assigner": "BerlinerEnsemble",
            "duties": [{"action": "compensate", "role": "remedy"}],
        },
        {
            "modality": "prohibition",
            "action": "distribute",
            "target": "showtimes",
            "assignee": "KulturPortal",
            "assigner": "DeutschesTheater",
            "duties": [{"action": "delete", "role": "remedy"}],
        },
        policy_type="Agreement",
        assigner="BerlinerEnsemble",
        assignee="KulturPortal",
    )
    rep = report(agreement_ir)
    assert rep["policy_type"] == "Agreement"
    assert rep["behaviour"] == "closed"
    assert rep["normative_vacuum"] is False

    powers = [p for p in rep["positions"] if p["position"] == "Power"]
    assert len(powers) == 2
    assert [(p["bearer"], p["action"], p["prohibited_action"]) for p in powers] == [
        ("BerlinerEnsemble", "compensate", "distribute"),
        ("DeutschesTheater", "delete", "distribute"),
    ]
    assert rep["summary"] == {
        "Duty-to-Omit": 2,
        "NoRight": 1,
        "Permission": 1,
        "Power": 2,
        "Right-to-Omission": 2,
        "Subjection": 2,
    }
    # JSON-serialisable snapshot check
    assert json.loads(json.dumps(rep)) == rep


def test_strong_permission_adds_immunity_disability_without_changing_odrl_graph() -> None:
    weak = _policy(
        {
            "modality": "permission",
            "action": "read",
            "target": "dataset-7",
            "assignee": "team",
            "strong": False,
        },
        policy_type="Agreement",
        assigner="steward",
        assignee="team",
    )
    strong = _policy(
        {
            "modality": "permission",
            "action": "read",
            "target": "dataset-7",
            "assignee": "team",
            "strong": True,
        },
        policy_type="Agreement",
        assigner="steward",
        assignee="team",
    )
    weak_rep = report(weak)
    strong_rep = report(strong)
    assert "Immunity" not in weak_rep["summary"]
    assert strong_rep["summary"]["Immunity"] == 1
    assert strong_rep["summary"]["Disability"] == 1

    # ODRL graph and SHACL are identical for strong=True and strong=False.
    weak_doc = compile_doc(weak)
    strong_doc = compile_doc(strong)
    # Strip uid (content hash covers strong field on IR dump) and verify identical ODRL payload + SHACL.
    assert {k: v for k, v in weak_doc.items() if k != "uid"} == {
        k: v for k, v in strong_doc.items() if k != "uid"
    }
    assert validate_odrl(weak_doc) == (True, [])
    assert validate_odrl(strong_doc) == (True, [])
    assert canonical_json(weak_doc["permission"]) == canonical_json(strong_doc["permission"])


def test_behaviour_open_holds_for_review_and_inserts_no_universal_permission() -> None:
    text = "Allow research-team to read dataset-7."
    reply = ir(
        rule(action="read", target="dataset-7", assignee="research-team", quote=text),
        behaviour="open",
    )
    draft, doc = convert(text, Extractor(scripted_llm([reply])))
    assert doc is not None
    assert draft.status == "needs_review"
    assert draft.auto_publish is False
    assert OPEN_BEHAVIOUR_REASON in draft.alerts
    # No universal permission inserted.
    assert len(doc["permission"]) == 1
    assert doc["permission"][0]["action"] == "read"


def test_offer_is_refused_by_evaluator_decide_and_not_scored_by_agreement() -> None:
    offer = {
        "policy_type": "Offer",
        "assigner": "steward",
        "rules": [
            {"modality": "permission", "action": "read", "target": "dataset-7", "assignee": "team"}
        ],
    }
    req = {"assignee": "team", "target": "dataset-7", "action": "read", "context": {}}
    assert decide(offer, req) is False
    assert agreement(offer, offer) == (0, 0)

    agree = dict(offer, policy_type="Agreement", assignee="team")
    assert decide(agree, req) is True
    agree_ok, agree_total = agreement(agree, agree)
    assert agree_total > 0 and agree_ok == agree_total


def test_positions_complete_verifier_rejects_open_behaviour_when_published() -> None:
    v = get_verifier("positions_complete")
    assert v.check([], {}).passed is True
    open_ir = ir(rule(), behaviour="open")
    trace = [
        {"tool": "save_odrl", "step": 2, "outcome": "success", "args": {"op": "write"},
         "detail": {"phase": "tool_result", "data": {"value": {}}}},
        {"detail": {"phase": "run_end", "draft": {"ir": open_ir}}},
    ]
    verdict = v.check(trace, {})
    assert verdict.passed is False
    assert "normative vacuum" in verdict.evidence
