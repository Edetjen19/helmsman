"""Shared fixtures. Every test runs with SIMULATE forced on and `.env` ignored
(`_env_file=None`), so no test can read the real key or touch the real Devin API."""
from __future__ import annotations

import pytest

from src.bootstrap import build_reconciler
from src.config import Settings
from src.scanner.portfolio import PORTFOLIO, spec_hash
from src.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(
        simulate=True,
        webhook_secret="testsecret",
        db_path=str(tmp_path / "test.db"),
        global_acu_budget=500,   # high so all 6 portfolio items dispatch at once in the e2e
        max_acu_limit=10,
        devin_api_key="",
        github_token="",
        github_repo="Edetjen19/superset",
        autoload_real_results=False,
        _env_file=None,
    )


@pytest.fixture
def store(settings):
    return Store(settings.db_path)


@pytest.fixture
def reconciler(settings, store):
    return build_reconciler(settings, store)


def seed_portfolio(store: Store) -> None:
    for issue in PORTFOLIO:
        store.get_or_create_remediation(
            issue_id=issue.key,
            spec_hash=spec_hash(issue.body),
            issue_number=issue.issue_number,
            issue_title=issue.title,
            issue_url=issue.html_url,
            klass=issue.klass,
            sim_outcome=issue.sim_outcome,
        )


def run_ticks(reconciler, n: int) -> None:
    for _ in range(n):
        reconciler.tick()
