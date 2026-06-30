"""The dashboard's data source is the committed real-results snapshot, not the SIMULATE replay."""
from __future__ import annotations

import json
import pathlib

from fastapi.testclient import TestClient

from src.ingest.app import create_app
from src.store import FsmState

FIXTURE = pathlib.Path("data/real_results.json")


def _load(store):
    store.load_real_results(json.loads(FIXTURE.read_text()))


def test_fixture_is_real_three_remediations_plus_backlog(settings, store):
    _load(store)
    rems = {r["issue_number"]: r for r in store.list_remediations()}
    assert len(rems) == 6

    # Real PR state from GitHub: #8 merged (issue closed), #10/#9 still at the gate.
    assert rems[5]["fsm_state"] == FsmState.MERGED.value and rems[5]["pr_number"] == 8
    assert rems[2]["fsm_state"] == FsmState.AWAITING_MERGE.value and rems[2]["pr_number"] == 10
    assert rems[7]["fsm_state"] == FsmState.AWAITING_MERGE.value and rems[7]["pr_number"] == 9
    assert sum(1 for r in rems.values() if r["fsm_state"] == FsmState.MERGED.value) == 1

    # apispec (#5) self-healed twice on the real run; the real events are preserved.
    assert rems[5]["heal_attempts"] == 2
    types = [e["type"] for e in store.recent_events(200) if e["remediation_id"] == rems[5]["id"]]
    assert types.count("self_heal") == 2 and "verified_green" in types

    # Backlog: #3/#4 open, #6 deferred (a real policy choice, not a fabricated refusal).
    assert rems[3]["fsm_state"] == FsmState.OPEN.value
    assert rems[4]["fsm_state"] == FsmState.OPEN.value
    assert rems[6]["fsm_state"] == FsmState.DEFERRED.value
    assert "deferred" in (rems[6]["note"] or "").lower()
    assert sum(1 for r in rems.values() if r["fsm_state"] == FsmState.REFUSED.value) == 0


def test_board_renders_real_links_no_sim_badges(settings, store):
    _load(store)
    html = TestClient(create_app(settings, store)).get("/partials/fleet").text
    for n in (8, 10, 9):
        assert f"github.com/Edetjen19/superset/pull/{n}" in html
    assert "app.devin.ai/sessions/" in html          # real, clickable session links
    assert "sim-tag" not in html and ">sim<" not in html  # no SIMULATE badges
    assert "deferred" in html                         # the #6 deferral is shown


def test_load_results_endpoint_populates_empty_store(settings, store):
    # Test settings disable autoload, so the store starts empty until the button is clicked.
    client = TestClient(create_app(settings, store))
    assert store.is_empty()
    client.post("/load-results")
    assert len(store.list_remediations()) == 6
