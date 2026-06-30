"""Probe B (DESIGN.md §4.2): does a Devin session accept a message after going idle, and
resume? This is the self-heal-by-message assumption. REAL ACU, capped hard at 3.

Cheap by design: a trivial prompt, NO repo (so it can't spend ACU opening a PR), low
max_acu_limit, an ACU guard that stops messaging if spend climbs, and a terminate at the end.

    docker compose run --rm --no-deps web python -m scripts.probe_b
"""
from __future__ import annotations

import time

from src.config import get_settings
from src.devin.client import RealDevinClient
from src.devin.schemas import SessionCreateRequest

MAX_ACU = 3                 # hard cap passed to Devin (server-enforced)
ACU_SOFTSTOP = 2.6          # stop messaging/polling if we get near the cap
POLL_EVERY = 12             # seconds
IDLE_DETAIL = {"waiting_for_user", "finished", "blocked", "waiting_for_approval"}
IDLE_STATUS = {"suspended", "exit"}
RESUMED_STATUS = {"running", "resuming", "claimed", "new"}


def _line(s, tag=""):
    print(f"  [{tag:^9}] status={s.status!r:<12} detail={(s.status_detail or '')!r:<22} acus={s.acus_consumed}")


def main() -> None:
    settings = get_settings()
    if not settings.devin_api_key.startswith("cog_"):
        raise SystemExit("No cog_ key in env; cannot run Probe B.")
    client = RealDevinClient(api_key=settings.devin_api_key, org_base=settings.org_base)

    print(f"Probe B, REAL session, max_acu_limit={MAX_ACU}. Creating throwaway session…")
    req = SessionCreateRequest(
        prompt=(
            "Connectivity probe for an automation harness. Reply with exactly the single word "
            "READY, then STOP and wait for my next instruction. Do not edit any code, do not open "
            "a pull request, do not run long-running tasks."
        ),
        max_acu_limit=MAX_ACU,
        tags=["probe-b", "helmsman-throwaway"],
        title="Helmsman Probe B (throwaway)",
        structured_output_required=False,
        resumable=True,
    )
    created = client.create_session(req)
    sid = created.session_id
    print(f"created session_id={sid}  url={created.url}")

    verdict = "INCONCLUSIVE"
    try:
        # 1) wait until the session is idle (waiting_for_user / finished / suspended / exit)
        print("phase 1: poll until idle")
        last = created
        for i in range(10):
            time.sleep(POLL_EVERY)
            last = client.get_session(sid)
            _line(last, f"poll {i}")
            if last.acus_consumed >= ACU_SOFTSTOP:
                print("  ACU soft-stop reached; proceeding to message step")
                break
            if last.status in IDLE_STATUS or (last.status_detail in IDLE_DETAIL):
                print("  session is idle")
                break

        acus_before = last.acus_consumed
        status_before = last.status

        # 2) message the idle session and watch for resume
        if last.acus_consumed >= MAX_ACU:
            verdict = "ACU_CAP_HIT_BEFORE_MESSAGE"
        else:
            print("phase 2: send a follow-up message and watch for resume")
            resp = client.message_session(sid, "Good. Now reply with exactly the single word RESUMED, then stop.")
            _line(resp, "msg-resp")
            resumed = resp.status in RESUMED_STATUS or resp.status_detail == "working"
            for i in range(6):
                time.sleep(POLL_EVERY)
                s = client.get_session(sid)
                _line(s, f"after {i}")
                if s.status in RESUMED_STATUS or s.status_detail == "working":
                    resumed = True
                if s.acus_consumed >= ACU_SOFTSTOP:
                    print("  ACU soft-stop reached; stopping poll")
                    break
                if resumed and (s.status in IDLE_STATUS or s.status_detail in IDLE_DETAIL):
                    break
            acus_after = client.get_session(sid).acus_consumed
            if resumed:
                verdict = f"RESUME CONFIRMED (status accepted/flipped; acus {acus_before}->{acus_after})"
            else:
                verdict = f"NO RESUME OBSERVED (fallback = new bounded session; acus {acus_before}->{acus_after})"
    finally:
        # 3) always terminate to stop further spend
        try:
            client.terminate_session(sid)
            print(f"terminated session {sid}")
        except Exception as exc:  # noqa: BLE001
            print(f"terminate failed (non-fatal): {exc}")
        final = None
        try:
            final = client.get_session(sid)
        except Exception:
            pass
        spent = final.acus_consumed if final else "unknown"
        print("\n================ PROBE B RESULT ================")
        print(f"verdict: {verdict}")
        print(f"acus_consumed (final): {spent}  (cap was {MAX_ACU})")
        print(f"session: {created.url}")
        print("===============================================")


if __name__ == "__main__":
    main()
