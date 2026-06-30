"""The M1 gate: the end-to-end SIMULATE loop goes green, issue -> session -> PR -> CI ->
self-heal -> awaiting_merge -> (human) merged. Zero network, zero real ACU."""
from __future__ import annotations

from fastapi.testclient import TestClient

from src.ingest.app import create_app
from src.store import FsmState

from .conftest import run_ticks, seed_portfolio


def test_end_to_end_loop_settles(reconciler, store):
    seed_portfolio(store)
    run_ticks(reconciler, 16)

    rems = {r["issue_number"]: r for r in store.list_remediations()}

    # The four straightforward fixes reach the human approval gate (real fork issues #2,#3,#4,#7).
    for n in (2, 3, 4, 7):
        assert rems[n]["fsm_state"] == FsmState.AWAITING_MERGE.value, rems[n]

    # Hero (apispec, issue #5): CI went red, the reconciler self-healed, CI went green.
    hero = rems[5]
    assert hero["fsm_state"] == FsmState.AWAITING_MERGE.value, hero
    assert hero["heal_attempts"] == 1

    # High-risk EOL bump (issue #6): Devin declined; we do NOT auto-merge it.
    assert rems[6]["fsm_state"] == FsmState.REFUSED.value
    assert rems[6]["refusal_reason"]

    # The self-heal beats actually happened, in order.
    types = [e["type"] for e in store.recent_events(300)]
    assert "ci_red" in types and "self_heal" in types and "verified_green" in types

    # Cost is recorded from (simulated) acus_consumed, and the client is the SIMULATE one.
    assert reconciler.devin.simulated is True
    assert store.total_acus() > 0


def test_success_requires_pr_and_verdict_not_status_alone(reconciler, store):
    """A 'fail' outcome (status error, no PR) must NOT be treated as success."""
    store.get_or_create_remediation(
        issue_id="fail-1", spec_hash="h", issue_number=5001, klass="dependency-upgrade",
        sim_outcome="fail",
    )
    run_ticks(reconciler, 6)
    rem = store.list_remediations()[0]
    assert rem["fsm_state"] == FsmState.FAILED.value
    assert rem["pr_url"] is None


def test_no_double_dispatch_for_one_remediation(reconciler, store):
    store.get_or_create_remediation(issue_id="g-1", spec_hash="h", issue_number=6001, sim_outcome="green")
    run_ticks(reconciler, 4)
    rem = store.list_remediations()[0]
    # Exactly one session ever created for this remediation (dedupe).
    assert len(store.sessions_for(rem["id"])) == 1


def test_gate_links_to_github_and_has_no_merge_endpoint(settings, store, reconciler):
    """The dashboard never merges: there is no /approve endpoint, and an awaiting_merge row links
    out to the PR on GitHub for a human to review and merge."""
    store.get_or_create_remediation(issue_id="g-2", spec_hash="h", issue_number=7001, sim_outcome="green")
    run_ticks(reconciler, 8)
    rem = store.list_remediations()[0]
    assert rem["fsm_state"] == FsmState.AWAITING_MERGE.value

    client = TestClient(create_app(settings, store))
    # the merge endpoint is gone
    assert client.post(f"/remediations/{rem['id']}/approve").status_code == 404
    # the board offers a review link to the real PR instead of a merge button
    html = client.get("/partials/fleet").text
    assert "Review PR" in html and "/approve" not in html


def test_dashboard_renders(settings, store, reconciler):
    seed_portfolio(store)
    run_ticks(reconciler, 8)
    client = TestClient(create_app(settings, store))
    home = client.get("/")
    assert home.status_code == 200
    assert "Helmsman" in home.text
    assert "REAL RESULTS" in home.text               # the board shows real results, not a SIMULATE replay
    assert 'rel="icon"' in home.text                 # favicon link present
    partial = client.get("/partials/fleet")
    assert partial.status_code == 200 and "Fleet" in partial.text
    fav = client.get("/favicon.ico")                 # browser auto-request resolves
    assert fav.status_code == 200 and "svg" in fav.headers["content-type"]
