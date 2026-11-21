"""Billing export: per-run line items scoped by key and date, JSON and CSV."""

from __future__ import annotations

import json
from pathlib import Path

from agentcrash import db as db_module
from odrl_agent import web as web_mod
from odrl_agent.cli import main as cli_main


def _stub_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "proj"
    (root / "ui").mkdir(parents=True)
    (root / "ui" / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def _seed_unattributed(conn, run_key: str, created_at: str) -> None:
    db_module.insert_run(
        conn,
        {
            "run_key": run_key,
            "run_id": "r0",
            "scenario_id": "odrl_convert",
            "faults": [],
            "policy_variant": "fixed",
            "policy_file": "policies/fixed-odrl-convert.yaml",
            "seed": 42,
            "max_steps": 12,
            "nl_text": "Allow Y.",
            "status": "done",
            "verdict": "PASS",
            "model_calls": 1,
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "trace_path": "t",
            "state_path": "s",
            "created_at": created_at,
        },
    )
    conn.commit()


def _seed_run(
    conn, run_key: str, fp: str, created_at: str, tokens: int = 10
) -> None:
    web_mod._ensure_saas_tables(conn)
    db_module.insert_run(
        conn,
        {
            "run_key": run_key,
            "run_id": "r1",
            "scenario_id": "odrl_convert",
            "faults": [],
            "policy_variant": "fixed",
            "policy_file": "policies/fixed-odrl-convert.yaml",
            "seed": 42,
            "max_steps": 12,
            "nl_text": "Allow X.",
            "status": "done",
            "verdict": "PASS",
            "model_calls": 1,
            "prompt_tokens": tokens,
            "completion_tokens": tokens,
            "total_tokens": 2 * tokens,
            "trace_path": "t",
            "state_path": "s",
            "created_at": created_at,
        },
    )
    conn.execute(
        "INSERT INTO nl2odrl_run_tenants (run_key, key_fp, created_at)"
        " VALUES (?, ?, ?)",
        (run_key, fp, created_at),
    )
    conn.commit()


def _db(tmp_path: Path):
    conn = db_module.connect(tmp_path / "b.db")
    db_module.migrate(conn)
    return conn


def test_billing_lines_filters(tmp_path) -> None:
    conn = _db(tmp_path)
    try:
        fp_a, fp_b = "a" * 16, "b" * 16
        _seed_run(conn, "k-old", fp_a, "2026-01-01T00:00:00+00:00")
        _seed_run(conn, "k-new", fp_a, "2026-06-01T00:00:00+00:00")
        _seed_run(conn, "k-other", fp_b, "2026-06-01T00:00:00+00:00")
        _seed_unattributed(conn, "k-legacy", "2026-02-01T00:00:00+00:00")
        assert {r["run_key"] for r in web_mod.billing_lines(conn)} == {
            "k-old",
            "k-new",
            "k-other",
            "k-legacy",
        }
        legacy = [
            r for r in web_mod.billing_lines(conn) if r["run_key"] == "k-legacy"
        ]
        assert [r["key_fp"] for r in legacy] == ["local"]
        assert [r["run_key"] for r in web_mod.billing_lines(conn, fp_a)] == [
            "k-old",
            "k-new",
        ]
        assert [r["run_key"] for r in web_mod.billing_lines(
            conn, fp_a, since="2026-03-01T00:00:00+00:00"
        )] == ["k-new"]
        assert web_mod.billing_lines(conn, "c" * 16) == []
    finally:
        conn.close()


def test_billing_cli_export_json_and_csv(tmp_path, monkeypatch, capsys) -> None:
    root = _stub_root(tmp_path, monkeypatch)
    conn = db_module.connect(root / "agentcrash.db")
    try:
        db_module.migrate(conn)
        _seed_run(conn, "k-1", "f" * 16, "2026-06-01T00:00:00+00:00", tokens=7)
    finally:
        conn.close()
    assert cli_main(["billing", "export"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert [r["run_key"] for r in body["lines"]] == ["k-1"]
    assert body["lines"][0]["total_tokens"] == 14
    assert cli_main(["billing", "export", "--format", "csv"]) == 0
    csv_out = capsys.readouterr().out
    assert csv_out.splitlines()[0].startswith("run_key,key_fp,created_at")
    assert "k-1" in csv_out
    assert cli_main(["billing", "export", "--key-fp", "0" * 16]) == 0
    assert json.loads(capsys.readouterr().out)["lines"] == []
