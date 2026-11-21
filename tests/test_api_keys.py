"""Product API keys: issuance stores fingerprints, CLI round-trips, gate honors them."""

from __future__ import annotations

from pathlib import Path

from odrl_agent import db as db_module
from odrl_agent import web as web_mod
from odrl_agent.cli import main as cli_main


def _stub_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "proj"
    (root / "ui").mkdir(parents=True)
    (root / "ui" / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def test_keys_issue_list_revoke(tmp_path, monkeypatch, capsys) -> None:
    root = _stub_root(tmp_path, monkeypatch)
    assert cli_main(["keys", "issue", "--label", "tenant-a"]) == 0
    raw = capsys.readouterr().out.splitlines()[0]
    assert raw.startswith("nl2odrl_")
    assert cli_main(["keys", "list"]) == 0
    listed = capsys.readouterr().out
    assert "tenant-a" in listed and web_mod._fp(raw) in listed
    assert raw not in listed
    conn = db_module.connect(root / "agentcrash.db")
    try:
        assert web_mod._fp(raw) in web_mod._stored_key_fps(conn)
    finally:
        conn.close()
    assert cli_main(["keys", "revoke", "--fp", web_mod._fp(raw)[:8]]) == 0
    assert "revoked 1" in capsys.readouterr().out
    assert cli_main(["keys", "revoke", "--fp", "deadbeef"]) != 0
