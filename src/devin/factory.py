"""Single chokepoint for picking the real vs. simulated Devin client.

Guardrail: the real client is only ever built when SIMULATE is explicitly false AND a
cog_ key is present. Everything else (tests, dev loops, demo b-roll) gets SIMULATE.
"""
from __future__ import annotations

from ..config import Settings
from .client import DevinClient, RealDevinClient
from .simulate import SimulatedDevinClient


def build_devin_client(settings: Settings) -> DevinClient:
    if settings.simulate:
        return SimulatedDevinClient()
    if not settings.devin_api_key.startswith("cog_"):
        raise RuntimeError(
            "SIMULATE=false but no cog_ key present, refusing to build a real Devin client."
        )
    return RealDevinClient(api_key=settings.devin_api_key, org_base=settings.org_base)
