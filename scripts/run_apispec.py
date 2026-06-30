"""M4: ONE real apispec remediation, end to end, stopping at the human gate.

Drives the real reconciler against a single remediation (the fork's apispec issue) so exactly
one Devin session spawns. Real Devin (cog_) + real GitHub CI via the token client. Bounded by
max_acu_limit per session and a wall-clock guard. STOPS at awaiting_merge, never merges.

    docker compose run --rm --no-deps -e GITHUB_TOKEN=$(gh auth token) \
        web python -m scripts.run_apispec --max-acu 20 --max-minutes 45

Reports: Devin session URL, PR URL, acus_consumed, whether self-heal fired, final verdict.
"""
from __future__ import annotations

import argparse
import time

from src.bootstrap import build_reconciler
from src.config import Settings
from src.github.rest import GitHubRest
from src.scanner.portfolio import spec_hash
from src.store import FsmState, Store

STOP_STATES = {
    FsmState.AWAITING_MERGE.value, FsmState.REFUSED.value,
    FsmState.FAILED.value, FsmState.NEEDS_HUMAN.value, FsmState.EXPIRED.value,
}


def _find_apispec_issue(settings: Settings) -> dict:
    rest = GitHubRest.from_settings(settings)
    if rest is None:
        raise SystemExit("No GITHUB_TOKEN in env; pass -e GITHUB_TOKEN=$(gh auth token).")
    for iss in rest.list_labeled_issues("devin-remediate"):
        if "apispec" in (iss.get("title", "") or "").lower():
            return iss
    raise SystemExit("Could not find the apispec issue on the fork.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--max-acu", type=int, default=20)
    p.add_argument("--max-minutes", type=int, default=45)
    p.add_argument("--poll", type=int, default=25)
    p.add_argument("--db", default="data/m4.db")
    args = p.parse_args()

    # Real mode: SIMULATE off, cog_ key + GITHUB_TOKEN from env. One session, capped.
    settings = Settings(simulate=False, max_acu_limit=args.max_acu, db_path=args.db)
    if not settings.devin_api_key.startswith("cog_"):
        raise SystemExit("No cog_ key in env.")

    issue = _find_apispec_issue(settings)
    print(f"apispec issue: #{issue['number']} {issue['title']}\n  {issue['html_url']}")

    store = Store(settings.db_path)
    rem, _ = store.get_or_create_remediation(
        issue_id=issue.get("node_id") or f"gh-{issue['number']}",
        spec_hash=spec_hash(issue.get("body") or issue["title"]),
        issue_number=issue["number"],
        issue_title=issue["title"],
        issue_url=issue["html_url"],
        klass="dependency-upgrade",
    )
    rid = rem["id"]

    reconciler = build_reconciler(settings, store)
    print(f"REAL RUN: simulate={settings.simulate} max_acu_limit={settings.max_acu_limit} "
          f"global_budget={settings.global_acu_budget} repo={settings.github_repo}")
    print("Driving one remediation to the human gate (will NOT merge)…\n")

    deadline = time.monotonic() + args.max_minutes * 60
    last_state = None
    while time.monotonic() < deadline:
        reconciler.tick()
        r = store.get_remediation(rid)
        sess = store.active_session_for(rid) or (store.sessions_for(rid)[-1] if store.sessions_for(rid) else None)
        acus = sess["acus_consumed"] if sess else 0.0
        surl = sess["session_url"] if sess else ""
        if r["fsm_state"] != last_state:
            print(f"[{time.strftime('%H:%M:%S')}] state={r['fsm_state']:<14} acus={acus} heal={r['heal_attempts']} pr={r['pr_url'] or '-'}")
            if surl:
                print(f"            session: {surl}")
            last_state = r["fsm_state"]
        if r["fsm_state"] in STOP_STATES:
            break
        time.sleep(args.poll)

    # ---- report ----
    r = store.get_remediation(rid)
    sessions = store.sessions_for(rid)
    total_acu = round(sum(s["acus_consumed"] for s in sessions), 3)
    events = [e["type"] for e in store.recent_events(300) if e["remediation_id"] == rid]
    self_heal_fired = r["heal_attempts"] > 0 or "self_heal" in events
    print("\n================ M4 RESULT ================")
    print(f"final state:     {r['fsm_state']}")
    print(f"PR:              {r['pr_url'] or '(none)'}")
    print(f"acus_consumed:   {total_acu}  (cap/session {settings.max_acu_limit})")
    print(f"self-heal fired: {self_heal_fired}  (heal_attempts={r['heal_attempts']}, ci_red={'ci_red' in events})")
    for s in sessions:
        print(f"session:         {s['session_url']}  status={s['status']}/{s['status_detail']} acus={s['acus_consumed']}")
    if r["fsm_state"] == FsmState.AWAITING_MERGE.value:
        print("\nParked at the HUMAN GATE. Not merged. Approve in the dashboard or report PR for review.")
    else:
        print(f"\nStopped in {r['fsm_state']} (not at the gate). last_error={r.get('last_error')!r}")
    print("==========================================")


if __name__ == "__main__":
    main()
