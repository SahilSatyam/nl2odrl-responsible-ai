"""Focused tests for the NL->ODRL workflow optimizations (all offline).

Covers: $from arg references (unit + loop integration), brief conversion
guidance + repair hint, validate_odrl memoization, prompt-pack loading,
and `run --instruction-pack` adoption. Transport retry/cap tests live in
test_adapter_live.py next to the other LlamaAdapter transport tests.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest
from agentcrash.cli import load_yaml
from agentcrash.clock import VirtualClock
from agentcrash.ledger import Ledger
from test_run import NAIVE_ACTIONS, scripted_transport

from odrl_agent.adapter import (
    ModelNotConfiguredError,
    build_agent_brief,
    resolve_arg_refs,
    run_react_loop,
)
from odrl_agent.cli import main as cli_main
from odrl_agent.dspy_opt import (
    BASELINE_INSTRUCTION,
    TRAINSET_ROWS,
    PackRejectedError,
    load_pack,
    trainset_hash,
)
from odrl_agent.tools import ToolRegistry

REPO = Path(__file__).resolve().parent.parent
PACK = REPO / "prompts" / "pack-probe-v1.json"

TOOLS = ["parse_policy", "validate_odrl", "save_odrl"]
SCENARIO = {"task": "Convert the request", "environment": {}}
NAIVE_POLICY = {"rules": []}
STRICT_POLICY = {
    "rules": [
        {"rule": "on_untrusted_content", "action": "require_confirmation"},
        {"rule": "on_stale_read", "action": "check_then_act"},
        {"rule": "on_tool_timeout", "action": "check_then_act"},
    ]
}


def test_refs_passthrough_without_markers() -> None:
    args = {"op": "write", "record": "r", "value": {"a": [1, 2]}}
    assert resolve_arg_refs(args, {}) == args


def test_refs_latest_result_by_path() -> None:
    memory = {"parse_policy": {"odrl": {"type": "Offer"}, "bytes": 10}}
    args = {
        "op": "write",
        "record": "r",
        "value": {"$from": {"tool": "parse_policy", "path": "odrl"}},
    }
    assert resolve_arg_refs(args, memory) == {
        "op": "write",
        "record": "r",
        "value": {"type": "Offer"},
    }


def test_refs_dotted_and_index_paths() -> None:
    memory = {"t": {"a": {"b": [{"c": 7}]}}}
    assert resolve_arg_refs({"$from": {"tool": "t", "path": "a.b.0.c"}}, memory) == 7
    assert resolve_arg_refs({"$from": {"tool": "t"}}, memory) == {"a": {"b": [{"c": 7}]}}


def test_refs_unresolvable_fail_closed() -> None:
    with pytest.raises(ModelNotConfiguredError):
        resolve_arg_refs({"$from": {"tool": "nope", "path": "odrl"}}, {})
    with pytest.raises(ModelNotConfiguredError):
        resolve_arg_refs({"$from": {"tool": "t", "path": "missing"}}, {"t": {}})
    with pytest.raises(ModelNotConfiguredError):
        resolve_arg_refs({"$from": {"tool": "t", "path": "a"}}, {"t": [1]})
    with pytest.raises(ModelNotConfiguredError):
        resolve_arg_refs({"$from": "parse_policy"}, {"parse_policy": {}})
    with pytest.raises(ModelNotConfiguredError):
        resolve_arg_refs({"$from": {"tool": "t", "path": "a"}, "extra": 1}, {"t": {"a": 1}})


def test_brief_carries_baseline_guidance_by_default() -> None:
    brief = build_agent_brief(SCENARIO, NAIVE_POLICY, "policy: {}", TOOLS)
    assert "Convert a natural-language access request into an ODRL 2.2 policy" in brief
    assert "$from" in brief


def test_brief_instruction_override_replaces_baseline() -> None:
    brief = build_agent_brief(
        SCENARIO, NAIVE_POLICY, "policy: {}", TOOLS, convert_instruction="PACK SAYS HI"
    )
    assert "PACK SAYS HI" in brief
    assert "Convert a natural-language access request into an ODRL 2.2 policy" not in brief


def test_brief_repair_hint_strict_only() -> None:
    strict = build_agent_brief(SCENARIO, STRICT_POLICY, "policy: {}", TOOLS)
    assert "only the listed errors fixed" in strict
    naive = build_agent_brief(SCENARIO, NAIVE_POLICY, "policy: {}", TOOLS)
    assert "only the listed errors fixed" not in naive
    assert BASELINE_INSTRUCTION.strip().splitlines()[0] in naive


def _loop_transport(monkeypatch, actions) -> None:
    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

    state = {"i": 0}

    def _fake_urlopen(request, timeout=None):
        idx = state["i"]
        action = actions[idx] if idx < len(actions) else {"type": "done", "reason": "end"}
        state["i"] += 1
        return _FakeResponse({"choices": [{"message": {"content": json.dumps(action)}}]})

    monkeypatch.setenv("LLAMA_API_KEY", "test-key")
    monkeypatch.setenv("LLAMA_BASE_URL", "https://llama.example/v1")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)


def test_loop_resolves_from_refs_end_to_end(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(REPO)
    scenario, scenario_text = load_yaml(REPO / "scenarios" / "odrl_convert" / "scenario.yaml")
    policy, policy_text = load_yaml(REPO / "policies" / "default.yaml")
    _loop_transport(
        monkeypatch,
        [
            {"type": "call", "tool": "parse_policy", "args": {"doc_id": "access-request-1"}},
            {
                "type": "call",
                "tool": "save_odrl",
                "args": {
                    "op": "write",
                    "record": "odrl-policy-1",
                    "value": {"$from": {"tool": "parse_policy", "path": "odrl"}},
                },
            },
            {"type": "done", "reason": "saved via ref"},
        ],
    )
    trace = tmp_path / "r.jsonl"
    state = tmp_path / "r.json"
    summary = run_react_loop(
        scenario=scenario,
        policy=policy,
        seed=42,
        trace_path=str(trace),
        state_path=str(state),
        scenario_text=scenario_text,
        policy_text=policy_text,
        max_steps=12,
    )
    assert summary["status"] == "completed"
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    parse_data = next(
        e["detail"]["data"]
        for e in events
        if e.get("detail", {}).get("phase") == "tool_result" and e.get("tool") == "parse_policy"
    )
    save_call = next(
        e
        for e in events
        if e.get("detail", {}).get("phase") == "tool_call"
        and e.get("tool") == "save_odrl"
        and e.get("args", {}).get("op") == "write"
    )
    assert save_call["args"]["value"] == parse_data["odrl"]
    assert "$from" not in json.dumps(save_call["args"])
    assert save_call["detail"].get("refs_resolved") is True


def test_validate_cache_memoizes_and_isolates() -> None:
    specs = [{"name": "validate_odrl", "latency_ms": 50, "timeout_ms": 1000, "mutating": False}]
    clock = VirtualClock()
    registry = ToolRegistry(specs, Ledger(), None, None, None)
    doc = {
        "@context": "http://www.w3.org/ns/odrl.jsonld",
        "type": "Offer",
        "uid": "urn:odrl:policy:cache-1",
        "permission": [{"action": "use", "target": "urn:data:d", "assignee": "team"}],
    }
    first = registry.call("validate_odrl", {"odrl": doc}, clock)
    assert first["outcome"] == "success" and first["data"]["valid"] is True
    assert len(registry._validate_cache) == 1
    first["data"]["errors"].append("poison")
    second = registry.call("validate_odrl", {"odrl": doc}, clock)
    assert second["data"]["errors"] == []
    assert clock.now() == 100


def test_load_pack_shipped_probe() -> None:
    pack = load_pack(PACK)
    assert pack["instruction"].strip()
    assert isinstance(pack["trainset_match"], bool)


def test_load_pack_rejects_bad_files(tmp_path) -> None:
    bad_version = tmp_path / "bad.json"
    bad_version.write_text(json.dumps({"pack_version": 999, "instruction": "x"}))
    with pytest.raises(PackRejectedError):
        load_pack(bad_version)
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"pack_version": 1, "instruction": "  "}))
    with pytest.raises(PackRejectedError):
        load_pack(empty)
    with pytest.raises(PackRejectedError):
        load_pack(tmp_path / "missing.json")


def test_load_pack_hash_match_reported(tmp_path) -> None:
    good = {
        "pack_version": 1,
        "instruction": "Do the conversion well.",
        "trainset_hash": trainset_hash(TRAINSET_ROWS),
    }
    path = tmp_path / "good.json"
    path.write_text(json.dumps(good))
    assert load_pack(path)["trainset_match"] is True


def test_cli_run_accepts_instruction_pack(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(REPO)
    scripted_transport(monkeypatch, NAIVE_ACTIONS)
    trace = tmp_path / "p.jsonl"
    state = tmp_path / "p.json"
    code = cli_main(
        [
            "run",
            "--scenario",
            str(REPO / "scenarios" / "odrl_convert_timeout" / "scenario.yaml"),
            "--seed",
            "42",
            "--trace-out",
            str(trace),
            "--state-out",
            str(state),
            "--instruction-pack",
            str(PACK),
        ]
    )
    assert code == 0
    assert trace.exists()


def test_cli_run_rejects_bad_pack(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(REPO)
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    code = cli_main(
        [
            "run",
            "--scenario",
            str(REPO / "scenarios" / "odrl_convert_timeout" / "scenario.yaml"),
            "--seed",
            "42",
            "--trace-out",
            str(tmp_path / "p.jsonl"),
            "--state-out",
            str(tmp_path / "p.json"),
            "--instruction-pack",
            str(bad),
        ]
    )
    assert code == 2
