"""Pure IR carving pass: action closure and prohibition subtraction (Phase 1, Task 1.2)."""
from __future__ import annotations

from scripted_llm import ir, rule, scripted_llm

from odrl_agent.carve import carve
from odrl_agent.extractor import Extractor
from odrl_agent.ir import PolicyIR
from odrl_agent.odrl import validate_odrl
from odrl_agent.pipeline import convert, widening
from odrl_agent.reasoning import DEONTIC_CONFLICT, HIERARCHY_CONFLICT, OBLIGATION_CONFLICT, analyze


def _c(lo: str, op: str, val: str, vt: str = "string") -> dict:
    return {
        "kind": "atomic",
        "left_operand": lo,
        "operator": op,
        "right_operand": val,
        "value_type": vt,
    }


def _policy(*rules: dict, **extra) -> PolicyIR:
    return PolicyIR.model_validate({"rules": list(rules), **extra})


def test_carve_subtracts_overlapping_prohibition_constraint() -> None:
    src = _policy(
        {
            "modality": "permission",
            "action": "read",
            "target": "dataset-7",
            "assignee": "research-team",
        },
        {
            "modality": "prohibition",
            "action": "read",
            "target": "dataset-7",
            "assignee": "research-team",
            "constraints": [_c("purpose", "eq", "commercial")],
        },
    )
    assert [f.code for f in analyze(src).findings] == [DEONTIC_CONFLICT]

    carved = carve(src)
    assert analyze(carved).ok
    perms = [r for r in carved.rules if r.modality == "permission"]
    assert len(perms) == 1
    assert perms[0].constraints[0].left_operand == "purpose"
    assert perms[0].constraints[0].operator == "neq"
    assert perms[0].constraints[0].right_operand == "commercial"
    # Source IR was not mutated.
    assert src.rules[0].constraints == []


def test_carve_drops_unconditionally_prohibited_permission() -> None:
    src = _policy(
        {"modality": "permission", "action": "read", "target": "dataset-7", "assignee": "team"},
        {"modality": "prohibition", "action": "read", "target": "dataset-7", "assignee": "team"},
    )
    carved = carve(src)
    assert [r.modality for r in carved.rules] == ["prohibition"]
    assert analyze(carved).ok


def test_carve_copies_obligations_and_duties_unchanged() -> None:
    src = _policy(
        {
            "modality": "permission",
            "action": "read",
            "target": "dataset-7",
            "assignee": "team",
            "duties": [{"action": "attribute", "target": "dataset-7"}],
        },
        {
            "modality": "prohibition",
            "action": "read",
            "target": "dataset-7",
            "assignee": "team",
            "constraints": [_c("purpose", "eq", "commercial")],
        },
        {
            "modality": "obligation",
            "action": "delete",
            "target": "dataset-7",
            "assignee": "team",
            "constraints": [_c("count", "gteq", "1", "integer")],
        },
    )
    carved = carve(src)
    perms = [r for r in carved.rules if r.modality == "permission"]
    obls = [r for r in carved.rules if r.modality == "obligation"]
    assert len(perms) == 1 and [d.action for d in perms[0].duties] == ["attribute"]
    assert len(obls) == 1 and obls[0].constraints[0].right_operand == "1"


def test_action_closure_permissions_only_materialises_narrower_actions_never_prohibitions() -> None:
    play_only = _policy(
        {"modality": "permission", "action": "play", "target": "video-1", "assignee": "team"},
        {"modality": "prohibition", "action": "transfer", "target": "video-1", "assignee": "team"},
    )
    carved = carve(play_only)
    perm_actions = {r.action for r in carved.rules if r.modality == "permission"}
    prohib_actions = {r.action for r in carved.rules if r.modality == "prohibition"}
    assert perm_actions == {"play", "display"}
    # Never adds implied prohibitions such as sell or give.
    assert prohib_actions == {"transfer"}

    no_closure = play_only.model_copy(update={"action_closure": "none"})
    carved_none = carve(no_closure)
    assert {r.action for r in carved_none.rules if r.modality == "permission"} == {"play"}


def test_pipeline_emit_carved_flag_defaults_off_and_compiles_carved_when_enabled() -> None:
    text = "Allow the research team to play dataset-7."
    reply = ir(rule(action="play", quote="Allow the research team to play dataset-7"))

    draft_off, doc_off = convert(text, Extractor(scripted_llm([reply])))
    assert draft_off.emit_carved is False
    assert len(doc_off["permission"]) == 1
    assert doc_off["permission"][0]["action"] == "play"

    draft_on, doc_on = convert(text, Extractor(scripted_llm([reply])), emit_carved=True)
    assert draft_on.emit_carved is True
    assert validate_odrl(doc_on) == (True, [])
    # Extractor IR on the draft stays uncarved; compiled doc has implied display.
    assert [r["action"] for r in draft_on.ir["rules"]] == ["play"]
    assert {p["action"] for p in doc_on["permission"]} == {"play", "display"}


def test_widening_still_rejects_conflict_strategy_only_fix() -> None:
    conflict = _policy(
        {"modality": "permission", "action": "read", "target": "dataset-7", "assignee": "team"},
        {"modality": "prohibition", "action": "read", "target": "dataset-7", "assignee": "team"},
    )
    strategy_only = conflict.model_copy(update={"conflict_strategy": "prohibit"})
    reasons = widening(conflict, strategy_only)
    assert reasons and any("conflict_strategy" in r for r in reasons)


def test_walkthrough_sentence_shape_has_no_attribute_conflict_after_carve() -> None:
    eu = _c("spatial", "eq", "European Union")
    until = _c("dateTime", "lteq", "2026-12-31", "date")
    after_expiry = _c("dateTime", "gt", "2026-12-31", "date")
    commercial = _c("purpose", "eq", "commercial")
    duties = [
        {"action": "attribute", "target": "2026 Healthcare Analytics Dataset"},
        {"action": "delete", "target": "2026 Healthcare Analytics Dataset", "constraints": [after_expiry]},
    ]
    walkthrough = _policy(
        {
            "modality": "permission",
            "action": "read",
            "target": "2026 Healthcare Analytics Dataset",
            "constraints": [eu, until],
            "duties": duties,
        },
        {
            "modality": "permission",
            "action": "derive",
            "target": "2026 Healthcare Analytics Dataset",
            "constraints": [eu, until],
            "duties": duties,
        },
        {
            "modality": "prohibition",
            "action": "distribute",
            "target": "2026 Healthcare Analytics Dataset",
        },
        {
            "modality": "prohibition",
            "action": "use",
            "target": "2026 Healthcare Analytics Dataset",
            "constraints": [commercial],
        },
        policy_type="Agreement",
        assigner="Acme Data Corp",
        assignee="University Research Labs",
    )
    carved = carve(walkthrough)
    report = analyze(carved)
    codes = {f.code for f in report.findings}
    assert OBLIGATION_CONFLICT not in codes
    assert HIERARCHY_CONFLICT not in codes
    assert report.ok
