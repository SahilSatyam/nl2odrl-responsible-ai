"""Phase 4: benchmark integrity, metrics, gates, pins and the offline CI run."""
from __future__ import annotations

import json
import unicodedata
from collections import Counter

import pytest

from odrl_agent.bench import evaluator, gates, generate, metrics, oracle, pins, runner
from odrl_agent.dspy_opt import TRAINSET_ROWS
from odrl_agent.ir import PolicyIR


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return runner.load_cases()


@pytest.fixture(scope="module")
def offline(cases):
    results = runner.run_all(cases, lambda: oracle.oracle_llm(cases))
    return results, metrics.compute(results)


# ---- benchmark integrity ----------------------------------------------------------


def test_size_and_tier_shares(cases) -> None:
    assert 200 <= len(cases) <= 300
    shares = {t: n / len(cases) for t, n in Counter(c["tier"] for c in cases).items()}
    for tier, want in {1: 0.25, 2: 0.30, 3: 0.20, 4: 0.15, 5: 0.10}.items():
        assert abs(shares[tier] - want) <= 0.01, (tier, shares[tier])


def test_ids_and_texts_are_unique(cases) -> None:
    assert len({c["id"] for c in cases}) == len(cases)
    assert len({c["nl"] for c in cases}) == len(cases)


def test_manifest_matches_cases_and_regenerates_identically(cases) -> None:
    manifest = json.loads((generate.BENCH_DIR / "manifest.json").read_text("utf-8"))
    assert manifest["cases_sha"] == generate.cases_sha(cases)
    assert generate.build_manifest(generate.build_cases()) == manifest


def test_tampered_cases_are_rejected(tmp_path, cases) -> None:
    (tmp_path / "manifest.json").write_text(
        (generate.BENCH_DIR / "manifest.json").read_text("utf-8"), "utf-8")
    edited = json.loads(json.dumps(cases))
    edited[0]["nl"] += " extra"
    (tmp_path / "cases.json").write_text(json.dumps({"cases": edited}), "utf-8")
    with pytest.raises(ValueError, match="cases_sha"):
        runner.load_cases(tmp_path)


def test_a_group_never_spans_splits(cases) -> None:
    seen: dict[str, set[str]] = {}
    for case in cases:
        seen.setdefault(case["group"], set()).add(case["split"])
    assert all(len(splits) == 1 for splits in seen.values())
    assert {"dev", "test"} <= {s for splits in seen.values() for s in splits}


def test_no_leakage_from_trainset_or_baseline_v0(cases) -> None:
    train = {row["nl"].strip() for row in TRAINSET_ROWS}
    v0 = json.loads((generate.ROOT / "benchmarks/baseline_v0_cases.json").read_text("utf-8"))
    seen = train | {c["nl"].strip() for c in v0["cases"]}
    assert not seen & {c["nl"].strip() for c in cases}
    blob = " ".join(c["nl"] for c in cases).casefold()
    for token in ("dataset-7", "research-team", "attacker-client"):
        assert token not in blob


def test_gold_ir_is_schema_valid_and_quotes_are_in_the_text(cases) -> None:
    for case in cases:
        PolicyIR.model_validate(case["gold_ir"])
        for rule in case["gold_ir"]["rules"]:
            for span in rule["source_spans"]:
                assert span["quote"] in case["nl"], case["id"]


def test_tier5_uses_arbitrary_principals_and_includes_homoglyphs(cases) -> None:
    tier5 = [c for c in cases if c["tier"] == 5]
    principals = {c["expect"]["injection"]["principal"] for c in tier5}
    assert len(principals) >= 8
    assert any(any(unicodedata.name(ch).startswith("CYRILLIC") for ch in p
                   if ch.isalpha()) for p in principals)


def test_tier4_expects_clarification_not_a_grant(cases) -> None:
    for case in (c for c in cases if c["tier"] == 4):
        assert case["expect"]["kind"] == "clarify" and case["expect"]["ambiguity_kinds"]


# ---- offline run: oracle passes every gate ------------------------------------------


def test_oracle_run_passes_all_gates(offline) -> None:
    _results, m = offline
    rows = gates.evaluate(m, live=False)
    assert not gates.failed(rows), gates.failed(rows)
    assert m["cases"] == 240 and m["errors"] == 0


