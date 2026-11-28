"""Normalisation pass: DNF, interval split, cap 64, obligations untouched (Phase 2, Task 2.2)."""
from __future__ import annotations

import json
import random
from pathlib import Path

from agentcrash.verifiers import load_trace_events
from scripted_llm import install, ir, rule, scripted_llm

from odrl_agent.bench.evaluator import decide
from odrl_agent.compiler import canonical_json, compile_ir
from odrl_agent.extractor import Extractor
from odrl_agent.ir import PolicyIR
from odrl_agent.normalise import MAX_NORMAL_RULES, normalise
from odrl_agent.odrl import validate_odrl
from odrl_agent.pipeline import convert, run_conversion
from odrl_agent.reasoning import UNSATISFIABLE, analyze

REPO = Path(__file__).resolve().parent.parent


def _c(lo: str, op: str, val: str, vt: str = "integer") -> dict:
    return {
        "kind": "atomic",
        "left_operand": lo,
        "operator": op,
        "right_operand": val,
        "value_type": vt,
    }


def _policy(*rules: dict, **extra) -> PolicyIR:
    return PolicyIR.model_validate({"rules": list(rules), **extra})


def test_rewrites_and_splits_numeric_intervals_on_partner_constants() -> None:
    src = _policy(
        {
            "modality": "permission",
            "action": "read",
            "target": "dataset-7",
            "assignee": "team",
            "constraints": [_c("count", "gteq", "10")],
        },
        {
            "modality": "prohibition",
            "action": "read",
            "target": "dataset-7",
            "assignee": "team",
            "constraints": [_c("count", "eq", "20")],
        },
    )
    norm = normalise(src)
    perms = [r for r in norm.rules if r.modality == "permission"]
    # count >= 10 split at 20 -> {=10}, {(10,20)}, {=20}, {>20}
    assert len(perms) == 4
    ops = [[(c.operator, c.right_operand) for c in r.constraints] for r in perms]
    assert ops == [
        [("eq", "10")],
        [("gt", "10"), ("lt", "20")],
        [("eq", "20")],
        [("gt", "20")],
    ]
    # All operators belong to {eq, lt, gt}
    for r in norm.rules:
        for c in r.constraints:
            assert c.operator in ("eq", "lt", "gt")


def test_obligations_and_duty_constraints_are_copied_unchanged() -> None:
    src = _policy(
        {
            "modality": "permission",
            "action": "read",
            "target": "dataset-7",
            "assignee": "team",
            "constraints": [_c("payAmount", "gteq", "5", "decimal")],
            "duties": [{
                "action": "delete",
                "target": "dataset-7",
                "constraints": [_c("delayPeriod", "lteq", "P14D", "duration")],
            }],
        },
        {
            "modality": "prohibition",
            "action": "distribute",
            "target": "dataset-7",
            "assignee": "team",
            "constraints": [_c("payAmount", "eq", "20", "decimal")],
        },
        {
            "modality": "obligation",
            "action": "compensate",
            "target": "dataset-7",
            "assignee": "team",
            "constraints": [_c("payAmount", "gt", "10", "decimal")],
        },
    )
    norm = normalise(src)
    obls = [r for r in norm.rules if r.modality == "obligation"]
    assert len(obls) == 1
    assert len(obls[0].constraints) == 1
    assert obls[0].constraints[0].operator == "gt"
    assert obls[0].constraints[0].right_operand == "10"
    perms = [r for r in norm.rules if r.modality == "permission"]
    assert len(perms) == 4
    for p in perms:
        assert len(p.duties) == 1
        assert p.duties[0].constraints[0].operator == "lteq"
        assert p.duties[0].constraints[0].right_operand == "P14D"


def test_empty_interval_drops_permission_and_preserves_unsat_finding() -> None:
    src = _policy({
        "modality": "permission",
        "action": "read",
        "target": "dataset-7",
        "assignee": "team",
        "constraints": [_c("count", "gt", "0"), _c("count", "lt", "0")],
    })
    assert [f.code for f in analyze(src).findings] == [UNSATISFIABLE]
    norm = normalise(src)
    assert norm.rules == []

    # Pipeline with emit_normal_form=True keeps source unsat finding and does not auto-publish.
    text = "team may read dataset-7 when count > 0 and count < 0."
    reply = ir(rule(
        action="read",
        target="dataset-7",
        assignee="team",
        quote=text,
        constraints=[_c("count", "gt", "0"), _c("count", "lt", "0")],
    ))
    draft, doc = convert(text, Extractor(scripted_llm([reply] * 3)), emit_normal_form=True)
    assert doc is None
    assert draft.auto_publish is False
    assert draft.status == "needs_review"
    assert draft.reasoning["unsatisfiable"]


def test_explosion_over_64_returns_source_tree_with_ambiguity() -> None:
    # 7 binary disjunctions -> 2^7 = 128 > 64 clauses
    ors = [
        {
            "kind": "logical",
            "operator": "or",
            "constraints": [
                _c("count", "eq", str(2 * k)),
                _c("payAmount", "eq", str(2 * k + 1), "decimal"),
            ],
        }
        for k in range(7)
    ]
    src = _policy(
        {
            "modality": "permission",
            "action": "read",
            "target": "dataset-7",
            "assignee": "team",
            "constraints": ors,
        },
        {
            "modality": "obligation",
            "action": "delete",
            "target": "dataset-7",
            "assignee": "team",
            "constraints": [_c("count", "gteq", "1")],
        },
    )
    out = normalise(src)
    assert len(out.rules) == len(src.rules) == 2
    assert out.rules[0].constraints == src.rules[0].constraints
    assert out.rules[1].constraints == src.rules[1].constraints
    assert any(a.kind == "other" and a.description == "normal form exploded" for a in out.ambiguities)
    assert MAX_NORMAL_RULES == 64


