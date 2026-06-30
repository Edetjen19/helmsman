"""Wire the control-plane components from settings. Shared by the worker entrypoint and
the ingest app so both see the same store and config."""
from __future__ import annotations

from .config import Settings, get_settings
from .devin import build_devin_client
from .github import build_verifier
from .github.issues import build_issues
from .reconciler import BudgetGuard, Reconciler
from .store import Store


def build_store(settings: Settings | None = None) -> Store:
    settings = settings or get_settings()
    return Store(settings.db_path)


def build_reconciler(settings: Settings | None = None, store: Store | None = None) -> Reconciler:
    settings = settings or get_settings()
    store = store or build_store(settings)
    issues = build_issues(settings) if settings.enable_issue_sync else None
    return Reconciler(
        store=store,
        devin=build_devin_client(settings),
        verifier=build_verifier(settings),
        budget=BudgetGuard(global_budget=settings.global_acu_budget, max_acu_limit=settings.max_acu_limit),
        settings=settings,
        issues=issues,
    )
