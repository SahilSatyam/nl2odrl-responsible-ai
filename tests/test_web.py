"""Convert API: validation, catalog, background run, verdict, patch (scripted)."""

from __future__ import annotations

import pytest

from odrl_agent.web import (
    ConvertError,
    RunRegistry,
    build_inline_run,
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
    assert "author the ODRL" in first["scenario"]["task"]
    fixed = build_inline_run("Allow X.", "fixed", 42, 12, root, 1, tmp_path)
    assert fixed["policy_file"] == "policies/fixed-odrl-convert.yaml"


SLOPPY_DOC = {
    "@context": "http://www.w3.org/ns/odrl.jsonld",
    "type": "Offer",
    "uid": "urn:odrl:policy:dataset-7",
    "permission": [{"action": "use", "assignee": "research-team"}],
}


def _scripted(monkeypatch, actions):
    import json
    import urllib.request

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


def _sloppy_naive_actions():
    parse = {"type": "call", "tool": "parse_policy", "args": {"doc_id": "pasted-request-1"}}
    write = {
        "type": "call",
        "tool": "save_odrl",
        "args": {"op": "write", "record": "odrl-policy-1", "value": SLOPPY_DOC},
    }
    return [parse, write, {"type": "done", "reason": "saved"}]


def test_convert_flow_reports_verdict_and_patch(monkeypatch, tmp_path) -> None:
    from pathlib import Path

    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    _scripted(monkeypatch, _sloppy_naive_actions())
    root = Path(__file__).resolve().parent.parent
    registry = RunRegistry(root, out_dir=tmp_path)
    started = registry.start("Allow the team to use dataset-7.", "naive", 42, 12)
    assert started["status"] == "running"
    done = registry.wait(started["run_key"], timeout=30.0)
    assert done is not None and done["status"] == "done"
    assert done["verdict"]["verdict"] == "FAIL"
    assert {r["id"] for r in done["verdict"]["results"] if not r["passed"]} >= {
        "odrl_valid",
    }
    assert done["patch"] is not None
    assert done["patch"]["patch"]["rules"], "reference patch must carry rules"
    assert done["chain_ok"] is True
    assert done["model_calls"] == 0
    assert done["usage_total"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    assert len(done["events"]) > 4
    assert all(e.get("injector_event") is None for e in done["events"])


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