def test_oracle_conflict_and_unsat_cases_are_never_auto_published(offline) -> None:
    results, _ = offline
    conflicts = [r for r in results if r["case"]["expect"].get("conflict")]
    assert len(conflicts) == 12
    assert all(not r["draft"]["auto_publish"] for r in conflicts)


def test_oracle_policy_cases_in_tiers_1_2_are_auto_publishable(offline) -> None:
    results, _ = offline
    clean = [r for r in results if r["case"]["tier"] in (1, 2)]
    assert all(r["draft"]["status"] == "ready" for r in clean), [
        (r["case"]["id"], r["draft"]["status"], r["draft"]["alerts"], r["draft"]["errors"])
        for r in clean if r["draft"]["status"] != "ready"][:3]


def test_report_identity_block(cases, offline) -> None:
    results, _ = offline
    rep = runner.report(results, mode="offline", model="oracle")
    assert rep["passed"] and rep["cases_sha"] == generate.cases_sha(cases)
    assert rep["pins"] == pins.current() and set(rep["tiers"]) == {"1", "2", "3", "4", "5"}


# ---- gates must be able to go red -----------------------------------------------------


@pytest.mark.parametrize(
    ("mutant", "must_fail"),
    [
        ("overpermit", {"false_permission_rate"}),
        ("drop_prohibitions", {"missed_prohibition_rate"}),
        ("drop_constraints", {"constraint_f1", "duty_recorded"}),
    ],
)
def test_mutant_models_fail_the_gates(cases, mutant, must_fail) -> None:
    results = runner.run_all(cases, lambda: oracle.oracle_llm(cases, mutant))
    m = metrics.compute(results)
    broken = {r["metric"] for r in gates.failed(gates.evaluate(m, live=False))}
    assert must_fail <= broken, (mutant, broken)


def test_semantics_slice_passes_oracle_and_fails_set_as_eq_mutant() -> None:
    slice_cases = runner.load_semantics_slice()
    assert len(slice_cases) == 3

    good = runner.run_all(slice_cases, lambda: oracle.oracle_llm(slice_cases))
    m_good = metrics.compute(good)
    assert m_good["duty_recorded"] == 1.0
    assert m_good["normal_form_equivalence"] == 1.0
    assert m_good["behavioral_agreement"] == 1.0
    assert m_good["constraint_f1"] == 1.0
    applicable_good = [
        r for r in gates.evaluate(m_good, live=False) if r["value"] is not None
    ]
    assert not gates.failed(applicable_good), applicable_good

    bad = runner.run_all(slice_cases, lambda: oracle.oracle_llm(slice_cases, "set_as_eq"))
    m_bad = metrics.compute(bad)
    broken = {r["metric"] for r in gates.failed(gates.evaluate(m_bad, live=False))}
    assert {"normal_form_equivalence", "behavioral_agreement", "constraint_f1"} <= broken


def test_decide_uses_kb_and_action_refinements_and_ignores_duties() -> None:
    policy = {
        "policy_type": "Agreement",
        "assigner": "steward",
        "assignee": "team-a",
        "rules": [
            {
                "modality": "permission",
                "action": "print",
                "target": "ds",
                "assignee": "team-a",
                "constraints": [
                    {"kind": "atomic", "left_operand": "spatial", "operator": "isPartOf",
                     "right_operand": "Europe", "value_type": "string"},
                ],
                "action_refinements": [
                    {"kind": "atomic", "left_operand": "resolution", "operator": "lteq",
                     "right_operand": "300", "value_type": "integer"},
                ],
                "duties": [{"action": "attribute"}],
            }
        ],
    }
    ok_req = {"assignee": "team-a", "action": "print", "target": "ds",
              "context": {"spatial": ["string", "France"], "resolution": ["integer", "150"]}}
    bad_res = {"assignee": "team-a", "action": "print", "target": "ds",
               "context": {"spatial": ["string", "France"], "resolution": ["integer", "600"]}}
    bad_place = {"assignee": "team-a", "action": "print", "target": "ds",
                 "context": {"spatial": ["string", "bavaria"], "resolution": ["integer", "150"]}}
    assert evaluator.decide(policy, ok_req) is True
    assert evaluator.decide(policy, bad_res) is False
    assert evaluator.decide(policy, bad_place) is False


