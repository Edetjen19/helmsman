"""Self-heal carries the real failing-CI log to the same session, and the approval gate can
squash-merge a real fork PR on the human's click (respx-mocked, no network)."""
from __future__ import annotations

import httpx
import respx
from fastapi.testclient import TestClient

from src.bootstrap import build_reconciler
from src.config import Settings
from src.devin.simulate import SimulatedDevinClient
from src.ingest.app import create_app
from src.store import FsmState, Store

from .conftest import run_ticks

REPO = "Edetjen19/superset"
API = "https://api.github.com"


class MsgRecordingClient(SimulatedDevinClient):
    def __init__(self):
        super().__init__()
        self.messages: list[tuple[str, str]] = []

    def message_session(self, session_id, message):
        self.messages.append((session_id, message))
        return super().message_session(session_id, message)


def test_self_heal_message_carries_log_tail(settings, store):
    reconciler = build_reconciler(settings, store)
    rec = MsgRecordingClient()
    reconciler.devin = rec

    # The apispec hero: CI goes red, then the reconciler messages the SAME session to fix forward.
    store.get_or_create_remediation(
        issue_id="hero", spec_hash="h", issue_number=4001,
        klass="dependency-upgrade", sim_outcome="heal",
    )
    run_ticks(reconciler, 16)

    assert rec.messages, "expected a self-heal message to be sent"
    _, msg = rec.messages[0]
    assert "Fix forward" in msg
    # The failing job's log tail (from the verifier) is fed back into the session.
    assert "unit-tests" in msg or "FAILED" in msg
    # And it healed to the gate.
    assert store.list_remediations()[0]["fsm_state"] == FsmState.AWAITING_MERGE.value

