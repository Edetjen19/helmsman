"""Level-triggered resync: fork issues -> store, then SIMULATE workers remediate them.

Uses an injected fake issues client (no real gh). Proves: real-shaped issues become tracked
remediations, dedupe holds across resyncs, the demo sim-outcome mapping picks the right path,
and the whole thing settles through the FSM with zero ACU.
"""
from __future__ import annotations

from src.bootstrap import build_reconciler
from src.github.issues import Issue, IssuesClient
from src.store import FsmState

from .conftest import run_ticks


class FakeIssues(IssuesClient):
    def __init__(self, issues):
        self._issues = issues

    def ensure_labels(self) -> None: ...

    def list_labeled(self, label: str):
        return list(self._issues)

    def create_issue(self, *, title, body, labels) -> Issue:  # not used here
        raise NotImplementedError


def _issues():
    return [
        Issue(101, "apispec version cap blocks upgrade", "raise the cap", "https://gh/x/issues/101",
              "NODE_101", ["devin-remediate", "dependency-upgrade"]),
        Issue(102, "datetime.utcnow() is deprecated", "27 sites", "https://gh/x/issues/102",
              "NODE_102", ["devin-remediate", "deprecation-migration"]),
        Issue(103, "EOL dependency pins (pandas / numpy / Flask)", "eol", "https://gh/x/issues/103",
              "NODE_103", ["devin-remediate", "dependency-upgrade"]),
    ]


def test_resync_enqueues_and_dedupes(settings, store):
    reconciler = build_reconciler(settings, store)
    reconciler.issues = FakeIssues(_issues())

    created = reconciler.resync()
    assert created == 3
    rems = {r["issue_number"]: r for r in store.list_remediations()}
    assert rems[101]["klass"] == "dependency-upgrade"
    assert rems[101]["sim_outcome"] == "heal"      # apispec -> self-heal demo path
    assert rems[103]["sim_outcome"] == "refuse"    # EOL -> refusal demo path
    assert rems[102]["sim_outcome"] == "green"

    # Re-running resync creates nothing new (dedupe on node id + spec_hash).
    assert reconciler.resync() == 0
    assert len(store.list_remediations()) == 3


def test_resynced_issues_settle_through_fsm(settings, store):
    reconciler = build_reconciler(settings, store)
    reconciler.issues = FakeIssues(_issues())
    reconciler.resync()
    run_ticks(reconciler, 16)

    rems = {r["issue_number"]: r for r in store.list_remediations()}
    assert rems[101]["fsm_state"] == FsmState.AWAITING_MERGE.value   # healed to green
    assert rems[101]["heal_attempts"] == 1
    assert rems[102]["fsm_state"] == FsmState.AWAITING_MERGE.value
    assert rems[103]["fsm_state"] == FsmState.REFUSED.value


def test_resync_noop_without_issues_client(reconciler):
    # Default reconciler (sync disabled) has no issues client; resync is a no-op.
    assert reconciler.issues is None
    assert reconciler.resync() == 0
