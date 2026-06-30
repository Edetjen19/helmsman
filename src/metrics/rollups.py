"""Dashboard rollups (DESIGN.md §7). Everything here is derived from the store, the
ACU/$ numbers come from real `acus_consumed`, labeled as measured (the rate is a stated
assumption, never presented as measured)."""
from __future__ import annotations

import json
import statistics
from datetime import datetime
from typing import Any, Optional

from ..config import Settings
from ..store import FsmState, Store

_TERMINAL_FAIL = {
    FsmState.FAILED.value, FsmState.REFUSED.value, FsmState.NEEDS_HUMAN.value, FsmState.EXPIRED.value,
}
_ACTIVE = {
    FsmState.QUEUED.value, FsmState.DISPATCHED.value, FsmState.PR_OPENED.value,
    FsmState.VERIFYING.value, FsmState.HEALING.value,
}


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def compute_metrics(store: Store, settings: Settings) -> dict[str, Any]:
    rems = store.list_remediations()
    total = len(rems)
    by_state: dict[str, int] = {}
    for r in rems:
        by_state[r["fsm_state"]] = by_state.get(r["fsm_state"], 0) + 1

    merged = by_state.get(FsmState.MERGED.value, 0)
    awaiting = by_state.get(FsmState.AWAITING_MERGE.value, 0)
    failed = sum(by_state.get(s, 0) for s in _TERMINAL_FAIL)
    refused = by_state.get(FsmState.REFUSED.value, 0)
    needs_human = by_state.get(FsmState.NEEDS_HUMAN.value, 0)
    in_flight = sum(by_state.get(s, 0) for s in _ACTIVE)

    # Cost: real ACU from sessions, $ at a STATED rate (assumption, not measured).
    acus_total = round(store.total_acus(), 2)
    resolved = merged + awaiting  # PRs that reached green
    acus_per_pr = round(acus_total / resolved, 2) if resolved else 0.0
    usd_per_pr = round(acus_per_pr * settings.acu_usd_rate, 2)

    # MTTR: labeled -> verified green, per remediation that got there.
    green_at: dict[int, str] = {}
    for ev in store.events_by_type("verified_green"):
        rid = ev["remediation_id"]
        if rid is not None and rid not in green_at:
            green_at[rid] = ev["created_at"]
    durations: list[float] = []
    rem_by_id = {r["id"]: r for r in rems}
    for rid, gts in green_at.items():
        r = rem_by_id.get(rid)
        start, end = _parse(r["labeled_at"]) if r else None, _parse(gts)
        if start and end:
            durations.append((end - start).total_seconds())
    mttr_seconds = round(statistics.median(durations)) if durations else None

    # Self-heal success rate.
    heal_events = store.events_by_type("self_heal")
    healed_rids = {e["remediation_id"] for e in heal_events}
    healed_to_green = sum(1 for rid in healed_rids if rid in green_at)
    heal_rate = round(100 * healed_to_green / len(healed_rids)) if healed_rids else None

    # Throughput.
    prs_opened = len(store.events_by_type("pr_opened"))

    # Budget gauge.
    budget_pct = round(100 * acus_total / settings.global_acu_budget) if settings.global_acu_budget else 0

    snapshots = store.metric_snapshots()
    spark = _sparkline(snapshots)

    return {
        "total": total,
        "by_state": by_state,
        "merged": merged,
        "awaiting": awaiting,
        "failed": failed,
        "refused": refused,
        "needs_human": needs_human,
        "in_flight": in_flight,
        "queued": by_state.get(FsmState.QUEUED.value, 0),
        "prs_opened": prs_opened,
        "acus_total": acus_total,
        "acus_per_pr": acus_per_pr,
        "usd_per_pr": usd_per_pr,
        "acu_usd_rate": settings.acu_usd_rate,
        "global_acu_budget": settings.global_acu_budget,
        "budget_pct": min(budget_pct, 100),
        "mttr_seconds": mttr_seconds,
        "mttr_human": _fmt_duration(mttr_seconds),
        "heal_attempts": len(heal_events),
        "heal_rate": heal_rate,
        "success_rate": round(100 * merged / total) if total else None,
        "resolved": awaiting + merged,           # reached the gate or merged
        "burn_down": snapshots,
        "spark": spark,
        "simulate": settings.simulate,
        "can_merge": bool(settings.github_token),  # the gate squash-merges for real only with a token
        "repo": settings.github_repo,
    }


