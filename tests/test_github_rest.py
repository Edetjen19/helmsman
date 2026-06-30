"""Real GitHub access (REST) + the false-green guard, all against respx mocks. No network."""
from __future__ import annotations

import httpx
import pytest
import respx

from src.config import Settings
from src.github import CIState, RestVerifier, analyze_check_runs, build_verifier
from src.github.issues import RestIssues, build_issues, GhIssues
from src.github.rest import GitHubRest

REPO = "Edetjen19/superset"
API = "https://api.github.com"


def _completed(name, conclusion, summary=""):
    return {"name": name, "status": "completed", "conclusion": conclusion, "output": {"summary": summary}}


# --------------------------------------------------------------------------- #
# analyze_check_runs, the false-green guard, pure
# --------------------------------------------------------------------------- #
def test_analyze_pending_when_a_check_incomplete():
    runs = [_completed("lint-check", "success"), {"name": "test-sqlite", "status": "in_progress", "conclusion": None}]
    assert analyze_check_runs(runs).state == CIState.PENDING


def test_analyze_green_when_unit_job_ran():
    runs = [
        _completed("unit-tests-required", "success"),
        _completed("test-sqlite / unit-tests (current)", "success"),
        _completed("lint-check", "success"),
    ]
    r = analyze_check_runs(runs)
    assert r.state == CIState.GREEN and r.unit_tests_ran is True


def test_analyze_red_captures_failing_check_and_log_tail():
    runs = [
        _completed("test-sqlite / unit-tests (current)", "failure", summary="FAILED tests/unit_tests/x.py::t - boom"),
        _completed("lint-check", "success"),
    ]
    r = analyze_check_runs(runs)
    assert r.state == CIState.RED
    assert "unit-tests" in r.failing_check
    assert "boom" in r.failing_log_tail


def test_analyze_false_green_when_unit_skipped_but_anchor_green():
    # The subtle bug we refuse to ship: anchor passes because unit-tests was SKIPPED, not run.
    runs = [
        _completed("unit-tests-required", "success"),
        _completed("test-sqlite / unit-tests (current)", "skipped"),
        _completed("lint-check", "success"),
    ]
    r = analyze_check_runs(runs)
    assert r.state == CIState.RED
    assert r.false_green is True
    assert r.unit_tests_ran is False


def test_analyze_frontend_only_green_when_fe_job_ran():
    # A frontend-only PR: python unit-tests legitimately SKIP; the frontend job is the gate.
    runs = [
        _completed("unit-tests-required", "success"),
        _completed("test-sqlite / unit-tests (current)", "skipped"),
        _completed("frontend-build", "success"),
    ]
    r = analyze_check_runs(runs, changed_files=["superset-frontend/src/x.tsx"])
    assert r.state == CIState.GREEN          # NOT a false-green: python skip is correct here
    assert r.false_green is False


def test_analyze_frontend_only_false_green_when_fe_job_skipped():
    runs = [
        _completed("unit-tests-required", "success"),
        _completed("frontend-build", "skipped"),
    ]
    r = analyze_check_runs(runs, changed_files=["superset-frontend/src/x.tsx"])
    assert r.state == CIState.RED and r.false_green is True


def test_analyze_python_change_requires_unit_even_with_files():
    runs = [_completed("unit-tests-required", "success"), _completed("frontend-build", "success")]
    r = analyze_check_runs(runs, changed_files=["superset/models/helpers.py"])
    assert r.state == CIState.RED and r.false_green is True   # python touched but unit-tests didn't run


def test_analyze_cancelled_and_skipped_are_not_red():
    runs = [
        _completed("test-sqlite / unit-tests (current)", "success"),
        _completed("flaky-job", "cancelled"),
        _completed("optional-job", "skipped"),
        _completed("neutral-job", "neutral"),
    ]
    assert analyze_check_runs(runs, changed_files=["superset/x.py"]).state == CIState.GREEN


def test_analyze_docs_only_needs_no_test_gate():
    runs = [_completed("unit-tests-required", "success"), _completed("test-sqlite / unit-tests (current)", "skipped")]
    assert analyze_check_runs(runs, changed_files=["README.md", "docs/x.md"]).state == CIState.GREEN


# --------------------------------------------------------------------------- #
# RestVerifier, PR -> head_sha -> check-runs, via respx
# --------------------------------------------------------------------------- #
def _rest():
    return GitHubRest(token="ghx_test", repo=REPO, api_url=API)


