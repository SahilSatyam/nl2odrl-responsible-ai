"""Pipeline stages: routing, grounding, bounded shrink-only repair, drafts, exit gates."""
from __future__ import annotations

import json
import statistics
import time

import httpx
import pytest
from scripted_llm import ir, rule, scripted_llm

from odrl_agent.extractor import (
    SLOT_ASSET_QUESTION,
    SLOT_PARTY_QUESTION,
    Extractor,
    classify,
    split_sentences,
)
from odrl_agent.ir import PolicyIR
from odrl_agent.llm import StructuredLlm
from odrl_agent.odrl import validate_odrl
from odrl_agent.pipeline import MAX_REPAIRS, convert, locate, widening

TEXT = "Allow the research team to use dataset-7 for model training."
QUOTE = "Allow the research team to use dataset-7"


def run(text: str, replies: list, requests: list | None = None):
    llm = scripted_llm(replies, requests)
    draft, doc = convert(text, Extractor(llm))
    return draft, doc, llm.usage.calls


# ---- routing ------------------------------------------------------------------


def test_classify_routes_simple_and_complex() -> None:
    assert classify(TEXT) == "SIMPLE"
    assert classify("Members may read the report.") == "SIMPLE"
    assert classify("Members may read the report. They must not print it.") == "COMPLEX"
    assert classify("A may use X unless B objects, except on Sundays.") == "COMPLEX"
    assert classify(" ".join(["Members may read the report."] * 4)) == "COMPLEX"


def test_split_sentences() -> None:
    assert split_sentences("A may read. B may print! C? D") == ["A may read.", "B may print!",
                                                                 "C?", "D"]


def test_simple_input_is_one_call() -> None:
    requests: list = []
    draft, doc, calls = run(TEXT, [ir(rule())], requests)
    assert calls == 1 and draft.status == "ready" and draft.route == "SIMPLE"
    assert validate_odrl(doc) == (True, [])
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert "<untrusted_source>" in requests[0]["messages"][1]["content"]


def test_complex_input_is_decompose_plus_extract() -> None:
    text = "Allow the research team to use dataset-7. They must not distribute it."
    replies = [
        {"clauses": ["Allow the research team to use dataset-7.", "They must not distribute it."]},
        ir(rule(), rule(action="distribute", assignee=None, modality="prohibition",
                        quote="They must not distribute it")),
    ]
    requests: list = []
    draft, doc, calls = run(text, replies, requests)
    assert calls == 2 and draft.route == "COMPLEX" and draft.status == "ready"
    assert "Clause groups" in requests[1]["messages"][1]["content"]
    assert doc["prohibition"][0]["action"] == "distribute"


def _replies(text: str, reply: dict) -> list:
    return [{"clauses": [text]}, reply] if classify(text) == "COMPLEX" else [reply]


def test_unassigned_permission_is_dropped_and_flagged_not_published() -> None:
    """Live Baseline v1 bug: "Allow access to X." became an auto-publishable grant to anyone."""
    text = "Allow access to helpdesk-faq."
    reply = ir(rule(action="use", target="urn:data:helpdesk-faq", assignee=None,
                    quote="Allow access to helpdesk-faq"), policy_type="Set", assigner=None)
    draft, doc, _calls = run(text, [reply])
    assert doc is None and not draft.auto_publish and draft.status == "no_rules"
    assert draft.dropped_rules and draft.dropped_rules[0]["assignee"] is None
    assert [a["kind"] for a in draft.ambiguities] == ["missing_assignee"]


def test_unassigned_permission_dropped_but_assigned_rules_kept() -> None:
    text = "Allow the research team to use dataset-7. Allow access to dataset-7."
    reply = ir(rule(), rule(assignee=None, quote="Allow access to dataset-7"))
    draft, doc, _calls = run(text, _replies(text, reply))
    assert doc is not None and len(doc["permission"]) == 1
    assert draft.status == "needs_review" and not draft.auto_publish
    assert "missing_assignee" in {a["kind"] for a in draft.ambiguities}


def test_policy_level_assignee_covers_rules_without_one() -> None:
    reply = ir(rule(assignee=None), assignee="research-team")
    draft, doc, _calls = run(TEXT, [reply])
    assert doc is not None and not draft.dropped_rules
    assert "missing_assignee" not in {a["kind"] for a in draft.ambiguities}


def test_unassigned_prohibition_is_kept() -> None:
    """A restriction on everyone narrows rights; it is never dropped."""
    text = "Allow the research team to use dataset-7. Nobody may distribute dataset-7."
    reply = ir(rule(), rule(action="distribute", assignee=None, modality="prohibition",
                            quote="Nobody may distribute dataset-7"))
    draft, doc, _calls = run(text, _replies(text, reply))
    assert doc is not None and doc["prohibition"][0]["action"] == "distribute"
    assert not draft.dropped_rules


