"""HMAC verification + the webhook ingest path."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from src.ingest.app import create_app
from src.ingest.security import compute_signature, verify_signature
from src.store import Store


def test_verify_signature_roundtrip():
    secret, body = "s3cr3t", b'{"hello":"world"}'
    sig = compute_signature(secret, body)
    assert verify_signature(secret, body, sig) is True
    assert verify_signature(secret, body + b"x", sig) is False   # body tampered
    assert verify_signature("wrong", body, sig) is False         # wrong secret
    assert verify_signature(secret, body, None) is False         # missing header
    assert verify_signature(secret, body, "garbage") is False    # malformed header
    assert verify_signature("", body, sig) is False              # no secret configured


def _labeled_payload(number=4002, with_label=True):
    labels = [{"name": "deprecation-migration"}]
    if with_label:
        labels.append({"name": "devin-remediate"})
    return {
        "action": "labeled",
        "label": {"name": "devin-remediate"},
        "issue": {
            "number": number,
            "node_id": f"NODE_{number}",
            "title": "datetime.utcnow() deprecated",
            "html_url": f"https://github.com/Edetjen19/superset/issues/{number}",
            "body": "evidence: 27 sites",
            "labels": labels,
        },
        "repository": {"full_name": "Edetjen19/superset"},
    }


def _client(settings, store):
    return TestClient(create_app(settings, store))


def test_webhook_enqueues_signed_labeled_issue(settings, store):
    client = _client(settings, store)
    body = json.dumps(_labeled_payload()).encode()
    sig = compute_signature(settings.webhook_secret, body)
    resp = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig})
    assert resp.status_code == 200
    assert resp.json()["created"] is True
    rems = store.list_remediations()
    assert len(rems) == 1 and rems[0]["fsm_state"] == "queued"
    assert rems[0]["klass"] == "deprecation-migration"


def test_webhook_rejects_bad_signature(settings, store):
    client = _client(settings, store)
    body = json.dumps(_labeled_payload()).encode()
    resp = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert resp.status_code == 401
    assert store.list_remediations() == []


def test_webhook_ignores_unlabeled_issue(settings, store):
    client = _client(settings, store)
    body = json.dumps(_labeled_payload(with_label=False)).encode()
    sig = compute_signature(settings.webhook_secret, body)
    resp = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig})
    assert resp.status_code == 200 and resp.json() == {"ignored": True}
    assert store.list_remediations() == []


def test_webhook_rejects_oversized_payload(settings, store):
    client = _client(settings, store)
    big = b'{"action":"labeled","pad":"' + b"a" * 200_050 + b'"}'
    resp = client.post("/webhook", content=big, headers={"X-Hub-Signature-256": "sha256=x"})
    assert resp.status_code == 413


def test_webhook_dedupes_redelivery(settings, store):
    client = _client(settings, store)
    body = json.dumps(_labeled_payload()).encode()
    sig = compute_signature(settings.webhook_secret, body)
    first = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig})
    second = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig})
    assert first.json()["created"] is True
    assert second.json()["created"] is False        # re-delivery collapses onto one row
    assert len(store.list_remediations()) == 1
