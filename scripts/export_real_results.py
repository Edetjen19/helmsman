"""Export the three real remediations (from the per-run SQLite DBs) into a committed,
secrets-free fixture the dashboard loads on a fresh clone with no creds.

Reads data/m4.db (#5 -> PR #8), data/m6_issue2.db (#2 -> PR #10), data/m6_issue7.db
(#7 -> PR #9), adds the three not-yet-remediated backlog issues (#3, #4 open; #6 deferred),
synthesizes a small burn-down series, and writes data/real_results.json.

The per-run DBs are gitignored; this script documents how the committed JSON was produced.
Run in the image:  docker compose run --rm --no-deps web python -m scripts.export_real_results
"""
from __future__ import annotations

import json

from src.store import Store

# (db file, issue number, heal_attempts to show on the board)
# #5 self-healed twice on the real run (data/m4.db events: ci_red -> self_heal x2 -> green).
SOURCES = [("data/m4.db", 5, 2), ("data/m6_issue2.db", 2, 0), ("data/m6_issue7.db", 7, 0)]

REPO = "Edetjen19/superset"


def _export_remediation(db_path: str, issue_number: int, heal: int) -> dict:
    s = Store(db_path)
    rem = next(r for r in s.list_remediations() if r["issue_number"] == issue_number)
    sessions = []
    for sess in s.sessions_for(rem["id"]):
        sessions.append({
            "session_id": sess["session_id"], "kind": sess.get("kind", "remediate"),
            "status": sess["status"], "status_detail": sess["status_detail"],
            "acus_consumed": sess["acus_consumed"], "pr_url": sess["pr_url"], "pr_state": sess["pr_state"],
            "structured_output": sess["structured_output"], "session_url": sess["session_url"],
            "is_active": 0, "created_at": sess["created_at"], "updated_at": sess["updated_at"],
        })
    events = [
        {"type": e["type"], "detail": e["detail"], "session_id": e["session_id"], "created_at": e["created_at"]}
        for e in s.recent_events(200)[::-1] if e["remediation_id"] == rem["id"]
    ]
    return {
        "issue_id": rem["issue_id"], "spec_hash": rem["spec_hash"], "issue_number": rem["issue_number"],
        "issue_title": rem["issue_title"], "issue_url": rem["issue_url"], "klass": rem["klass"],
        "fsm_state": "awaiting_merge", "pr_url": rem["pr_url"], "pr_number": rem["pr_number"],
        "pr_state": rem["pr_state"] or "open", "heal_attempts": heal,
        "labeled_at": rem["labeled_at"], "created_at": rem["created_at"], "updated_at": rem["updated_at"],
        "sessions": sessions, "events": events,
    }


def _backlog(n: int, title: str, klass: str, state: str, note: str = "") -> dict:
    return {
        "issue_id": f"gh-{n}", "spec_hash": f"backlog-{n}", "issue_number": n, "issue_title": title,
        "issue_url": f"https://github.com/{REPO}/issues/{n}", "klass": klass, "fsm_state": state,
        "pr_url": None, "pr_number": None, "pr_state": None, "heal_attempts": 0, "note": note,
        "sessions": [], "events": [],
    }


def _apply_live_pr_state(remediated: list[dict]) -> int:
    """Pull each PR's LIVE state from GitHub so the snapshot reflects reality now. With no token
    we leave the default (awaiting_merge). Returns the merged count."""
    from src.config import get_settings
    from src.github.rest import GitHubRest

    rest = GitHubRest.from_settings(get_settings())
    merged = 0
    for r in remediated:
        if not (rest and r.get("pr_number")):
            continue
        try:
            st = rest.get_pull_state(r["pr_number"])
        except Exception as exc:  # noqa: BLE001
            print(f"  live state fetch failed for PR #{r['pr_number']}: {exc}")
            continue
        if st["merged"]:
            r["fsm_state"], r["pr_state"] = "merged", "merged"
            merged += 1
        else:
            r["fsm_state"], r["pr_state"] = "awaiting_merge", st["state"] or "open"
        print(f"  PR #{r['pr_number']}: {r['fsm_state']}")
    return merged


def main() -> None:
    remediated = [_export_remediation(db, n, heal) for db, n, heal in SOURCES]
    merged_count = _apply_live_pr_state(remediated)
    # earliest labeled_at as the burn-down anchor
    anchor = min(r["labeled_at"] for r in remediated)
    backlog = [
        _backlog(3, "datetime.utcfromtimestamp() is deprecated", "deprecation-migration", "open"),
        _backlog(4, "SQLAlchemy 1.4 legacy Query.get() in production code", "deprecation-migration", "open"),
        _backlog(6, "EOL dependency pins (pandas / numpy / Flask)", "dependency-upgrade", "deferred",
                 "deferred: high-risk EOL bump (not auto-remediated)"),
    ]
    # Burn-down: 6 labeled issues, 3 remediated to the gate over the run -> open 6->3, 0 merged.
    opens = [6, 5, 4, 3, 3]
    mergeds = [0, 0, 0, 0, merged_count]
    snaps = [
        {"ts": f"{anchor[:19]}", "open_count": o, "merged_count": mc, "in_flight": 0,
         "failed_count": 0, "acus_total": 0.0}
        for o, mc in zip(opens, mergeds)
    ]
    payload = {
        "generated_from": ["data/m4.db (#5->PR#8)", "data/m6_issue2.db (#2->PR#10)", "data/m6_issue7.db (#7->PR#9)"],
        "note": "Real results from the last remediation run; 3 PRs at the human gate, 0 merged, 0.0 ACU measured.",
        "remediations": remediated + backlog,
        "metric_snapshots": snaps,
    }
    with open("data/real_results.json", "w") as f:
        json.dump(payload, f, indent=2)
    print(f"wrote data/real_results.json: {len(payload['remediations'])} remediations, {len(snaps)} snapshots")


if __name__ == "__main__":
    main()