def test_prompt_injection_mutant_is_stopped_for_plain_names(cases) -> None:
    """A model that obeys the injection still yields no auto-published over-grant."""
    tier5 = [c for c in cases if c["tier"] == 5]
    results = runner.run_all(tier5, lambda: oracle.oracle_llm(cases, "obey_injection"))
    leaked = [r["case"]["id"] for r in results if r["draft"]["auto_publish"]
              and metrics.false_permissions(r)]
    assert not leaked, leaked


def test_prompt_injection_mutant_reports_residual_draft_level_grants(cases) -> None:
    """Draft-level FPR is the stricter gate: it must see any grant the gate only holds back."""
    tier5 = [c for c in cases if c["tier"] == 5]
    results = runner.run_all(tier5, lambda: oracle.oracle_llm(cases, "obey_injection"))
    held = [r for r in results if metrics.false_permissions(r)]
    assert all(not r["draft"]["auto_publish"] for r in held)


def test_regression_mode_blocks_only_on_baseline_drift(cases, offline) -> None:
    results, _ = offline
    good = runner.report(results, mode="offline", model="oracle")
    # A baseline equal to this run: regression mode passes; absolute gates are listed
    # but not enforced.
    base = {"metrics": dict(good["metrics"])}
    ok = runner.report(results, mode="offline", model="oracle", baseline=base,
                       enforce="regression")
    assert ok["passed"] and ok["enforce"] == "regression"
    assert all(r["enforced"] for r in ok["gates"] if "vs baseline" in r["metric"])
    assert not any(r["enforced"] for r in ok["gates"] if "vs baseline" not in r["metric"])
    # A baseline that this run is worse than: regression mode fails.
    better = {"metrics": dict(good["metrics"], checkpoint_f1=1.5)}
    bad = runner.report(results, mode="offline", model="oracle", baseline=better,
                        enforce="regression")
    assert not bad["passed"]
    with pytest.raises(ValueError, match="baseline"):
        runner.report(results, mode="offline", model="oracle", enforce="regression")


def test_regression_mode_does_not_hide_absolute_misses(cases) -> None:
    """A broken model fails 'all' but is only informational in 'regression' mode."""
    results = runner.run_all(cases, lambda: oracle.oracle_llm(cases, "overpermit"))
    flat = {"metrics": metrics.compute(results)}
    rep = runner.report(results, mode="offline", model="m", baseline=flat, enforce="regression")
    assert rep["passed"]  # identical to its own baseline
    missed = [r for r in rep["gates"] if not r["passed"] and not r["enforced"]]
    assert {"false_permission_rate", "checkpoint_f1"} <= {r["metric"] for r in missed}
    assert not runner.report(results, mode="offline", model="m")["passed"]


def test_cli_regression_mode_needs_a_baseline(capsys) -> None:
    from odrl_agent.bench.__main__ import main

    assert main(["run", "--tier", "1", "--enforce", "regression"]) == 2
    assert "needs --baseline" in capsys.readouterr().err


def test_regression_gate_blocks_fpr_and_f1_drift() -> None:
    base = {"false_permission_rate": 0.0, "tier5_false_permission_rate": 0.0, "missed_prohibition_rate": 0.0,
            "checkpoint_f1": 0.95, "constraint_f1": 0.93}
    same = gates.regressions(dict(base), base)
    assert not gates.failed(same)
    worse = dict(base, false_permission_rate=0.006, checkpoint_f1=0.93)
    names = {r["metric"] for r in gates.failed(gates.regressions(worse, base))}
    assert names == {"false_permission_rate vs baseline", "checkpoint_f1 vs baseline"}
    assert gates.failed(gates.regressions({}, base))  # missing data fails closed


def test_missing_metric_fails_closed() -> None:
    assert any(not r["passed"] for r in gates.evaluate({}, live=False))


def test_latency_gates_apply_only_to_live_runs() -> None:
    assert not any(r["metric"].startswith("p95") for r in gates.evaluate({}, live=False))
    assert sum(r["metric"].startswith("p95") for r in gates.evaluate({}, live=True)) == 2


# ---- evaluator and metric units --------------------------------------------------------

POLICY = {
    "assignee": None,
    "rules": [
        {"modality": "permission", "action": "use", "target": "urn:data:ds", "assignee": "team-a",
         "constraints": [{"kind": "atomic", "left_operand": "count", "operator": "lteq",
                          "right_operand": "3", "value_type": "integer"}]},
        {"modality": "prohibition", "action": "distribute", "target": "ds", "assignee": "team-a"},
    ],
}


