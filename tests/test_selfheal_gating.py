"""Self-heal gating (the M4 finding): the control plane must not fight Devin's own CI loop.

- While Devin's session is still WORKING, a red CI does not trigger a heal (Devin is iterating).
- The same failing commit is never re-healed; only a new head sha can trigger another heal.
Both bugs were found during the real M4 run (heal cap burned in 60s on one stale red).
"""
from __future__ import annotations

from src.config import Settings
from src.devin.client import DevinClient
from src.devin.schemas import PullRequest, SessionResponse, StructuredOutput
from src.github.verifier import CheckResult, CIState
from src.reconciler.budget import BudgetGuard
from src.reconciler.loop import Reconciler
from src.store import FsmState, Store


class _StubVerifier:
    def __init__(self, result: CheckResult):
        self.result = result

    def verify(self, remediation):
        return self.result


class _StubDevin(DevinClient):
    simulated = True

    def __init__(self, snap: SessionResponse):
        self.snap = snap
        self.messages: list[str] = []

    def create_session(self, req):
        return self.snap

    def get_session(self, sid):
        return self.snap

    def message_session(self, sid, message):
        self.messages.append(message)
        return self.snap


def _snap(status, detail):
    return SessionResponse(
        session_id="devin-x", status=status, status_detail=detail,
        pull_requests=[PullRequest(pr_url="https://github.com/Edetjen19/superset/pull/8", pr_state="open")],
        structured_output=StructuredOutput(remediated=False, pr_url="https://github.com/Edetjen19/superset/pull/8"),
    )


def _recon(store, settings, verifier, devin):
    return Reconciler(
        store=store, devin=devin, verifier=verifier,
        budget=BudgetGuard(global_budget=500, max_acu_limit=10), settings=settings,
    )


def _seed_verifying(store, issue_id):
    rem, _ = store.get_or_create_remediation(issue_id=issue_id, spec_hash="h", issue_number=8)
    store.update_remediation(rem["id"], fsm_state=FsmState.VERIFYING.value, pr_number=8,
                             pr_url="https://github.com/Edetjen19/superset/pull/8")
    store.create_session(session_id="devin-x", remediation_id=rem["id"])
    return rem["id"]


def test_no_heal_while_devin_session_is_working(settings, store):
    rid = _seed_verifying(store, "working")
    red = CheckResult(state=CIState.RED, head_sha="aaa", failing_check="unit-tests (current)", failing_log_tail="boom")
    devin = _StubDevin(_snap("running", "working"))
    rec = _recon(store, settings, _StubVerifier(red), devin)

    for _ in range(3):
        rec.tick()

    r = store.get_remediation(rid)
    assert r["fsm_state"] == FsmState.VERIFYING.value   # waited, did not heal
    assert r["heal_attempts"] == 0
    assert devin.messages == []                          # never messaged Devin mid-iteration


def test_heal_once_per_commit_then_cooldown(settings, store):
    rid = _seed_verifying(store, "idle")
    red = CheckResult(state=CIState.RED, head_sha="sha1", failing_check="unit-tests (current)", failing_log_tail="boom")
    devin = _StubDevin(_snap("running", "waiting_for_user"))  # idle, not working -> heal allowed
    rec = _recon(store, settings, _StubVerifier(red), devin)

    rec.tick()  # red on sha1, idle -> HEALING
    rec.tick()  # HEALING -> message + heal_attempts=1 -> verifying
    r = store.get_remediation(rid)
    assert r["heal_attempts"] == 1
    assert r["last_healed_sha"] == "sha1"
    assert len(devin.messages) == 1

    # Same failing sha on later ticks must NOT re-heal (per-commit cooldown).
    for _ in range(3):
        rec.tick()
    r = store.get_remediation(rid)
    assert r["heal_attempts"] == 1
    assert len(devin.messages) == 1