def _sparkline(snapshots: list[dict[str, Any]], width: int = 320, height: int = 64) -> dict[str, Any]:
    """Two polylines for the burn-down chart on a fixed pixel grid (no preserveAspectRatio).
    merged rises (accent), open declines (faint); a hairline baseline sits at y=58.
    Points: x = 2 + 316*i/(n-1), y = 58 - 52*v/Vmax."""
    pts = [(s["open_count"], s["merged_count"]) for s in snapshots]
    if len(pts) < 2:
        return {"width": width, "height": height, "open": "", "merged": "", "baseline": 58, "has_data": False}
    vmax = max(1, max(max(o, m) for o, m in pts))
    n = len(pts)

    def line(idx: int) -> str:
        out = []
        for i, pair in enumerate(pts):
            x = round(2 + 316 * i / (n - 1), 1)
            y = round(58 - 52 * pair[idx] / vmax, 1)
            out.append(f"{x},{y}")
        return " ".join(out)

    return {"width": width, "height": height, "open": line(0), "merged": line(1), "baseline": 58, "has_data": True}


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


# Status chip tone per FSM state: only ok / warn / bad / neutral (no brand accent).
STATE_TONE = {
    FsmState.QUEUED.value: "neutral",
    FsmState.DISPATCHED.value: "neutral",
    FsmState.PR_OPENED.value: "neutral",
    FsmState.VERIFYING.value: "neutral",
    FsmState.HEALING.value: "warn",
    FsmState.AWAITING_MERGE.value: "warn",   # caution: awaiting a human
    FsmState.MERGED.value: "ok",
    FsmState.REFUSED.value: "bad",
    FsmState.NEEDS_HUMAN.value: "bad",
    FsmState.FAILED.value: "bad",
    FsmState.EXPIRED.value: "bad",
    FsmState.OPEN.value: "neutral",       # backlog: not yet remediated
    FsmState.DEFERRED.value: "neutral",   # deliberate policy choice, not a failure
}

# Activity ledger: only four color roles; everything else stays neutral (muted).
_ACTIVITY_ROLE = {
    "verified_green": "ok", "merged": "ok",
    "self_heal": "warn", "ci_red": "warn",
    "refused": "bad", "failed": "bad", "heal_cap": "bad",
    "false_green_detected": "bad", "merge_failed": "bad", "needs_human": "bad",
}


def recent_activity(store, limit: int = 10) -> list[dict[str, Any]]:
    """Compact ledger: newest-first, collapsing consecutive same-type events into 'type ×N',
    capped to `limit`. Color fires only on exceptions; ~everything else is neutral."""
    groups: list[dict[str, Any]] = []
    for e in store.recent_events(80):  # newest first
        t = e["type"]
        if groups and groups[-1]["type"] == t:
            groups[-1]["count"] += 1
            continue
        if len(groups) >= limit:
            break
        groups.append({
            "type": t,
            "detail": (e["detail"] or "").replace(" simulate=False", "").replace(" simulate=True", ""),
            "time": (e["created_at"] or "")[11:19],
            "count": 1,
            "role": _ACTIVITY_ROLE.get(t, "neutral"),
        })
    return groups


def fleet_rows(store: Store) -> list[dict[str, Any]]:
    """One row per remediation, joined to its latest session, for the fleet board."""
    rows: list[dict[str, Any]] = []
    for r in store.list_remediations():
        sessions = store.sessions_for(r["id"])
        sess = sessions[-1] if sessions else None
        verdict = None
        if sess and sess.get("structured_output"):
            try:
                verdict = json.loads(sess["structured_output"])
            except (json.JSONDecodeError, TypeError):
                verdict = None
        rows.append(
            {
                "id": r["id"],
                "issue_number": r["issue_number"],
                "issue_title": r["issue_title"],
                "issue_url": r["issue_url"],
                "klass": r["klass"],
                "state": r["fsm_state"],
                "tone": STATE_TONE.get(r["fsm_state"], "muted"),
                "pr_url": r["pr_url"],
                "pr_number": r["pr_number"],
                "heal_attempts": r["heal_attempts"],
                "refusal_reason": r["refusal_reason"],
                "last_error": r["last_error"],
                "note": r["note"] if "note" in r.keys() else None,
                "session_id": sess["session_id"] if sess else None,
                "session_url": sess["session_url"] if sess else None,
                "session_status": (f'{sess["status"]}/{sess["status_detail"]}' if sess else None),
                "acus": round(sess["acus_consumed"], 2) if sess else 0.0,
                "verdict": verdict,
            }
        )
    return rows