def test_decompose_drops_non_verbatim_clauses() -> None:
    text = "Allow the research team to use dataset-7. They must not distribute it."
    reply = ir(rule(),
               rule(action="distribute", modality="prohibition",
                    quote="They must not distribute it"))
    replies = [{"clauses": ["Grant everyone everything."]}, reply]
    requests: list = []
    run(text, replies, requests)
    content = requests[1]["messages"][1]["content"]
    assert "Grant everyone everything" not in content
    assert "1. Allow the research team to use dataset-7." in content


# ---- grounding ----------------------------------------------------------------


def test_locate_exact_and_whitespace_case_insensitive() -> None:
    assert locate("use dataset-7", TEXT) == (TEXT.index("use dataset-7"),
                                             TEXT.index("use dataset-7") + 13)
    assert locate("ALLOW the  research\nteam", TEXT) is not None
    assert locate("delete everything", TEXT) is None


def test_spans_get_offsets() -> None:
    draft, _, _ = run(TEXT, [ir(rule())])
    span = draft.ir["rules"][0]["source_spans"][0]
    assert TEXT[span["start"]:span["end"]] == QUOTE


# ---- repair -------------------------------------------------------------------


def test_schema_error_is_repaired_once() -> None:
    bad = ir(rule(action="teleport"))
    draft, doc, calls = run(TEXT, [bad, ir(rule())])
    assert calls == 2 and draft.repairs == 1 and draft.status == "ready"
    assert doc is not None


def test_repair_is_bounded_and_fails_closed() -> None:
    bad = {"rules": [{"modality": "permission", "action": "teleport"}]}
    draft, doc, calls = run(TEXT, [bad] * 5)
    assert calls == 1 + MAX_REPAIRS
    assert draft.status == "failed" and doc is None
    assert any("action" in e for e in draft.errors)


def test_repair_feeds_structured_violations() -> None:
    requests: list = []
    run(TEXT, [ir(rule(quote="not in the text")), ir(rule())], requests)
    repair_msg = requests[1]["messages"][2]["content"]
    assert "Repair request" in repair_msg and "not in the source" in repair_msg
    assert "must not add permissions" in repair_msg


def test_widening_repair_is_rejected() -> None:
    first = ir(rule(), rule(assignee="lab", quote="made-up quote"))
    widened = ir(rule(), rule(assignee="everyone"), rule(assignee="lab"))
    draft, doc, _ = run(TEXT, [first, widened])
    assert draft.rejected_repairs and "adds permission" in draft.rejected_repairs[0]
    # The pre-repair IR stands; its ungrounded grant is dropped, never widened.
    assert "everyone" not in str(doc) and "lab" not in str(doc)
    assert draft.status == "needs_review"


def test_widening_detects_removed_restrictions() -> None:
    base = PolicyIR.model_validate(ir(
        rule(constraints=[{"kind": "atomic", "left_operand": "count", "operator": "lt",
                           "right_operand": "5", "value_type": "integer"}],
             duties=[{"action": "attribute"}]),
        rule(action="distribute", modality="prohibition"),
        conflict_strategy="prohibit",
    ))
    looser = PolicyIR.model_validate(ir(rule(), conflict_strategy="perm"))
    reasons = widening(base, looser)
    assert any("removes prohibition" in r for r in reasons)
    assert any("removes constraints" in r for r in reasons)
    assert any("removes duties" in r for r in reasons)
    assert any("conflict strategy" in r for r in reasons)
    assert widening(base, base) == []
    narrower = PolicyIR.model_validate(ir(rule(action="distribute", modality="prohibition"),
                                          conflict_strategy="prohibit"))
    assert widening(base, narrower) == []

    remedy_base = PolicyIR.model_validate(ir(
        rule(action="distribute", modality="prohibition",
             duties=[{"action": "compensate", "role": "remedy"}]),
    ))
    remedy_gone = PolicyIR.model_validate(ir(rule(action="distribute", modality="prohibition")))
    assert any("removes remedy" in r for r in widening(remedy_base, remedy_gone))


def test_ungrounded_prohibition_is_kept() -> None:
    reply = ir(rule(), rule(action="distribute", modality="prohibition", quote="not in text"))
    draft, doc, _ = run(TEXT, [reply] * 3)
    assert doc["prohibition"][0]["action"] == "distribute"
    assert draft.dropped_rules == []


