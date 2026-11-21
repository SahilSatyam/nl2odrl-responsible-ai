"""Run tests: FR-01/02/03 oracles, seed matrix, refusal, wiring.

``odrl-host run`` drives the fixed-stage pipeline (``odrl_agent.pipeline``).
The model is scripted per test with structured IR replies (no network, no
key); real-model behaviour stays covered by the recorded fixtures plus the
key-gated live tests elsewhere.
"""
import json
import shutil
from pathlib import Path

import jsonschema
import pytest
from agentcrash.clock import VirtualClock
from agentcrash.ledger import Ledger
from agentcrash.tracer import compute_run_id, verify_chain
from scripted_llm import BASIC_IR, install

from odrl_agent.cli import main
from odrl_agent.tools import ToolRegistry

REPO = Path(__file__).resolve().parent.parent
SCENARIO_FILE = REPO / "scenarios" / "odrl_convert" / "scenario.yaml"
POLICY_FILE = REPO / "policies" / "default.yaml"
SEED_MATRIX = [1, 7, 42, 123, 2026]
SLOPPY_ODRL = {
    "@context": "http://www.w3.org/ns/odrl.jsonld",
    "type": "Offer",
    "uid": "urn:odrl:policy:dataset-7",
    "permission": [{"action": "use", "assignee": "research-team"}],
}


def run_cli(trace_path: Path, state_path: Path, *extra: str, seed: int = 42) -> int:
    """Run the reference scenario through cli.main; return the exit code."""
    return main(
        [
            "run",
            "--scenario",
            str(SCENARIO_FILE),
            "--seed",
            str(seed),
            "--trace-out",
            str(trace_path),
            "--state-out",
            str(state_path),
            *extra,
        ]
    )


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def normalize(path: Path) -> list[str]:
    """Normalize run-specific ids/hashes so reruns can be byte-compared."""
    normalized = []
    for i, event in enumerate(read_events(path), start=1):
        event = dict(event)
        event["run_id"] = "R"
        event["event_id"] = f"evt_R_{i:04d}"
        event["prev_hash"] = "H"
        normalized.append(json.dumps(event, sort_keys=True))
    return normalized


def validate_against(path: Path, schema_name: str, payload: dict) -> None:
    schema = json.loads((REPO / "schemas" / schema_name).read_text(encoding="utf-8"))
    jsonschema.validate(payload, schema)


def test_fr01_run_emits_schema_valid_trace_and_state(tmp_path, monkeypatch, capsys) -> None:
    install(monkeypatch, [BASIC_IR])
    trace, state = tmp_path / "t.jsonl", tmp_path / "s.json"
    assert run_cli(trace, state, seed=42) == 0
    events = read_events(trace)
    assert len(events) > 0
    for event in events:
        validate_against(trace, "trace-event.json", event)
    snapshot = json.loads(state.read_text(encoding="utf-8"))
    validate_against(state, "env-state.json", snapshot)
    assert verify_chain(str(trace))["ok"] is True
    ts = [e["ts_virtual"] for e in events]
    assert ts == sorted(ts) and ts[0] == 0
    assert Ledger.ledger_hash(snapshot) == snapshot["ledger_hash"]
    out = capsys.readouterr().out
    assert f"run_id={snapshot['run_id']}" in out and "saves=1" in out
    # The NL source is rendered inside untrusted delimiters (doc 11 S3).
    assert '<untrusted_content source="tests/fixtures/odrl_nl.yaml#sloppy_offer_v1">' in out


def test_run_is_one_model_call_and_fixed_stages(tmp_path, monkeypatch) -> None:
    install(monkeypatch, [BASIC_IR])
    trace = tmp_path / "t.jsonl"
    assert run_cli(trace, tmp_path / "s.json") == 0
    events = read_events(trace)
    stages = [e["detail"]["stage"] for e in events if e["detail"].get("phase") == "pipeline_stage"]
    assert stages == ["ingest", "decompose", "extract", "ground", "compile", "verify", "draft"]
    calls = [e for e in events if e["detail"].get("phase") == "model_call"]
    assert [c["detail"]["purpose"] for c in calls] == ["extract"]
    tools = [(e["tool"], (e["args"] or {}).get("op")) for e in events
             if e["detail"].get("phase") == "tool_result"]
    assert tools == [("validate_odrl", None), ("save_odrl", "read"), ("save_odrl", "write")]
    end = events[-1]["detail"]
    assert end["phase"] == "run_end" and end["draft_status"] == "ready"


def test_fr02_same_seed_is_byte_identical(tmp_path, monkeypatch) -> None:
    install(monkeypatch, [BASIC_IR])
    first = run_cli(tmp_path / "a.jsonl", tmp_path / "a.json", seed=42)
    second = run_cli(tmp_path / "b.jsonl", tmp_path / "b.json", seed=42)
    assert (first, second) == (0, 0)
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()


