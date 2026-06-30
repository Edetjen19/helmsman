"""CI verification for the fork's real PRs, with the false-green guard.

The subtle bug we refuse to ship (DESIGN.md §4.2 / Probe A): `unit-tests-required` is an
always-running anchor that passes when the unit-test job *succeeded OR was skipped*. A PR
that doesn't trip the change-detector goes falsely green. So the verifier confirms that the
job *relevant to what the PR changed* actually ran with conclusion==success: a python change
must run `unit-tests (current)`; a frontend-only change must run a frontend job (the python
tests legitimately skip). Only failure/timed_out/action_required count as RED.

Interface + a deterministic SIMULATE impl (used by the M1 skeleton) + a real `gh`-backed
impl (wired in M3 against actual fork PRs). Success is derived here for CI; final success is
CI-green AND the Devin structured-output verdict (the reconciler combines both).
"""
from __future__ import annotations

import abc
import json
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class CIState(str, Enum):
    PENDING = "pending"
    GREEN = "green"
    RED = "red"


@dataclass
class CheckResult:
    state: CIState
    unit_tests_ran: bool = False     # the real unit-test job ran (not skipped)
    false_green: bool = False        # anchor green but unit tests were skipped
    failing_check: str = ""
    failing_log_tail: str = ""
    summary: str = ""
    head_sha: str = ""               # the PR head sha this verdict is for (per-commit heal cooldown)
    raw: dict[str, Any] = field(default_factory=dict)


# Required gating contexts on the fork (DESIGN.md §3 / Probe A).
_ANCHOR = "unit-tests-required"
# Names that indicate the real unit-test job actually executed.
_UNIT_JOB_HINTS = ("unit-tests", "test-sqlite", "python-unit")
# Names that indicate a frontend gating job ran (for frontend-only changes).
_FRONTEND_JOB_HINTS = ("frontend-build", "jest", "frontend")
# Only these conclusions are hard failures; cancelled/stale/skipped/neutral are non-failing.
_RED_CONCLUSIONS = {"failure", "timed_out", "action_required", "startup_failure"}


class Verifier(abc.ABC):
    @abc.abstractmethod
    def verify(self, remediation: dict[str, Any]) -> CheckResult: ...


# --------------------------------------------------------------------------- #
# SIMULATE
# --------------------------------------------------------------------------- #
class SimulatedVerifier(Verifier):
    """Deterministic CI: a PR shows 'pending' for the first poll, then resolves.

    'green'/'refuse'/'fail' PRs resolve green. A 'heal' PR resolves RED on its first
    completed run and GREEN only after a heal landed (heal_attempts > 0). Poll state is
    keyed by (pr_number, heal_attempts) so a heal restarts CI cleanly.
    """

    def __init__(self) -> None:
        self._polls: dict[tuple[int, int], int] = {}

    def verify(self, remediation: dict[str, Any]) -> CheckResult:
        pr = int(remediation.get("pr_number") or 0)
        heal = int(remediation.get("heal_attempts") or 0)
        outcome = remediation.get("sim_outcome") or "green"
        key = (pr, heal)
        self._polls[key] = self._polls.get(key, 0) + 1
        polls = self._polls[key]

        if polls < 2:
            return CheckResult(state=CIState.PENDING, summary="CI running (test-sqlite, pre-commit, lint-check)…")

        if outcome == "heal" and heal == 0:
            tail = (
                "FAILED tests/unit_tests/test_apispec.py::test_openapi_spec_builds - "
                "AssertionError: spec assertion drifted after apispec bump\n"
                "=== 1 failed, 482 passed in 41.2s ==="
            )
            return CheckResult(
                state=CIState.RED,
                unit_tests_ran=True,
                failing_check="test-sqlite / unit-tests (current)",
                failing_log_tail=tail,
                summary="unit-tests (current) failed",
            )

        return CheckResult(
            state=CIState.GREEN,
            unit_tests_ran=True,
            summary="all required checks green; unit-tests (current) ran",
        )


