"""Deterministic SIMULATE client, replays a v3 session lifecycle with zero network
and zero ACU. Used for all dev/test/demo-b-roll. Drives every code path the real
client does (PR opens, success verdict, refusal, failure, self-heal) so the skeleton
exercises the full FSM without spending a cent.

Progression is poll-driven: each get_session() advances the session one step. With a
~20s reconciler poll interval, a session walks from "working" to a real PR + verdict
over a handful of ticks. The desired outcome is carried on a `sim-outcome:<x>` tag set
by the reconciler, exactly like the real client only knowing what you send it.
"""
from __future__ import annotations

from typing import Any

from .client import DevinClient
from .schemas import PullRequest, SessionCreateRequest, SessionResponse, StructuredOutput


class _SimSession:
    __slots__ = ("poll", "outcome", "pr_url", "pr_number", "repo", "title", "tags", "healed")

    def __init__(self, *, outcome: str, pr_url: str, pr_number: int, repo: str, title: str, tags: list[str]):
        self.poll = -1  # incremented to 0 on first read
        self.outcome = outcome
        self.pr_url = pr_url
        self.pr_number = pr_number
        self.repo = repo
        self.title = title
        self.tags = tags
        self.healed = False


def _outcome_from_tags(tags: list[str]) -> str:
    for t in tags:
        if t.startswith("sim-outcome:"):
            return t.split(":", 1)[1]
    return "green"


def _issue_from_tags(tags: list[str]) -> int | None:
    for t in tags:
        if t.startswith("issue-"):
            try:
                return int(t.split("-", 1)[1])
            except ValueError:
                return None
    return None


def _class_from_tags(tags: list[str]) -> str:
    for t in tags:
        if t.startswith("class-"):
            return t.split("-", 1)[1]
    return "deprecation-migration"


# Per-class verdict flavor so the board reads truthfully for each kind of fix.
_CLASS_VERDICT = {
    "dependency-upgrade": (
        "Version cap blocked the upgrade; bumped it and fixed the OpenAPI spec assertion the recompile could not.",
        ["requirements/base.in", "requirements/base.txt", "tests/unit_tests/test_apispec.py"],
        "pytest tests/unit_tests -k apispec",
    ),
    "deprecation-migration": (
        "Replaced the deprecated API at each site, preserving naive-vs-aware (and epoch) semantics per call.",
        ["superset/commands/report/execute.py", "superset/utils/dates.py"],
        "ruff check && pytest tests/unit_tests/commands/report",
    ),
    "lint-graduation": (
        "Graduated the rule to error, applied the autofixes, and bumped the plugin to the installed major.",
        ["superset-frontend/.oxlintrc.json"],
        "cd superset-frontend && npx oxlint --config oxlint.json",
    ),
}


class SimulatedDevinClient(DevinClient):
    simulated = True

    def __init__(self) -> None:
        self._sessions: dict[str, _SimSession] = {}
        self._counter = 0

    # ---- lifecycle -----------------------------------------------------------
    def create_session(self, req: SessionCreateRequest) -> SessionResponse:
        self._counter += 1
        n = self._counter
        sid = f"devin-sim-{n:04d}"
        repo = req.repos[0] if req.repos else "Edetjen19/superset"
        issue_n = _issue_from_tags(req.tags)
        pr_number = 9000 + (issue_n if issue_n is not None else n)
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"
        self._sessions[sid] = _SimSession(
            outcome=_outcome_from_tags(req.tags),
            pr_url=pr_url,
            pr_number=pr_number,
            repo=repo,
            title=req.title,
            tags=req.tags,
        )
        return self._snapshot(sid)

    def get_session(self, session_id: str) -> SessionResponse:
        if session_id not in self._sessions:
            # Unknown after a restart: reconstruct a benign finished-green session (with a
            # non-empty PR so success still derives) so a late poll never drags the FSM back.
            n = abs(hash(session_id)) % 1000
            pr_number = 9000 + n
            self._sessions[session_id] = _SimSession(
                outcome="green",
                pr_url=f"https://github.com/Edetjen19/superset/pull/{pr_number}",
                pr_number=pr_number,
                repo="Edetjen19/superset",
                title="",
                tags=[],
            )
            self._sessions[session_id].poll = 2
        return self._snapshot(session_id, advance=True)

    def message_session(self, session_id: str, message: str) -> SessionResponse:
        s = self._sessions.get(session_id)
        if s is None:
            return self.get_session(session_id)
        # A heal message resumes the session and lands a fix on the same branch.
        s.healed = True
        s.poll = 1  # back to "working", will re-finish green on subsequent polls
        return self._snapshot(session_id)

    # ---- snapshot rendering --------------------------------------------------
    def _snapshot(self, session_id: str, advance: bool = False) -> SessionResponse:
        s = self._sessions[session_id]
        if advance:
            s.poll += 1
        else:
            s.poll = max(s.poll, 0)
        p = s.poll
        url = f"https://app.devin.ai/sessions/{session_id}"
        base: dict[str, Any] = {"session_id": session_id, "url": url, "tags": s.tags, "title": s.title}

        if s.outcome == "fail":
            if p <= 0:
                return SessionResponse(**base, status="running", status_detail="working", acus_consumed=0.3)
            return SessionResponse(**base, status="error", status_detail="error", acus_consumed=0.5)

        if s.outcome == "refuse":
            if p <= 0:
                return SessionResponse(**base, status="running", status_detail="working", acus_consumed=0.2)
            so = StructuredOutput(
                remediated=False,
                refused=True,
                refusal_reason="Change touches a 100%-coverage module; declining to auto-edit.",
                root_cause="Out-of-policy edit surface.",
            )
            return SessionResponse(
                **base, status="exit", status_detail="finished", structured_output=so, acus_consumed=0.4
            )

        # 'green' and 'heal' both have the session open a PR and report success; for
        # 'heal' the *verifier* returns red until the reconciler sends a heal message.
        if p <= 0:
            return SessionResponse(**base, status="running", status_detail="working", acus_consumed=0.3)
        if p == 1:
            return SessionResponse(**base, status="running", status_detail="working", acus_consumed=0.7)
        pr = [PullRequest(pr_url=s.pr_url, pr_state="open")]
        if p == 2:
            return SessionResponse(
                **base, status="running", status_detail="working", pull_requests=pr, acus_consumed=1.1
            )
        klass = _class_from_tags(s.tags)
        root_cause, files_changed, verification = _CLASS_VERDICT.get(klass, _CLASS_VERDICT["deprecation-migration"])
        so = StructuredOutput(
            remediated=True,
            refused=False,
            change_class=klass,
            root_cause=root_cause,
            files_changed=files_changed,
            tests_added=True,
            verification_ran=verification,
            pr_url=s.pr_url,
            residual_risk="low",
        )
        acu = 1.6 + (0.4 if s.healed else 0.0)
        return SessionResponse(
            **base, status="exit", status_detail="finished", structured_output=so,
            pull_requests=pr, acus_consumed=acu,
        )
