"""Worker process: run the reconciler loop forever. `python -m src.reconciler`.

Backoff + jitter on unexpected errors so a transient failure never tight-loops. The loop is
level-triggered, so each tick re-reads state from the store, a crash/restart resumes cleanly.
"""
from __future__ import annotations

import random
import signal
import time

import structlog

from ..bootstrap import build_reconciler
from ..config import get_settings
from ..logging_setup import configure_logging

log = structlog.get_logger("worker")
_running = True


def _stop(*_args) -> None:
    global _running
    _running = False


def main() -> None:
    configure_logging()
    settings = get_settings()
    reconciler = build_reconciler(settings)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info(
        "worker_start",
        simulate=settings.simulate,
        poll_interval=settings.poll_interval_seconds,
        global_acu_budget=settings.global_acu_budget,
        repo=settings.github_repo,
        issue_sync=settings.enable_issue_sync,
    )
    # Scheduled scan (opens labeled issues) + resync (ingests them) on their own cadences.
    # GitHub only, no ACU. Both off by default; the SIMULATE seed demo needs neither.
    poll = max(1, settings.poll_interval_seconds)
    ticks_per_resync = max(1, settings.issue_sync_interval_seconds // poll)
    ticks_per_scan = max(1, settings.scan_schedule_seconds // poll) if settings.scan_schedule_seconds else 0
    tick_no = 0
    backoff = 0
    while _running:
        try:
            if ticks_per_scan and tick_no % ticks_per_scan == 0:
                from ..scanner.scan import scan_and_open

                opened = [r for r in scan_and_open(settings) if r[2]]
                if opened:
                    log.info("scheduled_scan", opened=len(opened))
            if settings.enable_issue_sync and tick_no % ticks_per_resync == 0:
                n = reconciler.resync()
                if n:
                    log.info("resync", new=n)
            reconciler.tick()
            tick_no += 1
            backoff = 0
        except Exception:  # noqa: BLE001, keep the worker alive; log and back off
            backoff = min(backoff + 1, 5)
            log.exception("tick_error", backoff=backoff)
        # Sleep in short slices so SIGTERM is honored promptly.
        delay = settings.poll_interval_seconds * (2 ** backoff if backoff else 1) + random.uniform(0, 0.5)
        waited = 0.0
        while _running and waited < delay:
            time.sleep(0.25)
            waited += 0.25
    log.info("worker_stop")


if __name__ == "__main__":
    main()
