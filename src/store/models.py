"""Domain enums + the per-remediation state machine (DESIGN.md §5.2)."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum


def now_iso() -> str:
    """Timezone-aware UTC timestamp. (We practice what the portfolio preaches:
    no naive datetime.utcnow() anywhere in this codebase.)"""
    return datetime.now(timezone.utc).isoformat()


class FsmState(str, Enum):
    # happy path
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    PR_OPENED = "pr_opened"
    VERIFYING = "verifying"
    HEALING = "healing"
    AWAITING_MERGE = "awaiting_merge"
    MERGED = "merged"
    # off-ramps
    REFUSED = "refused"            # Devin declined (structured_output.refused)
    NEEDS_HUMAN = "needs_human"    # heal cap hit / waiting_for_user
    FAILED = "failed"             # error / no PR / remediated==false
    EXPIRED = "expired"           # ACU limit / budget ceiling
    # backlog (shown on the real-results board; the reconciler does not act on these)
    OPEN = "open"                 # a labeled issue not yet remediated
    DEFERRED = "deferred"         # a deliberate policy choice not to auto-remediate (e.g. high-risk EOL)


# Terminal: the reconciler does no further work on these (OPEN/DEFERRED are inert backlog).
TERMINAL_STATES = frozenset(
    {FsmState.MERGED, FsmState.REFUSED, FsmState.NEEDS_HUMAN, FsmState.FAILED, FsmState.EXPIRED,
     FsmState.OPEN, FsmState.DEFERRED}
)
# Active: the reconciler advances these each tick. AWAITING_MERGE is parked
# (waiting on the human approval gate), so it is neither active nor terminal.
ACTIVE_STATES = frozenset(
    {FsmState.QUEUED, FsmState.DISPATCHED, FsmState.PR_OPENED, FsmState.VERIFYING, FsmState.HEALING}
)


class RemediationClass(str, Enum):
    DEPENDENCY_UPGRADE = "dependency-upgrade"
    DEPRECATION_MIGRATION = "deprecation-migration"
    LINT_GRADUATION = "lint-graduation"
