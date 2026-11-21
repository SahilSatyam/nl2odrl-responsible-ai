"""Focused tests for workflow options (all offline).

Covers ``validate_odrl`` memoization, prompt-pack loading and
``run --instruction-pack`` adoption. The ReAct-only options (``$from`` arg
references, the agent brief and its repair hint) retired with the loop.
Model transport tests live in ``test_llm.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from agentcrash.clock import VirtualClock
from agentcrash.ledger import Ledger
from scripted_llm import BASIC_IR, install

from odrl_agent.cli import main as cli_main
from odrl_agent.dspy_opt import (
    TRAINSET_ROWS,
    PackRejectedError,
    load_pack,
    trainset_hash,
)
from odrl_agent.tools import ToolRegistry

REPO = Path(__file__).resolve().parent.parent
PACK = REPO / "prompts" / "pack-probe-v1.json"


def test_validate_cache_memoizes_and_isolates() -> None:
    specs = [{"name": "validate_odrl", "latency_ms": 50, "timeout_ms": 1000, "mutating": False}]
    clock = VirtualClock()
    registry = ToolRegistry(specs, Ledger(), None, None, None)
    doc = {
        "@context": "http://www.w3.org/ns/odrl.jsonld",
        "type": "Offer",
        "uid": "urn:odrl:policy:cache-1",
        "assigner": "urn:party:steward",
        "permission": [{"action": "use", "target": "urn:data:d", "assignee": "urn:party:team"}],
    }
    first = registry.call("validate_odrl", {"odrl": doc}, clock)
    assert first["outcome"] == "success" and first["data"]["valid"] is True
    assert first["data"]["checked"] == "odrl-2.2-shapes-v1"
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


def _run_args(tmp_path: Path, pack: Path) -> list[str]:
    return [
        "run",
        "--scenario",
        str(REPO / "scenarios" / "odrl_convert" / "scenario.yaml"),
        "--seed",
        "42",
        "--trace-out",
        str(tmp_path / "p.jsonl"),
        "--state-out",
        str(tmp_path / "p.json"),
        "--instruction-pack",
        str(pack),
    ]


def test_cli_run_accepts_instruction_pack(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(REPO)
    install(monkeypatch, [BASIC_IR])
    assert cli_main(_run_args(tmp_path, PACK)) == 0
    assert (tmp_path / "p.jsonl").exists()


def test_cli_run_rejects_bad_pack(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(REPO)
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert cli_main(_run_args(tmp_path, bad)) == 2
