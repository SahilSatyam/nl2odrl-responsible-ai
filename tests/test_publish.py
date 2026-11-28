"""Draft-vs-publish HITL gate over HTTP: review queue, approve/edit/reject, once only."""
from __future__ import annotations

import copy
from pathlib import Path

from fastapi.testclient import TestClient
from scripted_llm import BASIC_IR, install, ir, rule

from odrl_agent.app import create_app

REPO = Path(__file__).resolve().parent.parent
TEXT = "Allow the research team to use dataset-7."
#: Grant to a party the text never names: held with UNGROUNDED_PRINCIPAL.
UNGROUNDED_IR = ir(rule(assignee="zephyr-labs"))
#: Permission and prohibition on the same request, no conflict strategy.
CONFLICT_IR = ir(rule(), rule(modality="prohibition"))


def _held_run(monkeypatch, tmp_path, reply, *, repeats=3):
    install(monkeypatch, [reply] * repeats)
    monkeypatch.chdir(REPO)
    client = TestClient(create_app(REPO, out_dir=tmp_path))
    started = client.post("/api/convert",
                          json={"nl_text": TEXT, "policy_variant": "fixed", "seed": 42})  # fmt: skip
    assert started.status_code == 200, started.text
    rk = started.json()["run_key"]
    final = client.app.state.registry.wait(rk, timeout=30.0)
    assert final is not None and final["status"] == "done"
    return client, rk, {}


def _decide(client, rk, headers, **body):
    return client.post(f"/api/runs/{rk}/publish", json=body, headers=headers)


def test_ungrounded_draft_is_queued_then_approved_once(monkeypatch, tmp_path) -> None:
    client, rk, h = _held_run(monkeypatch, tmp_path, UNGROUNDED_IR)
    snap = client.get(f"/api/runs/{rk}").json()
    assert snap["draft"]["status"] == "needs_review" and snap["saves"] == []
    queue = client.get("/api/drafts").json()["drafts"]
    assert [d["run_key"] for d in queue] == [rk]
    assert any("UNGROUNDED_PRINCIPAL" in a for a in queue[0]["draft"]["alerts"])

    assert _decide(client, rk, h, decision="approve").status_code == 400  # reason required
    resp = _decide(client, rk, h, decision="approve", reason="zephyr-labs is our partner lab")
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["published"] is True and out["decision"] == "approve"
    assert any("UNGROUNDED_PRINCIPAL" in a for a in out["alerts"])
    assert "zephyr-labs" in str(out["odrl"])

    again = _decide(client, rk, h, decision="approve", reason="twice")
    assert again.status_code == 409 and again.json()["error"] == "E_DECIDED"
    assert client.get("/api/drafts").json()["drafts"] == []
    decided = client.get("/api/drafts?include_decided=1").json()["drafts"]
    assert decided[0]["decision"] == "approve"
    pairs = client.get("/api/odrl").json()["pairs"]
    assert [p["run_key"] for p in pairs if p["run_key"] == rk] == [rk]
    assert client.get(f"/api/runs/{rk}").json()["decision"]["decision"] == "approve"


def test_conflicting_draft_cannot_be_approved_only_fixed_by_edit(monkeypatch, tmp_path) -> None:
    client, rk, h = _held_run(monkeypatch, tmp_path, CONFLICT_IR)
    draft = client.get(f"/api/runs/{rk}").json()["draft"]
    assert draft["status"] == "needs_review"
    assert draft["reasoning"]["conflicts"], draft["reasoning"]

    blocked = _decide(client, rk, h, decision="approve", reason="looks fine")
    assert blocked.status_code == 422 and blocked.json()["error"] == "E_BLOCKED"
    assert "DEONTIC_CONFLICT" in blocked.json()["hint"]

    still_bad = copy.deepcopy(draft["ir"])
    assert _decide(client, rk, h, decision="edit", reason="x", ir=still_bad).status_code == 422

    strategy_only = copy.deepcopy(draft["ir"])
    strategy_only["conflict_strategy"] = "prohibit"
    assert _decide(client, rk, h, decision="edit", reason="strategy only", ir=strategy_only).status_code == 422

    fixed = copy.deepcopy(draft["ir"])
    fixed["conflict_strategy"] = "prohibit"
    fixed["rules"] = [r for r in fixed["rules"] if r["modality"] != "permission"]
    resp = _decide(client, rk, h, decision="edit", reason="prohibition wins", ir=fixed)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["edited"] is True and out["published"] is True
    assert out["odrl"]["conflict"] in ("prohibit", "http://www.w3.org/ns/odrl/2/prohibit")


def test_edit_with_schema_error_is_400(monkeypatch, tmp_path) -> None:
    client, rk, h = _held_run(monkeypatch, tmp_path, UNGROUNDED_IR)
    bad = {"rules": [{"modality": "permission", "action": "teleport"}]}
    resp = _decide(client, rk, h, decision="edit", reason="r", ir=bad)
    assert resp.status_code == 400 and resp.json()["error"] == "E_SCHEMA"


def test_reject_publishes_nothing(monkeypatch, tmp_path) -> None:
    client, rk, h = _held_run(monkeypatch, tmp_path, UNGROUNDED_IR)
    resp = _decide(client, rk, h, decision="reject", reason="unknown party")
    assert resp.status_code == 200 and resp.json()["published"] is False
    assert all(p["run_key"] != rk for p in client.get("/api/odrl").json()["pairs"])


def test_auto_published_draft_cannot_be_decided(monkeypatch, tmp_path) -> None:
    client, rk, h = _held_run(monkeypatch, tmp_path, BASIC_IR)
    resp = _decide(client, rk, h, decision="approve", reason="r")
    assert resp.status_code == 409 and resp.json()["error"] == "E_STATE"


def test_unknown_decision_is_400(monkeypatch, tmp_path) -> None:
    client, rk, h = _held_run(monkeypatch, tmp_path, UNGROUNDED_IR)
    assert _decide(client, rk, h, decision="ship-it", reason="r").status_code == 400