# --------------------------------------------------------------------------- #
# Real (gh CLI), wired in M3 against actual fork PRs
# --------------------------------------------------------------------------- #
def analyze_check_runs(runs: list[dict[str, Any]], changed_files: Optional[list[str]] = None) -> CheckResult:
    """Pure: turn a commit's check-runs into a CI verdict, applying the change-aware false-green guard.

    The guard's job is to catch "green only because the meaningful job was skipped." Which job is
    meaningful depends on what the PR changed: a python change must actually run `unit-tests
    (current)`; a frontend-only change must run a frontend job (the python tests legitimately skip).
    `changed_files=None` keeps the python-centric default (back-compat for SIMULATE + unit tests).

    Only `failure / timed_out / action_required / startup_failure` count as RED; `cancelled / stale /
    skipped / neutral` are non-failing (they would otherwise trigger spurious heals).
    """
    if not runs or any(r.get("status") != "completed" for r in runs):
        return CheckResult(state=CIState.PENDING, summary=f"{len(runs)} checks, some still running")

    failing = [r for r in runs if r.get("conclusion") in _RED_CONCLUSIONS]
    unit_ran = any(r.get("conclusion") == "success" for r in runs if _is_unit_job(r.get("name", "")))
    fe_ran = any(r.get("conclusion") == "success" for r in runs if _is_frontend_job(r.get("name", "")))

    if failing:
        f = failing[0]
        return CheckResult(
            state=CIState.RED,
            unit_tests_ran=unit_ran,
            failing_check=f.get("name", "?"),
            failing_log_tail=_log_tail(f),
            summary=f"{len(failing)} failing check(s): " + ", ".join(r.get("name", "?") for r in failing[:3]),
        )

    # Which gating job MUST have actually run for this change?
    require_unit, require_fe = _required_gates(changed_files)
    missing = []
    if require_unit and not unit_ran:
        missing.append("unit-tests (current)")
    if require_fe and not fe_ran:
        missing.append("frontend-build")
    if missing:
        return CheckResult(
            state=CIState.RED,
            unit_tests_ran=unit_ran,
            false_green=True,
            failing_check=_ANCHOR,
            summary="false green: required job(s) did not run (skipped/absent): " + ", ".join(missing),
        )

    return CheckResult(
        state=CIState.GREEN,
        unit_tests_ran=unit_ran,
        summary="all required checks green; the change's gating job ran",
    )


def _required_gates(changed_files: Optional[list[str]]) -> tuple[bool, bool]:
    """(require_unit, require_fe) based on what the PR changed. None -> python-centric default."""
    if changed_files is None:
        return True, False
    py = any(
        (f.startswith(("superset/", "tests/")) and f.endswith(".py"))
        or f.startswith("requirements/")
        or f in ("pyproject.toml", "setup.py", "setup.cfg")
        for f in changed_files
    )
    fe = any(f.startswith("superset-frontend/") for f in changed_files)
    if py:
        return True, False          # python change: the python unit tests are the gate
    if fe:
        return False, True          # frontend-only: a frontend job is the gate
    return False, False             # docs/config only: no test gate required


class _CheckRunVerifier(Verifier):
    """Shared verify() for real backends: PR -> head_sha -> check-runs -> analyze."""

    def _head_sha(self, pr_number: int) -> str: ...
    def _check_runs(self, sha: str) -> list[dict[str, Any]]: ...
    def _pull_files(self, pr_number: int) -> Optional[list[str]]:
        return None

    def verify(self, remediation: dict[str, Any]) -> CheckResult:
        pr_number = remediation.get("pr_number")
        if not pr_number:
            return CheckResult(state=CIState.PENDING, summary="no PR number yet")
        sha = self._head_sha(int(pr_number))
        result = analyze_check_runs(self._check_runs(sha), self._pull_files(int(pr_number)))
        result.head_sha = sha
        return result


class RestVerifier(_CheckRunVerifier):
    """Real CI verification via the token GitHub REST client (works in-container)."""

    def __init__(self, rest) -> None:  # type: ignore[no-untyped-def]
        self.rest = rest

    def _head_sha(self, pr_number: int) -> str:
        return self.rest.get_pull_head_sha(pr_number)

    def _check_runs(self, sha: str) -> list[dict[str, Any]]:
        return self.rest.get_check_runs(sha)

    def _pull_files(self, pr_number: int) -> Optional[list[str]]:
        return self.rest.get_pull_files(pr_number)


class GhVerifier(_CheckRunVerifier):
    """Real CI verification via the gh CLI (host fallback when no token is configured)."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _gh_json(self, path: str) -> Any:
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def _head_sha(self, pr_number: int) -> str:
        return self._gh_json(f"repos/{self.repo}/pulls/{pr_number}")["head"]["sha"]

    def _check_runs(self, sha: str) -> list[dict[str, Any]]:
        return self._gh_json(f"repos/{self.repo}/commits/{sha}/check-runs").get("check_runs", [])

    def _pull_files(self, pr_number: int) -> Optional[list[str]]:
        data = self._gh_json(f"repos/{self.repo}/pulls/{pr_number}/files?per_page=100")
        return [f.get("filename", "") for f in data]


def _is_unit_job(name: str) -> bool:
    low = name.lower()
    if _ANCHOR in low:
        return False  # the anchor is not the real job
    return any(h in low for h in _UNIT_JOB_HINTS)


def _is_frontend_job(name: str) -> bool:
    return any(h in name.lower() for h in _FRONTEND_JOB_HINTS)


def _log_tail(check_run: dict[str, Any], max_chars: int = 2000) -> str:
    out = check_run.get("output", {}) or {}
    text = out.get("summary") or out.get("text") or out.get("title") or ""
    return text[-max_chars:]


def build_verifier(settings) -> Verifier:  # type: ignore[no-untyped-def]
    if settings.simulate:
        return SimulatedVerifier()
    from .rest import GitHubRest

    rest = GitHubRest.from_settings(settings)
    if rest is not None:
        return RestVerifier(rest)
    return GhVerifier(settings.github_repo)  # host fallback (no token configured)
