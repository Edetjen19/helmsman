"""Runtime configuration, loaded from the environment (.env in dev, compose env_file in docker).

Defaults are dev-safe: SIMULATE is on and no real key is required to import this module,
so tests and the SIMULATE skeleton run with zero risk of touching the real Devin API.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Devin v3 (cog_ service-user key) ---
    devin_api_key: str = ""
    devin_org_id: str = ""
    devin_base_url: str = "https://api.devin.ai/v3"

    # --- GitHub (the fork) ---
    github_token: str = ""
    github_repo: str = "Edetjen19/superset"
    github_api_url: str = "https://api.github.com"

    # --- Control plane ---
    webhook_secret: str = ""
    max_acu_limit: int = 10
    global_acu_budget: float = 50.0
    poll_interval_seconds: int = 20
    simulate: bool = True

    # --- Scanner / resync ---
    # Path to a read-only Apache Superset clone (live scanner only; unused in SIMULATE).
    # Set via SUPERSET_CLONE; defaults to a sibling checkout next to this repo.
    superset_clone: str = "../superset"
    # Level-triggered resync: pull open devin-remediate issues from the fork into the store.
    # GitHub reads only (no ACU); off by default so the SIMULATE seed demo needs no network.
    enable_issue_sync: bool = False
    issue_sync_interval_seconds: int = 60
    # Scheduled scan trigger: periodically grep the clone and open labeled issues (the
    # "scan results" event). 0 = off. Needs the clone mounted + a token. GitHub writes only, no ACU.
    scan_schedule_seconds: int = 0

    # On startup, if the store is empty, load the committed real-results snapshot so a fresh
    # `docker compose up` shows the real board with no creds. Tests disable this.
    autoload_real_results: bool = True

    # --- Local ---
    db_path: str = "data/helmsman.db"
    # Stated ACU->USD rate for the cost panel. Labeled as an assumption, never as measured.
    acu_usd_rate: float = 2.25
    max_heal_attempts: int = 2

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def org_base(self) -> str:
        """`/v3/organizations/{org_id}` prefix for org-scoped calls."""
        return f"{self.devin_base_url}/organizations/{self.devin_org_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