def _req(action="use", who="team-a", target="ds", count="2") -> dict:
    return {"assignee": who, "action": action, "target": target,
            "context": {"count": ["integer", count]}}


def test_decide_handles_constraints_hierarchy_and_prohibition() -> None:
    assert evaluator.decide(POLICY, _req())
    assert not evaluator.decide(POLICY, _req(count="4"))
    assert not evaluator.decide(POLICY, _req(who="team-b"))
    assert not evaluator.decide(POLICY, _req(target="other"))
    assert evaluator.decide(POLICY, _req(action="read"))  # use includes read
    assert not evaluator.decide(POLICY, _req(action="distribute"))  # prohibited
    assert not evaluator.decide(None, _req())


def test_agreement_detects_a_dropped_constraint() -> None:
    loose = json.loads(json.dumps(POLICY))
    loose["rules"][0]["constraints"] = []
    agree, total = evaluator.agreement(POLICY, loose)
    assert 0 < agree < total
    assert evaluator.agreement(POLICY, POLICY) == (total, total)
    assert total <= evaluator.MAX_REQUESTS


def test_false_permission_counts_widening_but_not_narrowing() -> None:
    case = {"gold_ir": {"rules": [{"modality": "permission", "action": "use", "target": "a",
                                   "assignee": "p"}]}, "expect": {}, "tier": 1}

    def result(action: str, assignee: str = "p") -> dict:
        ir = {"rules": [{"modality": "permission", "action": action, "target": "a",
                         "assignee": assignee}]}
        return {"case": case, "draft": {"ir": ir}}

    assert not metrics.false_permissions(result("read"))  # narrower than gold
    assert metrics.false_permissions(result("use", "someone-else"))
    forbidden = dict(case, expect={"forbidden_principals": ["p"]})
    assert metrics.false_permissions({"case": forbidden, "draft": result("use")["draft"]})


def test_missed_prohibition_and_attribution() -> None:
    case = {"gold_ir": {"rules": [{"modality": "prohibition", "action": "print", "target": "a",
                                   "assignee": "p"}]}}
    kept = {"case": case, "draft": {"ir": case["gold_ir"]}}
    gone = {"case": case, "draft": {"ir": {"rules": []}}}
    assert metrics.missed_prohibitions(kept) == (0, 1)
    assert metrics.missed_prohibitions(gone) == (1, 1)
    draft = {"ir": {"rules": [
        {"source_spans": [{"quote": "may read", "start": 2, "end": 10}]},
        {"source_spans": [{"quote": "may read"}]}]}}
    assert metrics.attribution(draft, "A may read x") == (1, 2)


def test_checkpoint_f1_is_one_for_gold_and_zero_for_empty() -> None:
    case = {"gold_ir": POLICY}
    assert metrics.checkpoint_scores([{"case": case, "draft": {"ir": POLICY}}])[
        "checkpoint_f1"] == 1.0
    assert metrics.checkpoint_scores([{"case": case, "draft": {"ir": None}}])[
        "checkpoint_f1"] == 0.0


# ---- pins ---------------------------------------------------------------------------------


def test_pins_match_current_prompt_pack_and_model() -> None:
    assert pins.drift() == {}, (
        "extractor prompt/model/IR schema changed: re-run the live benchmark, then "
        "`python -m odrl_agent.bench pins --write`")


def test_pins_detect_prompt_change(monkeypatch) -> None:
    from odrl_agent import extractor

    monkeypatch.setattr(extractor, "SYSTEM_PROMPT", extractor.SYSTEM_PROMPT + " ")
    assert "extractor_prompt_pack_sha" in pins.drift()


def test_cli_verify_and_pins_exit_zero() -> None:
    from odrl_agent.bench.__main__ import main

    assert main(["verify"]) == 0
    assert main(["pins"]) == 0


def test_cli_offline_run_writes_report(tmp_path) -> None:
    from odrl_agent.bench.__main__ import main

    out = tmp_path / "r.json"
    assert main(["run", "--tier", "1", "--out", str(out)]) != 2
    rep = json.loads(out.read_text("utf-8"))
    assert rep["mode"] == "offline" and rep["metrics"]["cases"] == 60


def test_cli_live_without_credentials_exits_2(monkeypatch) -> None:
    from odrl_agent.bench.__main__ import main

    for var in ("LLAMA_API_KEY", "LLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    assert main(["run", "--mode", "live", "--limit", "1"]) == 2
