"""Convert API: validation, catalog, background run, verdict, patch (scripted)."""

from __future__ import annotations

import pytest

import odrl_agent.verifiers  # noqa: F401 -- import registers product verifiers
from odrl_agent.web import (
    ConvertError,
    RunRegistry,
    apply_model_settings,
    build_inline_run,
    model_status,
    validate_convert,
)


def test_validate_convert_rejects_bad_input() -> None:
    with pytest.raises(ConvertError):
        validate_convert({})
    with pytest.raises(ConvertError):
        validate_convert({"nl_text": "   "})
    with pytest.raises(ConvertError):
        validate_convert({"nl_text": "x" * 4001})
    with pytest.raises(ConvertError):
        validate_convert({"nl_text": "ok", "policy_variant": "maybe"})
    with pytest.raises(ConvertError):
        validate_convert({"nl_text": "ok", "seed": -1})
    with pytest.raises(ConvertError):
        validate_convert({"nl_text": "ok", "max_steps": 99})
    assert validate_convert({"nl_text": "  Allow X.  "})[0] == "Allow X."
    # unknown keys (e.g. a stale "faults" entry) are ignored, not rejected
    assert validate_convert({"nl_text": "ok", "faults": [{"kind": "x"}]})[0] == "ok"


def test_build_inline_run_composes_scenario_and_policy(tmp_path) -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    first = build_inline_run("Allow X.", "naive", 42, 12, root, 1, tmp_path)
    second = build_inline_run("Allow X.", "naive", 42, 12, root, 2, tmp_path)
    assert first["run_id"] != second["run_id"]
    assert first["patch_scenario_id"] == "odrl_convert"
    assert first["policy_file"] == "policies/default.yaml"
    assert first["faults"] == []
    assert first["scenario"]["environment"]["nl_docs"][0]["text"] == "Allow X."
    fixed = build_inline_run("Allow X.", "fixed", 42, 12, root, 1, tmp_path)
    assert fixed["policy_file"] == "policies/fixed-odrl-convert.yaml"


INLINE_TEXT = "Allow the research team to use dataset-7. Sharing it outside the team is forbidden."
INLINE_IR = {
    "analysis": "one grant, one prohibition",
    "policy_type": "Set",
    "rules": [
        {
            "modality": "permission",
            "action": "use",
            "target": "dataset-7",
            "assignee": "research-team",
            "source_spans": [{"quote": "Allow the research team to use dataset-7"}],
        },
        {
            "modality": "prohibition",
            "action": "distribute",
            "target": "dataset-7",
            "source_spans": [{"quote": "Sharing it outside the team is forbidden"}],
        },
    ],
}


def test_convert_flow_reports_verdict_and_draft(monkeypatch, tmp_path) -> None:
    from pathlib import Path

    from scripted_llm import USAGE, install

    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    # COMPLEX route: one decompose call, then one extract call.
    install(monkeypatch, [{"clauses": INLINE_TEXT.split(". ")}, INLINE_IR])
    root = Path(__file__).resolve().parent.parent
    registry = RunRegistry(root, out_dir=tmp_path)
    started = registry.start(INLINE_TEXT, "naive", 42, 12)
    assert started["status"] == "running"
    done = registry.wait(started["run_key"], timeout=30.0)
    assert done is not None and done["status"] == "done"
    assert done["verdict"]["verdict"] == "PASS"
    assert done["patch"] is None
    assert done["chain_ok"] is True
    assert done["model_calls"] == 2
    assert done["usage_total"] == {k: 2 * v for k, v in USAGE.items()}
    (save,) = done["saves"]
    assert save["valid"] is True and save["errors"] == []
    assert [r["action"] for r in save["value"]["prohibition"]] == ["distribute"]
    assert all(e.get("injector_event") is None for e in done["events"])


def test_model_settings_round_trip(monkeypatch) -> None:
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_BASE_URL", raising=False)
    assert model_status()["configured"] is False
    status = apply_model_settings(
        {"base_url": "https://llama.example/v1/", "api_key": "  secret  "}
    )
    assert status["configured"] is True
    assert status["base_url"] == "https://llama.example/v1"
    assert status["api_key_set"] is True
    assert "secret" not in status.values()
    with pytest.raises(ConvertError):
        apply_model_settings({"base_url": "not-a-url"})
    with pytest.raises(ConvertError):
        apply_model_settings({"api_key": "   "})
    with pytest.raises(ConvertError):
        apply_model_settings(["nope"])


def test_convert_without_model_key_reports_error(monkeypatch, tmp_path) -> None:
    from pathlib import Path as _P

    monkeypatch.chdir(_P(__file__).resolve().parent.parent)
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_BASE_URL", raising=False)
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    registry = RunRegistry(root, out_dir=tmp_path)
    started = registry.start("Allow the team to use dataset-7.", "naive", 42, 12)
    done = registry.wait(started["run_key"], timeout=30.0)
    assert done is not None and done["status"] == "error"
    assert done["error"]["code"] == "E_MODEL_API"
