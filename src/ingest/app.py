"""FastAPI app: the GitHub webhook (HMAC) + the ops dashboard + the human approval gate.

This is the `web` process. It only *enqueues* work and serves the console; the `worker`
process (the reconciler) does all dispatch/poll/heal. They share the SQLite store.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..bootstrap import build_store
from ..config import Settings, get_settings
from ..metrics import compute_metrics, fleet_rows, recent_activity
from ..store import FsmState, Store
from .security import verify_signature
from .webhook import parse_issue_labeled

log = structlog.get_logger("ingest")

_DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
_REAL_RESULTS = Path(__file__).resolve().parents[2] / "data" / "real_results.json"
_MAX_BODY = 200_000  # reject oversized webhook payloads


def _load_real_results(store: Store) -> int:
    """Load the committed real-results snapshot (the dashboard's data source) into the store."""
    import json

    if not _REAL_RESULTS.exists():
        log.warning("real_results_missing", path=str(_REAL_RESULTS))
        return 0
    return store.load_real_results(json.loads(_REAL_RESULTS.read_text()))


def _refresh_pr_states(settings: Settings, store: Store) -> None:
    """When a GITHUB_TOKEN is present, refresh each PR's live state so the board never lies:
    a merged PR shows `merged`, an open one shows `awaiting_merge` (at the gate). Credential-less,
    the committed snapshot is the fallback. The dashboard never merges; humans review on GitHub."""
    from ..github.rest import GitHubRest

    rest = GitHubRest.from_settings(settings)
    if rest is None:
        return
    pr_states = {FsmState.MERGED.value, FsmState.AWAITING_MERGE.value}
    for rem in store.list_remediations():
        n = rem.get("pr_number")
        if not n or rem["fsm_state"] not in pr_states:
            continue
        try:
            st = rest.get_pull_state(int(n))
        except Exception:  # noqa: BLE001, keep the snapshot fallback on any error
            continue
        target = FsmState.MERGED.value if st["merged"] else FsmState.AWAITING_MERGE.value
        if rem["fsm_state"] != target:
            store.update_remediation(rem["id"], fsm_state=target,
                                     pr_state=("merged" if st["merged"] else (st["state"] or "open")))


def create_app(settings: Optional[Settings] = None, store: Optional[Store] = None) -> FastAPI:
    settings = settings or get_settings()
    store = store or build_store(settings)
    # On a fresh clone the board shows REAL results from the last run, with no creds needed.
    if settings.autoload_real_results and store.is_empty():
        n = _load_real_results(store)
        if n:
            log.info("real_results_autoloaded", remediations=n)
    if settings.autoload_real_results:
        _refresh_pr_states(settings, store)  # reflect live PR state when a token is present
    templates = Jinja2Templates(directory=str(_DASHBOARD / "templates"))
    app = FastAPI(title="Helmsman", docs_url=None, redoc_url=None)
    static_dir = _DASHBOARD / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def _view(request: Request, template: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            template,
            {
                "metrics": compute_metrics(store, settings),
                "rows": fleet_rows(store),
                "events": recent_activity(store, 10),
                "settings": settings,
            },
        )

    # ---- dashboard -------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return _view(request, "index.html")

    @app.get("/partials/fleet", response_class=HTMLResponse)
    def fleet_partial(request: Request) -> HTMLResponse:
        return _view(request, "fleet.html")

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "simulate": settings.simulate})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        # Browsers auto-request /favicon.ico; serve the SVG mark so the tab icon shows
        # everywhere and the request never 404s.
        return FileResponse(str(_DASHBOARD / "static" / "favicon.svg"), media_type="image/svg+xml")

    # ---- webhook ---------------------------------------------------------
    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
        body = await request.body()
        if len(body) > _MAX_BODY:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        sig = request.headers.get("X-Hub-Signature-256")
        if not verify_signature(settings.webhook_secret, body, sig):
            log.warning("webhook_bad_signature")
            return JSONResponse({"error": "bad signature"}, status_code=401)

        import json as _json

        try:
            payload = _json.loads(body)
        except _json.JSONDecodeError:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        kwargs = parse_issue_labeled(payload)
        if kwargs is None:
            return JSONResponse({"ignored": True})

        rem, created = store.get_or_create_remediation(**kwargs)
        if created:
            store.add_event("queued", remediation_id=rem["id"], detail=f"issue #{rem['issue_number']}")
            log.info("enqueued", remediation=rem["id"], issue=rem["issue_number"])
        else:
            log.info("dedup_hit", remediation=rem["id"], issue=rem["issue_number"])
        return JSONResponse({"remediation_id": rem["id"], "created": created})

    # No merge endpoint: the dashboard never merges code. Each PR links out to GitHub, where a
    # human reviews and merges. The board reflects the PR's real state (see _refresh_pr_states).

    # ---- load the real-results snapshot (the board's data source) --------
    @app.post("/load-results")
    def load_results() -> Response:
        n = _load_real_results(store)
        _refresh_pr_states(settings, store)
        log.info("real_results_loaded", remediations=n)
        return RedirectResponse("/", status_code=303)

    return app


app = create_app()