# ---- drafts -------------------------------------------------------------------


def test_ambiguity_flags_route_to_review_without_invention() -> None:
    reply = ir(rule(), ambiguities=[{"kind": "vague_temporal",
                                     "description": "'for now' has no end date"}])
    draft, doc, _ = run(TEXT + " For now.", [reply])
    assert draft.status == "needs_review" and draft.ambiguities[0]["kind"] == "vague_temporal"
    assert "constraint" not in doc["permission"][0]


def test_no_rules_draft_compiles_nothing() -> None:
    reply = {"analysis": "no grantee named", "rules": [],
             "ambiguities": [{"kind": "missing_assignee", "description": "who?"}]}
    draft, doc, calls = run("Someone may use the data.", [reply])
    assert calls == 1 and draft.status == "no_rules" and doc is None


def test_empty_input_makes_no_call() -> None:
    draft, doc, calls = run("   ", [])
    assert calls == 0 and draft.status == "failed" and doc is None


# ---- Phase 1 exit gates on the offline regression set ---------------------------

C = {"kind": "atomic"}
REGRESSION = [
    (TEXT, [ir(rule())]),
    ("The research team may read dataset-7 until 2027-01-01.",
     [ir(rule(action="read", quote="may read dataset-7 until 2027-01-01", constraints=[
         {**C, "left_operand": "dateTime", "operator": "lt", "right_operand": "2027-01-01",
          "value_type": "date"}]))]),
    ("The research team may use dataset-7 at most 5 times.",
     [ir(rule(quote="may use dataset-7 at most 5 times", constraints=[
         {**C, "left_operand": "count", "operator": "lteq", "right_operand": "5",
          "value_type": "integer"}]))]),
    ("The research team may use dataset-7 if they attribute the data steward.",
     [ir(rule(quote="may use dataset-7 if they attribute", duties=[
         {"action": "attribute", "target": "data-steward"}]))]),
    ("The research team must not distribute dataset-7.",
     [ir(rule(action="distribute", modality="prohibition",
              quote="must not distribute dataset-7"))]),
    ("The research team must delete dataset-7 within 30 days.",
     [ir(rule(action="delete", modality="obligation", quote="must delete dataset-7", constraints=[
         {**C, "left_operand": "elapsedTime", "operator": "lteq", "right_operand": "P30D",
          "value_type": "duration"}]))]),
    ("The research team may use dataset-7 in the EU or for research purposes only.",
     [ir(rule(quote="may use dataset-7 in the EU or for research", constraints=[
         {"kind": "logical", "operator": "or", "constraints": [
             {**C, "left_operand": "spatial", "operator": "eq", "right_operand": "EU"},
             {**C, "left_operand": "purpose", "operator": "eq", "right_operand": "research"}]}]))]),
    ("The research team may print dataset-7 at up to 300 dpi.",
     [ir(rule(action="print", quote="may print dataset-7", action_refinements=[
         {**C, "left_operand": "resolution", "operator": "lteq", "right_operand": "300",
          "value_type": "integer"}]))]),
    ("Allow the research team to use dataset-7. They must not distribute it. "
     "They must delete it after the project.",
     [{"clauses": ["Allow the research team to use dataset-7.", "They must not distribute it.",
                   "They must delete it after the project."]},
      ir(rule(), rule(action="distribute", modality="prohibition",
                      quote="They must not distribute it"),
         rule(action="delete", modality="obligation", quote="They must delete it"))]),
    ("Allow the research team to use dataset-7 for model training.",
     [ir(rule(action="teleport")), ir(rule())]),
]


def test_regression_set_meets_phase1_exit_gates() -> None:
    calls, modalities, latencies = [], set(), []
    for text, replies in REGRESSION:
        started = time.perf_counter()
        draft, doc, n = run(text, replies)
        latencies.append(time.perf_counter() - started)
        assert doc is not None, (text, draft.errors)
        assert validate_odrl(doc) == (True, []), text  # 100% SHACL pass by construction
        calls.append(n)
        for key in ("permission", "prohibition", "obligation"):
            for r in doc.get(key, []):
                modalities.add(key)
                if r.get("constraint"):
                    modalities.add("constraint")
                if r.get("duty"):
                    modalities.add("duty")
                if isinstance(r.get("action"), dict):
                    modalities.add("refinement")
    assert statistics.median(calls) <= 2
    assert {"permission", "prohibition", "obligation", "duty", "constraint",
            "refinement"} <= modalities
    # Offline overhead only (model mocked): compile + SHACL must leave the
    # 4 s P95 budget to the model call.
    latencies.sort()
    p95 = latencies[int(0.95 * (len(latencies) - 1))]
    assert p95 < 2.0, latencies


