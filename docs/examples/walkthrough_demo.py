"""Reproduce the running example in docs/ODRL_PIPELINE_WALKTHROUGH.md.

The model replies are scripted (no network, no API key): they are what a good
extractor would return for the example policy. Everything after the model call
(grounding, compilation, SHACL, Z3 reasoning, the repair loop and the draft
status) is the repo's real code, run through ``odrl_agent.pipeline.convert``.

Run from the repository root:

    PYTHONPATH=.:tests python docs/examples/walkthrough_demo.py      (Windows: ";" instead of ":")
"""
from __future__ import annotations

import copy
import json

from odrl_agent import pipeline
from odrl_agent.extractor import Extractor, ModelRoute, classify
from scripted_llm import scripted_llm  # tests/scripted_llm.py

TEXT = (
    "Acme Data Corp permits University Research Labs to read and analyze the 2026 "
    "Healthcare Analytics Dataset strictly within the European Union until December 31, "
    "2026, provided they attribute Acme Data Corp in any published results and delete all "
    "local copies after expiration. Redistributing the dataset or using it for commercial "
    "purposes is strictly prohibited."
)
SENTENCE_1, SENTENCE_2 = TEXT.split(" Redistributing")[0], "Redistributing" + TEXT.split(" Redistributing")[1]

ACME, LABS, DATASET = "Acme Data Corp", "University Research Labs", "2026 Healthcare Analytics Dataset"


def atomic(operand: str, op: str, value: str, value_type: str = "string") -> dict:
    return {"kind": "atomic", "left_operand": operand, "operator": op,
            "right_operand": value, "value_type": value_type}  # fmt: skip


EU = atomic("spatial", "eq", "European Union")
UNTIL = atomic("dateTime", "lteq", "2026-12-31", "date")
AFTER_EXPIRY = atomic("dateTime", "gt", "2026-12-31", "date")
COMMERCIAL = atomic("purpose", "eq", "commercial")
DUTIES = [
    {"action": "attribute", "target": DATASET},
    {"action": "delete", "target": DATASET, "constraints": [AFTER_EXPIRY]},
]


def permission(action: str) -> dict:
    return {"modality": "permission", "action": action, "target": DATASET,
            "constraints": [EU, UNTIL], "duties": DUTIES,
            "source_spans": [{"quote": SENTENCE_1}]}  # fmt: skip


def prohibition(action: str, constraints: list | None = None) -> dict:
    rule = {"modality": "prohibition", "action": action, "target": DATASET,
            "source_spans": [{"quote": SENTENCE_2}]}  # fmt: skip
    if constraints:
        rule["constraints"] = constraints
    return rule


# What the extractor returns first: no conflict strategy, as its prompt instructs.
FIRST_IR = {
    "analysis": "Acme grants read and derive access with EU and date limits, two duties and two denials.",
    "policy_type": "Agreement",
    "assigner": ACME,
    "assignee": LABS,
    "rules": [permission("read"), permission("derive"), prohibition("distribute"),
              prohibition("use", [COMMERCIAL])],  # fmt: skip
    "ambiguities": [{
        "kind": "other",
        "description": "attribution of Acme Data Corp in published results has no ODRL "
                       "left operand; only the attribute action is recorded",
    }],
    "assumptions": [{"field": "rules.1.action", "assumed_value": "derive",
                     "reason": "ODRL 2.2 has no 'analyze' action"}],  # fmt: skip
}
# What a repair call returns after the Z3 check reports the use/read clash.
REPAIRED_IR = {**copy.deepcopy(FIRST_IR), "conflict_strategy": "prohibit"}


class PrintingRun:
    """Minimal stand-in for the trace writer: prints each pipeline stage."""

    def stage(self, name: str, **data) -> None:
        print(f"[stage] {name:<10} {json.dumps(data)}")

    def model_call(self, *args, **kwargs) -> None:
        pass

    def fault(self, boundary: str):
        return None


def main() -> None:
    print("classify() ->", classify(TEXT), "\n")
    replies = [{"clauses": [SENTENCE_1, SENTENCE_2]}, FIRST_IR, REPAIRED_IR, REPAIRED_IR]
    extractor = Extractor(scripted_llm(replies), ModelRoute())
    draft, doc = pipeline.convert(
        TEXT, extractor, PrintingRun(), None, pipeline.Grounder(trusted_parties=[])
    )
    print(f"\nstatus={draft.status} auto_publish={draft.auto_publish} repairs={draft.repairs} "
          f"valid={draft.valid} model_calls={extractor.llm.usage.calls}")  # fmt: skip
    print("alerts:", *draft.alerts, sep="\n  ")
    print("\n" + json.dumps(draft.odrl, indent=2))


if __name__ == "__main__":
    main()
