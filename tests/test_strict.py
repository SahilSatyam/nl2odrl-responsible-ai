"""strict.py is promoted: every conversion self-checks its trace envelope."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from agentcrash.cli import load_yaml
from agentcrash.verifiers import TraceCorruptError, load_state_snapshot, load_trace_events

from odrl_agent.chaos import mock_llm
from odrl_agent.instrument import chaos_sdk
from odrl_agent.pipeline import run_conversion
from odrl_agent.strict import strict_validate

REPO = Path(__file__).resolve().parent.parent


def _run(tmp_path):
    scenario, scenario_text = load_yaml(REPO / "scenarios" / "odrl_convert" / "scenario.yaml")
    policy, policy_text = load_yaml(REPO / "policies" / "fixed-odrl-convert.yaml")
    trace, state = tmp_path / "t.jsonl", tmp_path / "s.json"
    run_conversion(scenario=scenario, policy=policy, seed=42, trace_path=str(trace),
                   state_path=str(state), scenario_text=scenario_text, policy_text=policy_text,
                   llm=mock_llm(), sdk=chaos_sdk({}))  # fmt: skip
    kw = dict(scenario=scenario, scenario_text=scenario_text, policy_text=policy_text,
              policy=policy, state=load_state_snapshot(str(state)))  # fmt: skip
    return load_trace_events(str(trace)), kw


def test_conversion_trace_passes_strict_rules(tmp_path) -> None:
    events, kw = _run(tmp_path)
    assert strict_validate(events, **kw) == 42


def test_tampered_trace_fails_strict_rules(tmp_path) -> None:
    events, kw = _run(tmp_path)
    forged = json.loads(json.dumps(events))
    forged[2]["detail"]["phase"] = "forged"
    with pytest.raises(TraceCorruptError, match="prev_hash"):
        strict_validate(forged, **kw)
    with pytest.raises(TraceCorruptError, match="ledger_hash"):
        strict_validate(events, **{**kw, "state": {**kw["state"], "ledger_hash": "sha256:0"}})