def test_random_numeric_policies_preserve_decide_equivalence() -> None:
    rng = random.Random(2603)
    for _ in range(40):
        rules = []
        for _ in range(rng.randint(1, 3)):
            mod = rng.choice(["permission", "prohibition"])
            nodes = []
            for _ in range(rng.randint(1, 2)):
                op = rng.choice(["eq", "neq", "gt", "gteq", "lt", "lteq"])
                val = str(rng.choice([5, 10, 15, 20]))
                if rng.random() < 0.4:
                    op2 = rng.choice(["eq", "gt", "gteq", "lt", "lteq"])
                    val2 = str(rng.choice([5, 10, 15, 20]))
                    nodes.append({
                        "kind": "logical",
                        "operator": rng.choice(["and", "or", "xone"]),
                        "constraints": [_c("count", op, val), _c("count", op2, val2)],
                    })
                else:
                    nodes.append(_c("count", op, val))
            rules.append({
                "modality": mod,
                "action": "read",
                "target": "dataset-7",
                "assignee": "team",
                "constraints": nodes,
            })
        ir_obj = _policy(*rules)
        norm = normalise(ir_obj)
        src_dump = ir_obj.model_dump(mode="json")
        norm_dump = norm.model_dump(mode="json")
        for x in (0, 5, 7, 10, 12, 15, 18, 20, 25):
            req = {"assignee": "team", "target": "dataset-7", "action": "read",
                   "context": {"count": ["integer", str(x)]}}
            assert decide(src_dump, req) == decide(norm_dump, req)


def test_pipeline_emit_normal_form_flag_trace_and_canonical_snapshot(monkeypatch, tmp_path) -> None:
    text = "Allow research-team to read dataset-7 when count >= 10."
    reply = ir(rule(
        action="read",
        target="dataset-7",
        assignee="research-team",
        quote=text,
        constraints=[_c("count", "gteq", "10")],
    ))

    # Default off: compiled doc keeps single lteq/gteq constraint.
    draft_off, doc_off = convert(text, Extractor(scripted_llm([reply])))
    assert draft_off.emit_normal_form is False
    assert len(doc_off["permission"]) == 1
    assert doc_off["permission"][0]["constraint"][0]["operator"] == "gteq"

    # Opt in: compiled doc has split simple permissions; trace stores both IRs.
    install(monkeypatch, [reply])
    scenario = {
        "scenario": "odrl_convert",
        "task": "convert",
        "environment": {
            "nl_docs": [{"id": "doc-1", "text": text, "label": "test"}],
            "odrl_records": [{"key": "odrl-policy-1"}],
        },
    }
    policy = {"schema_version": "policy/v1", "rules": []}
    trace_path = tmp_path / "trace.jsonl"
    state_path = tmp_path / "state.json"
    summary = run_conversion(
        scenario=scenario,
        policy=policy,
        seed=1,
        trace_path=str(trace_path),
        state_path=str(state_path),
        scenario_text="s",
        policy_text="p",
        emit_normal_form=True,
    )
    assert summary["draft"]["emit_normal_form"] is True
    assert len(summary["draft"]["ir"]["rules"]) == 1
    assert len(summary["draft"]["odrl"]["permission"]) == 2
    assert validate_odrl(summary["draft"]["odrl"]) == (True, [])

    events = load_trace_events(str(trace_path))
    norm_stages = [e for e in events if (e.get("detail") or {}).get("stage") == "normalise"]
    assert len(norm_stages) == 1
    assert len(norm_stages[0]["detail"]["normal_form_ir"]["rules"]) == 2

    # Byte-stable canonical JSON snapshot of normalised fixture
    fixed_ir = _policy({
        "modality": "permission",
        "action": "read",
        "target": "dataset-7",
        "assignee": "research-team",
        "constraints": [_c("count", "gteq", "10")],
    })
    norm_ir = normalise(fixed_ir)
    doc, _ = compile_ir(norm_ir)
    doc_again, _ = compile_ir(norm_ir)
    assert canonical_json(doc) == canonical_json(doc_again)
    expected_canonical = (
        '{"@context":"http://www.w3.org/ns/odrl.jsonld","permission":['
        '{"action":"read","assignee":"urn:nl2odrl:party:research-team",'
        '"constraint":[{"leftOperand":"count","operator":"eq","rightOperand":{"@type":"http://www.w3.org/2001/XMLSchema#integer","@value":"10"},"type":"Constraint"}],'
        '"target":"urn:nl2odrl:asset:dataset-7","type":"Permission"},'
        '{"action":"read","assignee":"urn:nl2odrl:party:research-team",'
        '"constraint":[{"leftOperand":"count","operator":"gt","rightOperand":{"@type":"http://www.w3.org/2001/XMLSchema#integer","@value":"10"},"type":"Constraint"}],'
        '"target":"urn:nl2odrl:asset:dataset-7","type":"Permission"}],'
        '"type":"Set","uid":"urn:nl2odrl:policy:fc2e128bc93d2ef0"}'
    )
    assert canonical_json(doc) == expected_canonical
