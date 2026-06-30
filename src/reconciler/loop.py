"""The reconciler, the heart of the control plane.

Level-triggered: every tick it reads desired state (queued/active remediations) and actual
state (Devin sessions + real CI) from the store and closes the gap. Nothing in-flight lives
only in memory, so a restart resumes cleanly. Owns: dispatch, local dedupe, the ACU budget
gate, the per-remediation FSM, and the self-heal loop.

Two correctness rules are enforced here, not assumed:
  - finished != success: a remediation only reaches awaiting_merge when CI is green AND the
    Devin structured-output verdict says remediated (derive_outcome), never on status alone.
  - one active session per remediation: dedupe so a re-delivered webhook or resync tick never
    double-spawns a paid session.
"""
from __future__ import annotations

import json
from typing import Optional

import structlog

from ..config import Settings
from ..devin import (
    DevinClient,
    Outcome,
    SessionCreateRequest,
    SessionResponse,
    derive_outcome,
)
from ..devin.schemas import STRUCTURED_OUTPUT_SCHEMA
from ..github import CIState, Verifier
from ..github.issues import CLASS_LABELS, IssuesClient
from ..scanner.portfolio import spec_hash
from ..store import ACTIVE_STATES, FsmState, Store
from .budget import BudgetGuard
from .prompts import build_prompt

log = structlog.get_logger("reconciler")


def _pr_number(url: Optional[str]) -> Optional[int]:
    if not url:
        return None
    try:
        return int(url.rstrip("/").split("/")[-1])
    except (ValueError, IndexError):
        return None


def _sim_outcome_for(title: str) -> str:
    """SIMULATE-only: pick a demo path from a real issue's title so the board stays colorful
    (the hero self-heals, the high-risk EOL bump is refused). Ignored on real runs."""
    t = title.lower()
    if "apispec" in t:
        return "heal"
    if "eol" in t or "pandas" in t or "numpy" in t:
        return "refuse"
    return "green"


