"""Check-command tests: recorded fixtures, JSON determinism, exit codes.

Exit contract: 0 PASS, 1 VERDICT_FAIL, 2 usage/config, 3 CORRUPT_TRACE.
Malformed traces are rejected (exit 3) and never PASS.

The check under test is the generic ``agentcrash check`` (trace +
explicit ``--invariant`` ids). ODRL-specific invariants come from the
host package, which registers them on import.
"""

import json
from pathlib import Path

import yaml
from agentcrash.cli import main

import odrl_agent.verifiers  # noqa: F401 -- registers odrl_valid et al.

REPO = Path(__file__).resolve().parent.parent
SCENARIO_FILE = REPO / "scenarios" / "odrl_convert" / "scenario.yaml"
TRACE_DIR = REPO / "tests" / "fixtures" / "traces"
NAIVE_TRACE = TRACE_DIR / "odrl_convert-naive-seed42.jsonl"
NAIVE_STATE = TRACE_DIR / "odrl_convert-naive-seed42.state.json"
FIXED_TRACE = TRACE_DIR / "odrl_convert-fixed-seed42.jsonl"
FIXED_STATE = TRACE_DIR / "odrl_convert-fixed-seed42.state.json"


def scenario_invariants() -> list[str]:
    """Invariant ids declared by the fixture scenario, in order."""
    scenario = yaml.safe_load(SCENARIO_FILE.read_text(encoding="utf-8"))
    return [item["id"] for item in scenario.get("invariants", [])]


def check(trace, state, report, *extra):
    """Run check with explicit invariants; return the exit code."""
    args = ["check", "--trace", str(trace), "--state", str(state)]
    for invariant_id in scenario_invariants():
        args += ["--invariant", invariant_id]
    args += ["--report-out", str(report), *extra]
    return main(args)


def test_naive_trace_fails_validity_and_freshness(tmp_path, capsys) -> None:
    code = check(NAIVE_TRACE, NAIVE_STATE, tmp_path / "v.json")
    assert code == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "odrl_valid" in out
    fail_line = next(line for line in out.splitlines() if "odrl_valid" in line)
    assert "target" in fail_line
    assert "[FAIL]" in out and "fresh_write_only" in out
    assert "[PASS]" in out and "exactly_once_save" in out
    assert "[PASS]" in out and "no_attacker_grant" in out
    assert "verdict=FAIL" in out


def test_naive_report_carries_required_fields(tmp_path) -> None:
    report = tmp_path / "v.json"
    assert check(NAIVE_TRACE, NAIVE_STATE, report) == 1
    verdict = json.loads(report.read_text(encoding="utf-8"))
    assert set(verdict) >= {"api_version", "run_id", "verdict", "results"}
    rows = {row["id"]: row for row in verdict["results"]}
    assert rows["odrl_valid"]["passed"] is False
    assert rows["fresh_write_only"]["passed"] is False
    assert rows["exactly_once_save"]["passed"] is True
    assert rows["no_attacker_grant"]["passed"] is True


def test_json_format_is_deterministic_and_pure(tmp_path, capsys) -> None:
    bodies = []
    for i in range(3):
        report = tmp_path / f"v{i}.json"
        code = check(NAIVE_TRACE, NAIVE_STATE, report, "--format", "json")
        assert code == 1
        captured = capsys.readouterr()
        bodies.append(captured.out)
        assert "[FAIL]" in captured.err and "no_attacker_grant" in captured.err
    assert bodies[0] == bodies[1] == bodies[2]
    payload = json.loads(bodies[0])
    assert payload["api_version"] == "v1"
    assert payload["verdict"] == "FAIL"


def test_fixed_live_trace_passes_with_exit_zero(tmp_path, capsys) -> None:
    report = tmp_path / "v.json"
    code = check(FIXED_TRACE, FIXED_STATE, report)
    assert code == 0
    out = capsys.readouterr().out
    assert out.count("[PASS]") == len(scenario_invariants()) and "verdict=PASS" in out


def test_malformed_trace_rejected_never_passes(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"event_id": "evt_x"}\nnot json\n', encoding="utf-8")
    report = tmp_path / "v.json"
    code = check(bad, NAIVE_STATE, report)
    assert code == 3
    assert not report.exists()
    captured = capsys.readouterr()
    assert "FAIL" not in captured.out and "PASS" not in captured.out


def test_missing_trace_is_usage_error(capsys) -> None:
    assert main(["check", "--trace", "nope.jsonl"]) == 2


def test_unknown_verifier_is_config_error(tmp_path) -> None:
    code = main(
        [
            "check",
            "--trace",
            str(NAIVE_TRACE),
            "--invariant",
            "exactly_once_save",
            "--invariant",
            "nope_not_real",
            "--state",
            str(NAIVE_STATE),
            "--report-out",
            str(tmp_path / "v.json"),
        ]
    )
    assert code == 2


def test_plugin_flag_registers_host_invariants(tmp_path) -> None:
    """--plugin imports a host module so its invariants resolve."""
    report = tmp_path / "v.json"
    code = main(
        [
            "check",
            "--trace",
            str(NAIVE_TRACE),
            "--state",
            str(NAIVE_STATE),
            "--plugin",
            "odrl_agent.verifiers",
            *[arg for iid in scenario_invariants() for arg in ("--invariant", iid)],
            "--report-out",
            str(report),
        ]
    )
    assert code == 1
    assert json.loads(report.read_text(encoding="utf-8"))["verdict"] == "FAIL"


def test_unknown_plugin_is_config_error(tmp_path, capsys) -> None:
    code = main(
        [
            "check",
            "--trace",
            str(NAIVE_TRACE),
            "--plugin",
            "no_such_module_xyz",
            "--invariant",
            "exactly_once_save",
            "--report-out",
            str(tmp_path / "v.json"),
        ]
    )
    assert code == 2
    assert "cannot import" in capsys.readouterr().err


def test_missing_invariants_is_usage_error(tmp_path, capsys) -> None:
    code = main(
        [
            "check",
            "--trace",
            str(NAIVE_TRACE),
            "--state",
            str(NAIVE_STATE),
            "--report-out",
            str(tmp_path / "v.json"),
        ]
    )
    assert code == 2
    assert "no invariants" in capsys.readouterr().err
