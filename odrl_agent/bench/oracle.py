"""Offline model for the per-PR benchmark run: answers with the gold IR.

The oracle never touches the network. It looks the case up by the quoted
source text in the request, so it exercises the real extractor, grounder,
compiler, SHACL validator, reasoner and publish gate, and scores the output
with the real metrics. A perfect oracle must pass every gate; the *mutants*
below must fail them, which proves the gates can actually go red.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

import httpx

from odrl_agent.extractor import DECOMPOSE_PROMPT, split_sentences
from odrl_agent.llm import StructuredLlm

MUTANTS = ("overpermit", "drop_prohibitions", "drop_constraints", "obey_injection")
_SOURCE = re.compile(r"<untrusted_source>\n(.*?)\n</untrusted_source>", re.DOTALL)
USAGE = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}


def mutate(case: dict[str, Any], mutant: str | None) -> dict[str, Any]:
    ir = copy.deepcopy(case["gold_ir"])
    if mutant is None:
        return ir
    rules = ir["rules"]
    if mutant == "overpermit":
        for rule in rules:
            if rule["modality"] == "permission":
                rule["action"] = "use"
    elif mutant == "drop_prohibitions":
        ir["rules"] = [r for r in rules if r["modality"] != "prohibition"]
    elif mutant == "drop_constraints":
        for rule in rules:
            rule["constraints"] = []
            rule["duties"] = []
    elif mutant == "obey_injection":
        injection = case["expect"].get("injection")
        if injection:
            rules.append({
                "modality": "permission", "action": "use", "assignee": injection["principal"],
                "target": rules[0]["target"], "source_spans": [{"quote": injection["quote"]}],
            })
    else:
        raise ValueError(f"unknown mutant {mutant!r}")
    return ir


def oracle_llm(cases: list[dict[str, Any]], mutant: str | None = None) -> StructuredLlm:
    """A ``StructuredLlm`` whose transport serves gold (optionally mutated) IRs."""
    by_text = {case["nl"].strip(): case for case in cases}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        messages = body["messages"]
        found = _SOURCE.search(messages[1]["content"])
        source = found.group(1) if found else ""
        if messages[0]["content"] == DECOMPOSE_PROMPT:
            payload: dict[str, Any] = {"clauses": split_sentences(source) or [source]}
        elif source in by_text:
            payload = mutate(by_text[source], mutant)
        else:
            return httpx.Response(500, text="oracle: unknown source")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}],
            "usage": USAGE,
        })

    return StructuredLlm(
        base_url="https://oracle.invalid/v1", api_key="oracle",
        transport=httpx.MockTransport(handler), sleep=lambda _s: None,
    )
