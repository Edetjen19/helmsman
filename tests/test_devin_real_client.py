"""The REAL Devin client, exercised entirely against respx mocks, never the network,
never ACU. Proves create/poll/message + retry/422 handling and success derivation.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from src.devin.client import PermanentApiError, RealDevinClient
from src.devin.schemas import Outcome, SessionCreateRequest, derive_outcome

ORG_BASE = "https://api.devin.ai/v3/organizations/org-test"


def _client():
    return RealDevinClient(api_key="cog_test", org_base=ORG_BASE)


@respx.mock
def test_create_poll_message_and_success_derivation():
    respx.post(f"{ORG_BASE}/sessions").mock(
        return_value=httpx.Response(200, json={"session_id": "devin-abc", "status": "running", "status_detail": "working"})
    )
    respx.get(f"{ORG_BASE}/sessions/devin-abc").mock(
        return_value=httpx.Response(
            200,
            json={
                "session_id": "devin-abc",
                "status": "exit",
                "status_detail": "finished",
                "acus_consumed": 1.7,
                "structured_output": {"remediated": True, "refused": False, "root_cause": "cap", "pr_url": "https://github.com/Edetjen19/superset/pull/42"},
                "pull_requests": [{"pr_url": "https://github.com/Edetjen19/superset/pull/42", "pr_state": "open"}],
            },
        )
    )
    respx.post(f"{ORG_BASE}/sessions/devin-abc/messages").mock(
        return_value=httpx.Response(200, json={"session_id": "devin-abc", "status": "running", "status_detail": "working"})
    )

    c = _client()
    created = c.create_session(SessionCreateRequest(prompt="fix it", max_acu_limit=5, repos=["Edetjen19/superset"]))
    assert created.session_id == "devin-abc"

    polled = c.get_session("devin-abc")
    assert polled.pr_url.endswith("/42")
    assert polled.acus_consumed == 1.7
    assert derive_outcome(polled) == Outcome.SUCCESS

    healed = c.message_session("devin-abc", "CI failed, fix forward")
    assert healed.status == "running"


@respx.mock
def test_finished_without_pr_is_failure_not_success():
    respx.get(f"{ORG_BASE}/sessions/devin-x").mock(
        return_value=httpx.Response(
            200,
            json={"session_id": "devin-x", "status": "exit", "status_detail": "finished",
                  "structured_output": {"remediated": True, "refused": False, "root_cause": "x", "pr_url": ""}},
        )
    )
    polled = _client().get_session("devin-x")
    assert derive_outcome(polled) == Outcome.FAILURE  # finished + remediated but NO pr -> failure


@respx.mock
def test_422_is_permanent(monkeypatch):
    respx.post(f"{ORG_BASE}/sessions").mock(return_value=httpx.Response(422, text="bad schema"))
    with pytest.raises(PermanentApiError):
        _client().create_session(SessionCreateRequest(prompt="x", max_acu_limit=1))


@respx.mock
def test_429_then_success_retries(monkeypatch):
    monkeypatch.setattr("src.devin.client.time.sleep", lambda *_: None)  # no real backoff sleep
    route = respx.get(f"{ORG_BASE}/sessions/devin-r").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"session_id": "devin-r", "status": "running", "status_detail": "working"}),
        ]
    )
    polled = _client().get_session("devin-r")
    assert polled.session_id == "devin-r"
    assert route.call_count == 2


def test_factory_refuses_real_client_without_cog_key():
    from src.config import Settings
    from src.devin import build_devin_client

    s = Settings(simulate=False, devin_api_key="", _env_file=None)
    with pytest.raises(RuntimeError):
        build_devin_client(s)
