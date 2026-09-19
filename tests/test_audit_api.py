from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from rai_card.api import create_app

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "auditor-demo-token"


def client() -> TestClient:
    return TestClient(create_app(ROOT))


def test_health_is_public():
    res = client().get("/v1/health")
    assert res.status_code == 200
    body = res.json()
    assert body["predict"] is False
    assert body["converts_nl"] is False


def test_audit_requires_bearer():
    res = client().get("/v1/audit/card")
    assert res.status_code == 401


def test_audit_rejects_bad_token():
    res = client().get(
        "/v1/audit/card", headers={"Authorization": "Bearer wrong"}
    )
    assert res.status_code == 403


def test_card_and_bundle_are_json():
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Audit-Role": "auditor"}
    c = client()
    card = c.get("/v1/audit/card", headers=headers)
    assert card.status_code == 200
    body = card.json()
    assert body["system_id"] == "nl2odrl-v1"
    assert body["gate_passed"] is True
    assert body["card"]["decision_rights"]["attach_to_pep_requires_signoff"] is True
    assert "over-permission" in (body["markdown"] or "").lower()

    bundle = c.get("/v1/audit/bundle", headers=headers)
    assert bundle.status_code == 200
    assert bundle.json()["spec"]["attestations"]["no_auto_pep_attach"] is True


def test_gate_is_recomputed_not_stamped():
    headers = {"Authorization": f"Bearer {TOKEN}"}
    gate = client().get("/v1/audit/gate", headers=headers).json()
    assert gate["source"] == "recomputed_on_read"
    assert gate["passed"] is True
    ids = {c["check_id"] for c in gate["checks"]}
    assert "eval.over_permission" in ids


def test_gold_trace():
    headers = {"Authorization": f"Bearer {TOKEN}"}
    c = client()
    listing = c.get("/v1/audit/exhibits", headers=headers).json()
    assert any(i["id"] == "01" and i["complete"] for i in listing["items"])
    trace = c.get("/v1/audit/exhibits/01", headers=headers)
    assert trace.status_code == 200
    parts = trace.json()["parts"]
    assert "research group" in parts["nl"]
    assert "odrl" in parts["odrl"].lower() or "@context" in parts["odrl"]
    assert "permit" in parts["cedar"]
    assert "package dataset.d" in parts["rego"]


def test_over_permission_check_endpoint():
    headers = {"Authorization": f"Bearer {TOKEN}"}
    res = client().get("/v1/audit/checks/eval.over_permission", headers=headers)
    assert res.status_code == 200
    assert res.json()["passed"] is True


def test_predict_is_absent():
    c = client()
    assert c.post("/v1/predict", json={"text": "allow all"}).status_code == 404
    assert c.get("/v1/predict").json()["error"] == "not_an_inference_api"