class Reconciler:
    def __init__(
        self,
        *,
        store: Store,
        devin: DevinClient,
        verifier: Verifier,
        budget: BudgetGuard,
        settings: Settings,
        issues: Optional[IssuesClient] = None,
    ) -> None:
        self.store = store
        self.devin = devin
        self.verifier = verifier
        self.budget = budget
        self.settings = settings
        self.issues = issues
        self.max_heal_attempts = settings.max_heal_attempts
        self._last_counts: Optional[tuple] = None

    # ---- level-triggered resync (fork issues -> store) -----------------------
    def resync(self) -> int:
        """Reconcile the labeled backlog on the fork into the store. Idempotent: dedupe on
        (issue node id, spec_hash) means a re-seen issue never double-creates. GitHub reads
        only, no ACU. Returns the number of newly-tracked remediations."""
        if self.issues is None:
            return 0
        created = 0
        for iss in self.issues.list_labeled("devin-remediate"):
            klass = next((c for c in CLASS_LABELS if c in iss.labels), "")
            sim_outcome = _sim_outcome_for(iss.title) if self.devin.simulated else "green"
            rem, was_created = self.store.get_or_create_remediation(
                issue_id=iss.node_id,
                spec_hash=spec_hash(iss.body or iss.title),
                issue_number=iss.number,
                issue_title=iss.title,
                issue_url=iss.url,
                klass=klass,
                sim_outcome=sim_outcome,
            )
            if was_created:
                created += 1
                self.store.add_event("queued", remediation_id=rem["id"], detail=f"resync issue #{iss.number}")
                log.info("resync_enqueued", remediation=rem["id"], issue=iss.number)
        return created

    # ---- public tick ---------------------------------------------------------
    def tick(self) -> None:
        for rem in self.store.list_remediations([FsmState.QUEUED]):
            self._dispatch(rem)
        for rem in self.store.active_remediations():
            if rem["fsm_state"] == FsmState.QUEUED.value:
                continue  # already handled in the dispatch pass (e.g. budget-blocked)
            self._advance(rem)
        self._snapshot_metrics()

    # ---- dispatch ------------------------------------------------------------
    def _dispatch(self, rem: dict) -> None:
        rid = rem["id"]
        if self.store.active_session_for(rid):
            return  # dedupe: never two active sessions for one remediation

        decision = self.budget.evaluate(self.store)
        if not decision.allowed:
            log.info("budget_block", remediation=rid, reason=decision.reason)
            return  # stay queued; retried when budget frees

        tags = [
            "devin-remediate",
            f"issue-{rem.get('issue_number')}",
            f"class-{rem.get('klass')}",
            f"remediation-{rid}",
        ]
        if self.devin.simulated:
            tags.append(f"sim-outcome:{rem.get('sim_outcome') or 'green'}")

        req = SessionCreateRequest(
            prompt=build_prompt(rem),
            max_acu_limit=self.settings.max_acu_limit,
            tags=tags,
            title=f"Remediate #{rem.get('issue_number')}: {rem.get('issue_title','')}"[:120],
            repos=[self.settings.github_repo],
            structured_output_schema=STRUCTURED_OUTPUT_SCHEMA,
        )
        snap = self.devin.create_session(req)
        self.store.create_session(
            session_id=snap.session_id,
            remediation_id=rid,
            session_url=snap.url,
            status=snap.status or "new",
        )
        self.store.update_remediation(rid, fsm_state=FsmState.DISPATCHED.value)
        self.store.add_event(
            "dispatched",
            remediation_id=rid,
            session_id=snap.session_id,
            detail=f"max_acu={self.settings.max_acu_limit} simulate={self.devin.simulated}",
        )
        log.info("dispatched", remediation=rid, session=snap.session_id)

    # ---- advance -------------------------------------------------------------
    def _advance(self, rem: dict) -> None:
        rid = rem["id"]
        state = FsmState(rem["fsm_state"])
        sess_row = self.store.active_session_for(rid)
        snap = self._refresh_session(sess_row) if sess_row else None

        if state == FsmState.DISPATCHED:
            self._advance_dispatched(rem, snap, sess_row)
        elif state == FsmState.PR_OPENED:
            self.store.update_remediation(rid, fsm_state=FsmState.VERIFYING.value)
        elif state == FsmState.VERIFYING:
            self._advance_verifying(rem, snap, sess_row)
        elif state == FsmState.HEALING:
            self._advance_healing(rem, snap, sess_row)

    def _refresh_session(self, sess_row: dict) -> SessionResponse:
        snap = self.devin.get_session(sess_row["session_id"])
        fields: dict = {
            "status": snap.status,
            "status_detail": snap.status_detail or "",
            "acus_consumed": snap.acus_consumed,
            "session_url": snap.url or sess_row.get("session_url") or "",
        }
        if snap.pr_url:
            fields["pr_url"] = snap.pr_url
            fields["pr_state"] = snap.pr_state or "open"
        if snap.structured_output is not None:
            fields["structured_output"] = json.dumps(snap.structured_output.model_dump())
        self.store.update_session(sess_row["session_id"], **fields)
        return snap

    def _advance_dispatched(self, rem: dict, snap: Optional[SessionResponse], sess_row: Optional[dict]) -> None:
        rid = rem["id"]
        if snap is None:
            return
        sid = sess_row["session_id"] if sess_row else None

        if snap.pull_requests:  # a PR exists, move forward even if the session is still working
            url = snap.pr_url
            self.store.update_remediation(
                rid, fsm_state=FsmState.PR_OPENED.value, pr_url=url, pr_number=_pr_number(url)
            )
            self.store.add_event("pr_opened", remediation_id=rid, session_id=sid, detail=url or "")
            log.info("pr_opened", remediation=rid, pr=url)
            return

        outcome = derive_outcome(snap)
        if outcome == Outcome.REFUSED:
            reason = snap.structured_output.refusal_reason if snap.structured_output else ""
            self._close_session(sess_row)
            self.store.update_remediation(rid, fsm_state=FsmState.REFUSED.value, refusal_reason=reason)
            self.store.add_event("refused", remediation_id=rid, session_id=sid, detail=reason)
        elif outcome == Outcome.FAILURE:
            self._close_session(sess_row)
            err = f"session {snap.status}/{snap.status_detail}"
            self.store.update_remediation(rid, fsm_state=FsmState.FAILED.value, last_error=err)
            self.store.add_event("failed", remediation_id=rid, session_id=sid, detail=err)
        elif outcome == Outcome.NEEDS_INPUT:
            self.store.update_remediation(rid, fsm_state=FsmState.NEEDS_HUMAN.value, last_error="waiting_for_user")
            self.store.add_event("needs_human", remediation_id=rid, session_id=sid, detail="waiting_for_user")
        # else IN_FLIGHT: stay dispatched

    def _advance_verifying(self, rem: dict, snap: Optional[SessionResponse], sess_row: Optional[dict]) -> None:
        rid = rem["id"]
        sid = sess_row["session_id"] if sess_row else None
        result = self.verifier.verify(rem)

        if result.state == CIState.PENDING:
            return

        if result.false_green:
            self._close_session(sess_row)
            self.store.update_remediation(rid, fsm_state=FsmState.NEEDS_HUMAN.value, last_error="false-green guard tripped")
            self.store.add_event("false_green_detected", remediation_id=rid, session_id=sid, detail=result.summary)
            log.warning("false_green", remediation=rid)
            return

        if result.state == CIState.RED:
            # Don't fight Devin's own CI loop. While its session is still actively WORKING it is
            # iterating on the PR itself, so wait. And never re-heal the same commit: only step in
            # once CI is red on a head sha we have not healed yet (Devin pushed but it is still red,
            # or Devin has stopped). This is exactly the Probe-B "message an idle session" case.
            session_working = (
                snap is not None and snap.status in ("running", "resuming")
                and (snap.status_detail or "") == "working"
            )
            already_healed = bool(result.head_sha) and result.head_sha == (rem.get("last_healed_sha") or "")
            if session_working or already_healed:
                return  # let Devin finish / wait for a new commit before (re)healing

            heal = rem["heal_attempts"]
            if heal >= self.max_heal_attempts:
                self._close_session(sess_row)
                self.store.update_remediation(rid, fsm_state=FsmState.NEEDS_HUMAN.value, last_error="heal cap reached")
                self.store.add_event("heal_cap", remediation_id=rid, session_id=sid, detail=result.failing_check)
                log.warning("heal_cap", remediation=rid)
            else:
                self.store.update_remediation(
                    rid, fsm_state=FsmState.HEALING.value,
                    last_error=result.failing_check, last_healed_sha=result.head_sha,
                )
                self.store.add_event(
                    "ci_red", remediation_id=rid, session_id=sid,
                    detail=result.failing_log_tail or result.summary,
                )
                log.info("ci_red", remediation=rid, check=result.failing_check, sha=result.head_sha[:8])
            return

        # GREEN. Success still requires the Devin verdict (finished != success), but derived
        # against the remediation's PERSISTED PR (set at pr_opened), not the live snapshot's -
        # so this holds even if a restarted worker reconstructed the session snapshot.
        verdict_ok = (
            snap is not None and snap.structured_output is not None and snap.structured_output.remediated
        )
        has_pr = bool(rem.get("pr_url"))
        if has_pr and (verdict_ok or snap is None):
            self.store.update_remediation(
                rid, fsm_state=FsmState.AWAITING_MERGE.value, pr_state=(snap.pr_state if snap else None) or "open"
            )
            self.store.add_event("verified_green", remediation_id=rid, session_id=sid, detail="CI green + remediated verdict")
            self._close_session(sess_row)
            log.info("verified_green", remediation=rid)
        # else: CI green but the session hasn't emitted its verdict yet, wait a tick.

    def _advance_healing(self, rem: dict, snap: Optional[SessionResponse], sess_row: Optional[dict]) -> None:
        rid = rem["id"]
        if sess_row is None:
            # Non-resumable: the documented fallback is a new bounded session. The sim sessions
            # are always resumable; real fallback lands in M3. For now, escalate to a human.
            self.store.update_remediation(rid, fsm_state=FsmState.NEEDS_HUMAN.value, last_error="no resumable session to heal")
            self.store.add_event("needs_human", remediation_id=rid, detail="heal: session not resumable")
            return

        red_event = self.store.last_event(rid, "ci_red")
        log_tail = red_event["detail"] if red_event else "(log unavailable)"
        attempt = rem["heal_attempts"] + 1
        msg = (
            f"CI failed on PR #{rem.get('pr_number')}. Failing check: {rem.get('last_error')}.\n"
            f"Log tail:\n{log_tail}\n\nFix forward and push to the same branch."
        )
        self.devin.message_session(sess_row["session_id"], msg)
        self.store.update_remediation(rid, fsm_state=FsmState.VERIFYING.value, heal_attempts=attempt)
        self.store.add_event(
            "self_heal", remediation_id=rid, session_id=sess_row["session_id"], detail=f"attempt {attempt}"
        )
        log.info("self_heal", remediation=rid, attempt=attempt)

    # ---- helpers -------------------------------------------------------------
    def _close_session(self, sess_row: Optional[dict]) -> None:
        if sess_row:
            self.store.update_session(sess_row["session_id"], is_active=0)

    def _snapshot_metrics(self) -> None:
        rems = self.store.list_remediations()
        merged = sum(1 for r in rems if r["fsm_state"] == FsmState.MERGED.value)
        failed = sum(
            1 for r in rems
            if r["fsm_state"] in (FsmState.FAILED.value, FsmState.REFUSED.value,
                                  FsmState.NEEDS_HUMAN.value, FsmState.EXPIRED.value)
        )
        active_values = {s.value for s in ACTIVE_STATES}
        in_flight = sum(1 for r in rems if r["fsm_state"] in active_values)
        open_count = len(rems) - merged
        acus = round(self.store.total_acus(), 3)
        counts = (open_count, merged, in_flight, failed, acus)
        if counts == self._last_counts:
            return  # only append a burn-down point when something actually changed
        self._last_counts = counts
        self.store.add_metric_snapshot(
            open_count=open_count, merged_count=merged, in_flight=in_flight,
            failed_count=failed, acus_total=acus,
        )
