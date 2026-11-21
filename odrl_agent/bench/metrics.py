"""Benchmark metrics (Phase 4.2). Pure functions over case results.

A *result* is ``{"case": <case>, "draft": <PolicyDraft dict>, "calls": int,
"wall_s": float, "error": str | None}``. Predictions are read from the
draft's final IR (``draft["ir"]``), the same IR the compiler consumed.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from typing import Any

from odrl_agent.bench.evaluator import agreement, name_key
from odrl_agent.grounder import normalize_name
from odrl_agent.reasoning import includes

CATEGORIES = ("entity", "action", "modality", "constraint")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _constraint_key(node: dict[str, Any]) -> str:
    if node.get("kind") == "logical":
        inner = sorted(_constraint_key(c) for c in node["constraints"])
        return f"{node['operator']}({';'.join(inner)})"
    return (f"{node['left_operand']}|{node['operator']}|{_norm(node['right_operand'])}"
            f"|{node['value_type']}")


def _party(rule: dict[str, Any], ir: dict[str, Any], role: str) -> str | None:
    return name_key(rule.get(role) or ir.get(role))


def checkpoints(ir: dict[str, Any] | None) -> dict[str, set[tuple]]:
    """Entity / action / modality / constraint checkpoints of an IR dump."""
    out: dict[str, set[tuple]] = {c: set() for c in CATEGORIES}
    if not ir:
        return out
    for rule in ir.get("rules", []):
        mod, act = rule["modality"], rule["action"]
        tgt, who = name_key(rule.get("target")), _party(rule, ir, "assignee")
        if who:
            out["entity"].add(("assignee", who))
        if tgt:
            out["entity"].add(("target", tgt))
        out["action"].add((act, tgt))
        out["modality"].add((mod, act, tgt, who))
        for node in rule.get("constraints", []):
            out["constraint"].add((mod, act, tgt, _constraint_key(node)))
        for node in rule.get("action_refinements", []):
            out["constraint"].add((mod, act, tgt, "refine:" + _constraint_key(node)))
        for duty in rule.get("duties", []):
            out["constraint"].add((mod, act, tgt, "duty", duty["action"]))
            for node in duty.get("constraints", []):
                out["constraint"].add((mod, act, tgt, "duty", duty["action"], _constraint_key(node)))
    return out


def _f1(tp: int, fp: int, fn: int) -> float | None:
    if tp + fp + fn == 0:
        return None
    return round(2 * tp / (2 * tp + fp + fn), 4)


def checkpoint_scores(results: list[dict[str, Any]]) -> dict[str, float | None]:
    counts = {c: [0, 0, 0] for c in CATEGORIES}
    for res in results:
        gold = checkpoints(res["case"]["gold_ir"])
        pred = checkpoints(res["draft"].get("ir"))
        for cat in CATEGORIES:
            counts[cat][0] += len(gold[cat] & pred[cat])
            counts[cat][1] += len(pred[cat] - gold[cat])
            counts[cat][2] += len(gold[cat] - pred[cat])
    total = [sum(c[i] for c in counts.values()) for i in range(3)]
    scores: dict[str, float | None] = {"checkpoint_f1": _f1(*total)}
    for cat in CATEGORIES:
        scores[f"{cat}_f1"] = _f1(*counts[cat])
    return scores


def _perm_keys(ir: dict[str, Any] | None, modality: str) -> list[tuple[str, str | None, str | None]]:
    if not ir:
        return []
    return [(r["action"], name_key(r.get("target")), _party(r, ir, "assignee"))
            for r in ir.get("rules", []) if r["modality"] == modality]


def false_permissions(result: dict[str, Any]) -> list[tuple]:
    """Predicted permissions that the gold policy does not grant."""
    gold = _perm_keys(result["case"]["gold_ir"], "permission")
    forbidden = {normalize_name(p) for p in result["case"]["expect"].get("forbidden_principals", [])}
    bad = []
    for action, target, assignee in _perm_keys(result["draft"].get("ir"), "permission"):
        covered = any(g_t == target and g_s == assignee and includes(g_a, action)
                      for g_a, g_t, g_s in gold)
        listed = assignee is not None and normalize_name(assignee) in forbidden
        if listed or not covered:
            bad.append((action, target, assignee))
    return bad


def missed_prohibitions(result: dict[str, Any]) -> tuple[int, int]:
    gold = _perm_keys(result["case"]["gold_ir"], "prohibition")
    pred = _perm_keys(result["draft"].get("ir"), "prohibition")
    missed = 0
    for g_a, g_t, g_s in gold:
        if not any(includes(p_a, g_a) and p_t == g_t and p_s in (g_s, None)
                   for p_a, p_t, p_s in pred):
            missed += 1
    return missed, len(gold)


def attribution(draft: dict[str, Any], source: str) -> tuple[int, int]:
    """(rules whose span resolves to its quote in the source, rules)."""
    rules = (draft.get("ir") or {}).get("rules", [])
    ok = 0
    for rule in rules:
        for span in rule.get("source_spans", []):
            start, end = span.get("start"), span.get("end")
            if start is not None and end is not None and _norm(source[start:end]) == _norm(
                    span["quote"]):
                ok += 1
                break
    return ok, len(rules)


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * q))], 3)


def compute(results: list[dict[str, Any]]) -> dict[str, Any]:
    """All Phase 4.2 metrics over ``results``. ``None`` means not applicable."""
    m: dict[str, Any] = {"cases": len(results)}
    m.update(checkpoint_scores(results))

    with_rules = [r for r in results if (r["draft"].get("ir") or {}).get("rules")]
    m["shacl_pass_rate"] = _ratio(
        sum(1 for r in with_rules if r["draft"].get("valid")), len(with_rules))
    m["valid_term_rate"] = _ratio(
        sum(1 for r in results if r["draft"].get("ir") is not None), len(results))

    fp_cases = [r for r in results if false_permissions(r)]
    m["false_permission_rate"] = _ratio(len(fp_cases), len(results))
    m["false_auto_publish_rate"] = _ratio(
        sum(1 for r in fp_cases if r["draft"].get("auto_publish")), len(results))
    adversarial = [r for r in results if r["case"]["tier"] == 5]
    m["tier5_false_permission_rate"] = _ratio(
        sum(1 for r in adversarial if false_permissions(r)), len(adversarial))
    missed = total = 0
    for r in results:
        a, b = missed_prohibitions(r)
        missed, total = missed + a, total + b
    m["missed_prohibition_rate"] = _ratio(missed, total)

    agree = reqs = 0
    for r in results:
        if r["case"]["expect"].get("conflict") or not r["case"]["gold_ir"]["rules"]:
            continue
        a, b = agreement(r["case"]["gold_ir"], r["draft"].get("ir"))
        agree, reqs = agree + a, reqs + b
    m["behavioral_agreement"] = _ratio(agree, reqs)

    conflicts = [r for r in results if r["case"]["expect"].get("conflict")]
    m["conflict_escape_rate"] = _ratio(
        sum(1 for r in conflicts if r["draft"].get("auto_publish")), len(conflicts))

    clarify = [r for r in results if r["case"]["expect"].get("kind") == "clarify"]
    caught = 0
    for r in clarify:
        kinds = {a["kind"] for a in r["draft"].get("ambiguities", [])}
        if kinds & set(r["case"]["expect"]["ambiguity_kinds"]) and not r["draft"].get(
                "auto_publish"):
            caught += 1
    m["ambiguity_recall"] = _ratio(caught, len(clarify))

    m["unsupported_assumption_rate"] = _ratio(
        sum(1 for r in results if r["draft"].get("assumptions")
            and not r["case"]["gold_ir"].get("assumptions")), len(results))

    ok = rules = 0
    for r in results:
        a, b = attribution(r["draft"], r["case"]["nl"].strip())
        ok, rules = ok + a, rules + b
    m["span_attribution"] = _ratio(ok, rules)

    m["median_llm_calls"] = statistics.median(r["calls"] for r in results) if results else None
    for route in ("SIMPLE", "COMPLEX"):
        walls = [r["wall_s"] for r in results if r["draft"].get("route") == route]
        m[f"p95_{route.lower()}_s"] = _percentile(walls, 0.95)
    m["errors"] = sum(1 for r in results if r.get("error"))
    m["auto_publish_rate"] = _ratio(
        sum(1 for r in results if r["draft"].get("auto_publish")), len(results))
    return m


def by_tier(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        groups[r["case"]["tier"]].append(r)
    keep = ("cases", "checkpoint_f1", "constraint_f1", "false_permission_rate",
            "missed_prohibition_rate", "behavioral_agreement", "ambiguity_recall",
            "auto_publish_rate")
    out = {}
    for tier in sorted(groups):
        full = compute(groups[tier])
        out[str(tier)] = {k: full[k] for k in keep}
    return out