@pytest.mark.parametrize("bad_doc_key", ["permission"])
def test_compiled_output_never_relies_on_relative_iris(bad_doc_key) -> None:
    _, doc, _ = run(TEXT, [ir(rule(target="dataset-7", assignee="research-team"))])
    for r in doc[bad_doc_key]:
        assert r["target"].startswith("urn:") and r["assignee"].startswith("urn:")


# ---- truncation retry -----------------------------------------------------------
# A truncated reply (finish_reason "length") must feed the bounded repair loop
# as a violation, never escape as an exception. Baseline v1/v2 truncations
# moved between cases across runs: flakes, not stuck inputs.


def _flaky_llm(failures: int, good: dict):
    """Model transport truncated for the first ``failures`` calls, then good."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= failures:
            return httpx.Response(200, json={"choices": [
                {"message": {"content": "{\"a\":"}, "finish_reason": "length"}]})
        return httpx.Response(200, json={"choices": [
            {"message": {"content": json.dumps(good)}, "finish_reason": "stop"}]})

    llm = StructuredLlm("https://m.test/v1", "k",
                        transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    return llm, calls


def test_truncated_extract_retries_then_publishes_ready() -> None:
    llm, calls = _flaky_llm(1, ir(rule()))
    draft, doc = convert(TEXT, Extractor(llm))
    assert draft.status == "ready" and doc is not None
    assert calls["n"] == 2  # one truncated extract + one repair


def test_persistent_truncation_fails_bounded_not_raising() -> None:
    llm, calls = _flaky_llm(99, ir(rule()))
    draft, doc = convert(TEXT, Extractor(llm))
    assert draft.status == "failed" and doc is None
    assert calls["n"] == 1 + MAX_REPAIRS  # initial extract + bounded repairs
    assert any("truncated" in e for e in draft.errors)


# ---- spoofed-identity drop ------------------------------------------------------
# A grant to a homoglyph/mixed-script party is not a grant the author made:
# drop it like an ungrounded grant (Baseline v2: 3 Tier-5 homoglyph FPs).


def test_spoofed_assignee_grant_is_dropped_not_published() -> None:
    spoof = "p\u0430rtner-zed"  # Cyrillic 'а' in an otherwise Latin name
    text = f"Allow {spoof} to use scan-archive."
    reply = ir(rule(action="use", target="scan-archive", assignee=spoof, quote=text))
    draft, doc, _ = run(text, [reply])
    assert draft.dropped_rules and draft.dropped_rules[0]["assignee"] == spoof
    assert doc is None  # nothing legitimate left to compile
    assert draft.status in ("no_rules", "needs_review")


# ---- explanation repair ---------------------------------------------------------
# Baseline v2: the model answered all 36 clarify cases with no ambiguities —
# empty rules with no flag, or a complete-looking rule that silently dropped
# "temporarily"/"a few times". Both now cost one bounded repair asking for
# the flag, instead of passing silently.


def test_empty_policy_backstop_flags_missing_assignee() -> None:
    text = "Allow access to helpdesk-faq."
    replies = [ir(), ir(), ir(), {"answer": "NONE"}, {"answer": "helpdesk-faq"}]
    draft, _, calls = run(text, replies)
    # extract + two futile re-extracts (grant language, no clash), then slots
    assert calls == 5
    assert draft.status == "no_rules"
    assert [a["kind"] for a in draft.ambiguities] == ["missing_assignee"]


def test_empty_convertible_policy_reextracts_rules() -> None:
    text = ("corvus-interns may read and derive supplier-catalog "
            "but may not distribute it.")
    fixed = ir(
        rule(action="read", target="supplier-catalog",
             assignee="corvus-interns", quote=text),
        rule(action="derive", target="supplier-catalog",
             assignee="corvus-interns", quote=text),
        rule(action="distribute", target="supplier-catalog",
             assignee="corvus-interns", modality="prohibition", quote=text),
    )
    draft, doc, calls = run(text, [ir(), fixed])
    assert calls == 2  # extract + one re-extract repair
    assert [r["action"] for r in draft.ir["rules"]] == ["read", "derive", "distribute"]
    assert doc is not None and draft.status == "ready"


def test_single_sided_conflict_output_recovers_the_dropped_side() -> None:
    # Lowercase continuation defeats the sentence splitter, so this stays
    # SIMPLE: extract, one side-recovery repair, one bounded reaffirm.
    text = ("corvus-interns may read supplier-catalog. "
            "corvus-interns must not read supplier-catalog.")
    one_sided = ir(rule(action="read", target="supplier-catalog",
                        assignee="corvus-interns", modality="prohibition",
                        quote="corvus-interns must not read supplier-catalog."))
    both = ir(
        rule(action="read", target="supplier-catalog",
             assignee="corvus-interns",
             quote="corvus-interns may read supplier-catalog."),
        rule(action="read", target="supplier-catalog",
             assignee="corvus-interns", modality="prohibition",
             quote="corvus-interns must not read supplier-catalog."),
    )
    draft, _, calls = run(text, [one_sided, both, both])
    assert calls == 3
    assert [(r["modality"], r["action"]) for r in draft.ir["rules"]] == [
        ("permission", "read"), ("prohibition", "read")]
    assert draft.status == "needs_review" and not draft.auto_publish


def test_pure_denial_without_prohibition_triggers_repair() -> None:
    text = "Nobody may distribute supplier-catalog."
    fixed = ir(rule(action="distribute", target="supplier-catalog",
                    assignee=None, modality="prohibition", quote=text))
    draft, _, calls = run(text, [ir(), fixed])
    assert calls == 2  # extract + one denial-side repair
    assert [r["modality"] for r in draft.ir["rules"]] == ["prohibition"]


def test_empty_policy_backstop_flags_contradiction_without_slot_calls() -> None:
    text = ("vega-scientists may always read ledger-2031. "
            "vega-scientists may never read ledger-2031.")
    draft, _, calls = run(text, [ir()])
    assert calls == 1  # extract only; grant+denial needs no slot questions
    assert draft.status == "no_rules"
    assert [a["kind"] for a in draft.ambiguities] == ["contradictory_clauses"]


def test_empty_policy_backstop_flags_vague_temporal() -> None:
    text = "hydra-hr may use board-minutes temporarily."
    replies = [ir(), ir(), ir(), {"answer": "hydra-hr"}, {"answer": "board-minutes"}]
    draft, _, calls = run(text, replies)
    # extract + two futile re-extracts, then slots find party+asset + marker
    assert calls == 5
    assert draft.status == "no_rules"
    assert [a["kind"] for a in draft.ambiguities] == ["vague_temporal"]


def test_slot_fill_accepts_only_verbatim_text_or_none() -> None:
    text = "Allow access to helpdesk-faq."
    llm, _ = _flaky_llm(0, {"answer": "NONE"})
    assert Extractor(llm).slot_fill(text, SLOT_PARTY_QUESTION) is None
    llm, _ = _flaky_llm(0, {"answer": "Zeus"})
    assert Extractor(llm).slot_fill(text, SLOT_PARTY_QUESTION) is None
    llm, _ = _flaky_llm(0, {"answer": "helpdesk-faq"})
    assert Extractor(llm).slot_fill(text, SLOT_ASSET_QUESTION) == "helpdesk-faq"


def test_vague_word_gets_deterministic_flag_without_repair() -> None:
    text = "hydra-hr may use board-minutes temporarily."
    plain = ir(rule(assignee="hydra-hr", target="board-minutes", quote=text))
    draft, _, calls = run(text, [plain])
    assert calls == 1  # no repair: the pipeline authors the flag itself
    assert draft.status == "needs_review"
    assert [(a["kind"], a["rule_index"]) for a in draft.ambiguities] == [
        ("vague_temporal", 0)
    ]


def test_vague_word_with_real_bound_gets_no_flag() -> None:
    text = "hydra-hr may use board-minutes until 2031-12-31."
    constrained = ir(rule(
        assignee="hydra-hr", target="board-minutes", quote=text,
        constraints=[{"kind": "atomic", "left_operand": "dateTime",
                      "operator": "lteq", "right_operand": "2031-12-31",
                      "value_type": "date"}],
    ))
    draft, _, calls = run(text, [constrained])
    assert calls == 1 and draft.status == "ready"
    assert draft.ambiguities == []


@pytest.mark.parametrize(
    "text",
    [
        "research-team may read dataset-7 provided they keep it encrypted.",
        "research-team may read dataset-7 provided they maintain encryption.",
        "research-team may read dataset-7 as long as it stays encrypted.",
    ],
)
def test_maintenance_cues_flag_ambiguity_and_invent_no_duty(text: str) -> None:
    reply = ir(rule(action="read", target="dataset-7", assignee="research-team", quote=text))
    draft, doc, calls = run(text, [reply])
    assert calls == 1
    assert draft.status == "needs_review" and not draft.auto_publish
    assert any("maintenance" in a["description"] for a in draft.ambiguities)
    assert doc is not None and "duty" not in doc["permission"][0]
