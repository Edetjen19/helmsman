"""Store: dedupe, persistence (restart-safety), and the ACU budget gate."""
from __future__ import annotations

from src.config import Settings
from src.reconciler.budget import BudgetGuard
from src.store import FsmState, Store


def test_dedupe_on_issue_and_spec(store):
    a, created_a = store.get_or_create_remediation(issue_id="N1", spec_hash="h1", issue_number=1)
    b, created_b = store.get_or_create_remediation(issue_id="N1", spec_hash="h1", issue_number=1)
    assert created_a is True and created_b is False
    assert a["id"] == b["id"]
    assert len(store.list_remediations()) == 1

    # A different spec hash for the same issue is a distinct remediation.
    _, created_c = store.get_or_create_remediation(issue_id="N1", spec_hash="h2", issue_number=1)
    assert created_c is True
    assert len(store.list_remediations()) == 2


def test_persistence_survives_reopen(tmp_path):
    db = str(tmp_path / "persist.db")
    s1 = Store(db)
    rem, _ = s1.get_or_create_remediation(issue_id="N9", spec_hash="h9", issue_number=9)
    s1.update_remediation(rem["id"], fsm_state=FsmState.DISPATCHED.value)
    s1.create_session(session_id="devin-sim-0001", remediation_id=rem["id"])
    s1.add_event("dispatched", remediation_id=rem["id"], session_id="devin-sim-0001")

    # New Store on the same file = a process restart. In-flight work is still there.
    s2 = Store(db)
    again = s2.get_remediation(rem["id"])
    assert again["fsm_state"] == FsmState.DISPATCHED.value
    assert s2.active_session_for(rem["id"])["session_id"] == "devin-sim-0001"
    assert any(e["type"] == "dispatched" for e in s2.recent_events())


def test_budget_guard_blocks_when_worst_case_exceeds_ceiling(tmp_path):
    db = str(tmp_path / "budget.db")
    store = Store(db)
    guard = BudgetGuard(global_budget=20.0, max_acu_limit=10)

    # Nothing active yet: worst case = 0 + 0 + 10 <= 20 -> allowed.
    assert guard.evaluate(store).allowed is True

    # One active session consuming ACU: worst case = spent + 10 + 10 > 20 -> blocked.
    rem, _ = store.get_or_create_remediation(issue_id="N1", spec_hash="h1", issue_number=1)
    store.create_session(session_id="devin-sim-0001", remediation_id=rem["id"])
    store.update_session("devin-sim-0001", acus_consumed=1.5)
    decision = guard.evaluate(store)
    assert decision.allowed is False
    assert "budget ceiling" in decision.reason


def test_active_remediations_excludes_terminal_and_parked(store):
    rem, _ = store.get_or_create_remediation(issue_id="N1", spec_hash="h1", issue_number=1)
    store.update_remediation(rem["id"], fsm_state=FsmState.AWAITING_MERGE.value)
    assert store.active_remediations() == []  # awaiting_merge is parked on the human gate
    store.update_remediation(rem["id"], fsm_state=FsmState.VERIFYING.value)
    assert len(store.active_remediations()) == 1