@respx.mock
def test_rest_verifier_green():
    respx.get(f"{API}/repos/{REPO}/pulls/42").mock(return_value=httpx.Response(200, json={"head": {"sha": "abc123"}}))
    respx.get(f"{API}/repos/{REPO}/pulls/42/files?per_page=100").mock(
        return_value=httpx.Response(200, json=[{"filename": "superset/foo.py"}]))
    respx.get(f"{API}/repos/{REPO}/commits/abc123/check-runs").mock(
        return_value=httpx.Response(200, json={"check_runs": [
            _completed("unit-tests-required", "success"),
            _completed("test-sqlite / unit-tests (current)", "success"),
        ]})
    )
    v = RestVerifier(_rest())
    assert v.verify({"pr_number": 42}).state == CIState.GREEN


@respx.mock
def test_rest_verifier_red_with_log():
    respx.get(f"{API}/repos/{REPO}/pulls/7").mock(return_value=httpx.Response(200, json={"head": {"sha": "deadbeef"}}))
    respx.get(f"{API}/repos/{REPO}/pulls/7/files?per_page=100").mock(
        return_value=httpx.Response(200, json=[{"filename": "superset/foo.py"}]))
    respx.get(f"{API}/repos/{REPO}/commits/deadbeef/check-runs").mock(
        return_value=httpx.Response(200, json={"check_runs": [
            _completed("test-sqlite / unit-tests (current)", "failure", summary="AssertionError: spec drifted"),
        ]})
    )
    res = RestVerifier(_rest()).verify({"pr_number": 7})
    assert res.state == CIState.RED
    assert "spec drifted" in res.failing_log_tail


def test_rest_verifier_no_pr_is_pending():
    assert RestVerifier(_rest()).verify({"pr_number": None}).state == CIState.PENDING


# --------------------------------------------------------------------------- #
# RestIssues, list (filters PRs) + create
# --------------------------------------------------------------------------- #
@respx.mock
def test_rest_issues_list_filters_prs_and_maps():
    respx.get(f"{API}/repos/{REPO}/issues?labels=devin-remediate&state=open&per_page=100").mock(
        return_value=httpx.Response(200, json=[
            {"number": 2, "title": "datetime", "body": "b", "html_url": "u2", "node_id": "N2",
             "labels": [{"name": "devin-remediate"}, {"name": "deprecation-migration"}]},
            {"number": 9, "title": "a PR not an issue", "pull_request": {"url": "x"}, "labels": []},
        ])
    )
    issues = RestIssues(_rest()).list_labeled("devin-remediate")
    assert [i.number for i in issues] == [2]
    assert issues[0].node_id == "N2"
    assert "deprecation-migration" in issues[0].labels


@respx.mock
def test_rest_issues_create():
    respx.post(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(201, json={"number": 12, "title": "t", "body": "b", "html_url": "u12", "node_id": "N12", "labels": []})
    )
    iss = RestIssues(_rest()).create_issue(title="t", body="b", labels=["devin-remediate"])
    assert iss.number == 12 and iss.url == "u12"


@respx.mock
def test_rest_retries_429(monkeypatch):
    monkeypatch.setattr("src.github.rest.time.sleep", lambda *_: None)
    route = respx.get(f"{API}/repos/{REPO}/pulls/1").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"head": {"sha": "s"}}),
    ])
    assert _rest().get_pull_head_sha(1) == "s"
    assert route.call_count == 2


@respx.mock
def test_rest_merge_pull():
    route = respx.put(f"{API}/repos/{REPO}/pulls/5/merge").mock(return_value=httpx.Response(200, json={"merged": True}))
    assert _rest().merge_pull(5)["merged"] is True
    assert route.called


# --------------------------------------------------------------------------- #
# factory selection
# --------------------------------------------------------------------------- #
def test_build_verifier_selection():
    from src.github.verifier import SimulatedVerifier, GhVerifier

    sim = build_verifier(Settings(simulate=True, _env_file=None))
    assert isinstance(sim, SimulatedVerifier)

    rest_v = build_verifier(Settings(simulate=False, github_token="ghx", _env_file=None))
    assert isinstance(rest_v, RestVerifier)

    gh_v = build_verifier(Settings(simulate=False, github_token="", _env_file=None))
    assert isinstance(gh_v, GhVerifier)


def test_build_issues_selection():
    rest_i = build_issues(Settings(simulate=False, github_token="ghx", _env_file=None))
    assert isinstance(rest_i, RestIssues)
    gh_i = build_issues(Settings(simulate=True, github_token="", _env_file=None))
    assert isinstance(gh_i, GhIssues)
