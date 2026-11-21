"""Replay-command tests: fix verification, regression, fail-closed.

Exit contract: 0 fix VERIFIED, 1 VERDICT_FAIL, 2 usage, 4 rejected
patch (pre-run, no trace written). Re-runs use scripted transport
(no network, no key); the scripts replay feasible live behaviors,
including the reconcile-first fix the live model discovered.
"""

import json
from pathlib import Path

import pytest
from scripted_llm import BASIC_IR, install

from odrl_agent.cli import main
from odrl_agent.remediation import reference_patch_for

REPO = Path(__file__).resolve().parent.parent
SCENARIO_FILE = REPO / "scenarios" / "odrl_convert" / "scenario.yaml"
FIXED_POLICY = REPO / "policies" / "fixed-odrl-convert.yaml"
TRACE_DIR = REPO / "tests" / "fixtures" / "traces"
FAILING_TRACE = TRACE_DIR / "odrl_convert-naive-seed42.jsonl"
FAILING_STATE = TRACE_DIR / "odrl_convert-naive-seed42.state.json"

def replay(trace, output_args, *extra, state=None):
    """Run replay against the reference scenario; return the exit code."""
    return main(
        [
            "replay",
            "--trace",
            str(trace),
            "--scenario",
            str(SCENARIO_FILE),
            *(["--state", str(state)] if state else []),
            *output_args,
            *extra,
        ]
    )


def write_patch(tmp_path, mutate=None):
    """Diagnose-equivalent patch file (offline reference patch)."""
    patch = reference_patch_for("odrl_convert", "d15e139bd5e3e42e")
    if mutate:
        mutate(patch)
    path = tmp_path / "patch.json"
    path.write_text(json.dumps(patch, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def test_replay_patch_verifies_because_pipeline_validates(tmp_path, monkeypatch, capsys) -> None:
    install(monkeypatch, [BASIC_IR])
    monkeypatch.chdir(REPO)
    patch = write_patch(tmp_path)
    report_out = tmp_path / "replay.report.json"
    code = replay(
        FAILING_TRACE,
        ["--patch", str(patch), "--trace-out", str(tmp_path / "replay.jsonl")],
        "--state",
        str(FAILING_STATE),
        "--state-out",
        str(tmp_path / "replay.state.json"),
        "--report-out",
        str(report_out),
        "--no-regression",
        state=FAILING_STATE,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "fix: VERIFIED" in out
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["verdict"] == "PASS"
    assert sorted(report["fixed"]) == ["fresh_write_only", "odrl_valid"]
    assert report["broken"] == []
    assert report["api_version"] == "v1"


def test_replay_fixed_policy_verifies(tmp_path, monkeypatch, capsys) -> None:
    install(monkeypatch, [BASIC_IR])
    monkeypatch.chdir(REPO)
    code = replay(
        FAILING_TRACE,
        ["--policy", str(FIXED_POLICY), "--no-regression"],
        "--trace-out",
        str(tmp_path / "r.jsonl"),
        "--state-out",
        str(tmp_path / "r.json"),
        "--report-out",
        str(tmp_path / "r.report.json"),
        state=FAILING_STATE,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "fix: VERIFIED" in out and "regression: skipped" in out
    report = json.loads((tmp_path / "r.report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "PASS"
    assert sorted(report["fixed"]) == [
        "fresh_write_only",
        "odrl_valid",
    ]


def test_replay_time_travel_patch_fails_closed_with_no_trace(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO)
    patch = write_patch(
        tmp_path,
        mutate=lambda p: p["patch"]["rules"].__setitem__(
            0,
            {
                "rule": "on_tool_timeout",
                "tool": "save_odrl",
                "action": "time_travel",
                "retry": {"max_attempts": 1, "require_idempotency_key": False},
            },
        ),
    )
    trace_out = tmp_path / "evil.jsonl"
    code = replay(
        FAILING_TRACE,
        ["--patch", str(patch), "--trace-out", str(trace_out)],
        "--state-out",
        str(tmp_path / "evil.json"),
        "--report-out",
        str(tmp_path / "evil.report.json"),
        state=FAILING_STATE,
    )
    assert code == 4
    assert not trace_out.exists()
    assert "E_POLICY_REJECTED" in capsys.readouterr().err


def test_replay_malformed_patch_rejected_pre_run(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"patch_version": 1}), encoding="utf-8")
    trace_out = tmp_path / "bad.jsonl"
    code = replay(
        FAILING_TRACE,
        ["--patch", str(bad), "--trace-out", str(trace_out)],
        "--state-out",
        str(tmp_path / "bad.json"),
        state=FAILING_STATE,
    )
    assert code == 4
    assert not trace_out.exists()
    assert "E_POLICY_REJECTED" in capsys.readouterr().err


def test_replay_requires_exactly_one_of_policy_or_patch(tmp_path, capsys) -> None:
    patch = write_patch(tmp_path)
    assert (
        replay(
            FAILING_TRACE,
            ["--policy", str(FIXED_POLICY), "--patch", str(patch)],
            "--trace-out",
            str(tmp_path / "x.jsonl"),
            state=FAILING_STATE,
        )
        == 2
    )
    assert (
        replay(FAILING_TRACE, ["--trace-out", str(tmp_path / "y.jsonl")], state=FAILING_STATE) == 2
    )
    assert "E_CONFIG" in capsys.readouterr().err


def test_replay_live_model_end_to_end(tmp_path, monkeypatch, capsys) -> None:
    """Full replay against the real model; needs LLAMA_API_KEY (skipped in CI)."""
    import os

    if not os.environ.get("LLAMA_API_KEY", "").strip():
        pytest.skip("live replay needs LLAMA_API_KEY")
    monkeypatch.chdir(REPO)
    code = replay(
        FAILING_TRACE,
        ["--policy", str(FIXED_POLICY), "--no-regression"],
        "--trace-out",
        str(tmp_path / "live.jsonl"),
        "--state-out",
        str(tmp_path / "live.json"),
        "--report-out",
        str(tmp_path / "live.report.json"),
        state=FAILING_STATE,
    )
    assert code in (0, 1)
    report = json.loads((tmp_path / "live.report.json").read_text(encoding="utf-8"))
    assert report["api_version"] == "v1"
    assert "fixed" in report and "broken" in report