@pytest.mark.parametrize("seed", SEED_MATRIX)
def test_fr03_seed_matrix_reruns_match_after_normalization(tmp_path, monkeypatch, seed) -> None:
    install(monkeypatch, [BASIC_IR])
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    assert run_cli(first, tmp_path / "a.json", seed=seed) == 0
    assert run_cli(second, tmp_path / "b.json", seed=seed) == 0
    assert normalize(first) == normalize(second)


def test_fault_free_run_records_no_injector_phases(tmp_path, monkeypatch) -> None:
    install(monkeypatch, [BASIC_IR])
    trace = tmp_path / "t.jsonl"
    assert run_cli(trace, tmp_path / "s.json", seed=42) == 0
    events = read_events(trace)
    assert events, "expected a non-empty trace"
    assert all(e.get("injector_event") is None for e in events)
    assert all(e.get("outcome") != "timeout" for e in events)
    assert verify_chain(str(trace))["ok"] is True


def _link_or_copy(link: Path, target: Path) -> None:
    """Symlink a repo dir into the tmp cwd, copying when Windows blocks symlinks."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:  # WinError 1314 without symlink privilege / Developer Mode
        shutil.copytree(target, link)


def test_run_id_formula_and_default_trace_path(tmp_path, monkeypatch) -> None:
    install(monkeypatch, [BASIC_IR])
    monkeypatch.chdir(tmp_path)
    _link_or_copy(Path("scenarios"), REPO / "scenarios")
    _link_or_copy(Path("policies"), REPO / "policies")
    _link_or_copy(Path("tests"), REPO / "tests")
    scenario_text = SCENARIO_FILE.read_text(encoding="utf-8")
    policy_text = POLICY_FILE.read_text(encoding="utf-8")
    assert main(["run", "--scenario", "odrl_convert", "--seed", "42"]) == 0
    expected = compute_run_id(scenario_text, policy_text, 42)
    assert expected == "d15e139bd5e3e42e"
    assert (tmp_path / "traces" / f"{expected}.jsonl").is_file()
    assert (tmp_path / "reports" / f"{expected}.state.json").is_file()


def test_unknown_tool_and_bad_args_rejected_without_effect() -> None:
    ledger, clock = Ledger(), VirtualClock()
    nl_store = {
        "access-request-1": {
            "text": "Allow the research team to use dataset-7.",
            "label": "benign",
            "fixture": "tests/fixtures/odrl_nl.yaml#basic_offer_v1",
            "naive_odrl": dict(SLOPPY_ODRL),
            "expected_odrl": dict(SLOPPY_ODRL),
        }
    }
    registry = ToolRegistry.from_scenario(
        {
            "tools": [
                {"name": "parse_policy", "latency_ms": 80, "timeout_ms": 1000},
                {"name": "save_odrl", "latency_ms": 60, "timeout_ms": 1000},
            ]
        },
        ledger,
        None,
        {},
        nl_store,
    )
    assert registry.call("nope", {}, clock)["outcome"] == "error"
    assert registry.call("parse_policy", {}, clock)["outcome"] == "error"
    assert (
        registry.call("parse_policy", {"doc_id": "access-request-1", "bogus": 1}, clock)["outcome"]
        == "error"
    )
    assert registry.call("parse_policy", {"doc_id": "nope"}, clock)["outcome"] == "error"
    assert (
        registry.call("save_odrl", {"op": "read", "record": "missing"}, clock)["outcome"] == "error"
    )
    ledger.kv_seed("odrl-policy-1", {})
    good = registry.call("parse_policy", {"doc_id": "access-request-1"}, clock)
    assert good["outcome"] == "success" and clock.now() == 80
    write = registry.call(
        "save_odrl",
        {"op": "write", "record": "odrl-policy-1", "value": dict(SLOPPY_ODRL)},
        clock,
    )
    assert write["outcome"] == "success" and clock.now() == 140


def test_unrepairable_output_saves_nothing_and_exits_5(tmp_path, monkeypatch) -> None:
    """Three off-schema replies (extract + 2 repairs) end the run without a save."""
    bad = {"rules": [{"modality": "permission", "action": "teleport"}]}
    install(monkeypatch, [bad, bad, bad])
    trace = tmp_path / "t.jsonl"
    assert run_cli(trace, tmp_path / "s.json") == 5
    events = read_events(trace)
    end = events[-1]["detail"]
    assert end["phase"] == "run_end" and end["status"] == "failed" and end["saves"] == 0
    assert end["model_calls"] == 3
    assert not any(e["tool"] == "save_odrl" for e in events)


def test_model_api_failure_exits_5(tmp_path, monkeypatch, capsys) -> None:
    install(monkeypatch, [401])
    assert run_cli(tmp_path / "t.jsonl", tmp_path / "s.json") == 5
    assert "E_MODEL_API" in capsys.readouterr().err


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    install(monkeypatch, [BASIC_IR])
    trace = tmp_path / "t.jsonl"
    code = run_cli(trace, tmp_path / "s.json", "--dry-run")
    assert code == 0
    assert not trace.exists()
    out = capsys.readouterr().out
    assert "odrl_convert" in out and "nothing written" in out
