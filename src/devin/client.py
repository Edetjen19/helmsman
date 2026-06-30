"""Devin client interface + the real v3 REST implementation.

The interface is what the reconciler talks to. Swapping SIMULATE for the real client
is a one-line factory change (build_devin_client). The real client is NEVER exercised
in tests or dev loops, guardrails live in the factory and in the reconciler's budget.
"""
from __future__ import annotations

import abc
import random
import time
from typing import Optional

import httpx

from .schemas import SessionCreateRequest, SessionResponse

_RETRYABLE = {429, 500, 502, 503, 504}


class DevinClient(abc.ABC):
    simulated: bool = False

    @abc.abstractmethod
    def create_session(self, req: SessionCreateRequest) -> SessionResponse: ...

    @abc.abstractmethod
    def get_session(self, session_id: str) -> SessionResponse: ...

    @abc.abstractmethod
    def message_session(self, session_id: str, message: str) -> SessionResponse:
        """Self-heal channel. Auto-resumes a suspended session in v3."""
        ...


class RealDevinClient(DevinClient):
    """Talks to https://api.devin.ai/v3. Only used for the rehearsed real runs."""

    simulated = False

    def __init__(self, *, api_key: str, org_base: str, timeout: float = 30.0) -> None:
        if not api_key.startswith("cog_"):
            raise ValueError("RealDevinClient requires a cog_ v3 service-user key")
        self._org_base = org_base.rstrip("/")
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    def _request(self, method: str, path: str, *, json: Optional[dict] = None) -> dict:
        url = f"{self._org_base}{path}"
        for attempt in range(5):
            resp = self._http.request(method, url, json=json)
            if resp.status_code == 422:
                # Permanent validation error, never retry, surface it.
                raise PermanentApiError(resp.status_code, resp.text)
            if resp.status_code in _RETRYABLE:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else (2 ** attempt) + random.uniform(0, 0.5)
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        raise PermanentApiError(resp.status_code, "exhausted retries")

    def create_session(self, req: SessionCreateRequest) -> SessionResponse:
        body = req.model_dump(exclude_none=True)
        data = self._request("POST", "/sessions", json=body)
        return SessionResponse.model_validate(data)

    def get_session(self, session_id: str) -> SessionResponse:
        data = self._request("GET", f"/sessions/{session_id}")
        return SessionResponse.model_validate(data)

    def message_session(self, session_id: str, message: str) -> SessionResponse:
        data = self._request("POST", f"/sessions/{session_id}/messages", json={"message": message})
        return SessionResponse.model_validate(data)

    def create_playbook(self, *, name: str, instructions: str) -> dict:
        """Create a reusable playbook. Real Devin org write (no ACU). Returns the API object."""
        return self._request("POST", "/playbooks", json={"name": name, "instructions": instructions})

    def terminate_session(self, session_id: str) -> None:
        """Terminate a session to stop any further spend (used to clean up probes)."""
        self._request("DELETE", f"/sessions/{session_id}")

    def close(self) -> None:
        self._http.close()


class PermanentApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Devin API {status}: {body[:500]}")
        self.status = status
