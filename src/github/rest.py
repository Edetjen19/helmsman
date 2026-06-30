"""Token-based GitHub REST client (httpx). Works inside the worker container with no `gh`
binary, the verifier (PR -> head_sha -> check-runs) and the resync (list issues) both use it.

Auth is a GITHUB_TOKEN (a `gh auth token` value works). Reads are free; the only writes are
issue creation (scanner) and a human-initiated squash-merge (the approval gate), never an
auto-merge of agent code. Spends no Devin ACU.
"""
from __future__ import annotations

import random
import time
from typing import Any, Optional

import httpx

_RETRYABLE = {429, 500, 502, 503, 504}


class GitHubRest:
    def __init__(self, *, token: str, repo: str, api_url: str = "https://api.github.com", timeout: float = 30.0) -> None:
        if not token:
            raise ValueError("GitHubRest requires a token")
        self.repo = repo
        self._http = httpx.Client(
            base_url=api_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    @classmethod
    def from_settings(cls, settings) -> Optional["GitHubRest"]:  # type: ignore[no-untyped-def]
        token = getattr(settings, "github_token", "") or ""
        if not token:
            return None
        return cls(token=token, repo=settings.github_repo, api_url=getattr(settings, "github_api_url", "https://api.github.com"))

    def _request(self, method: str, path: str, *, json: Optional[dict] = None) -> Any:
        for attempt in range(5):
            resp = self._http.request(method, path, json=json)
            if resp.status_code in _RETRYABLE:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else (2 ** attempt) + random.uniform(0, 0.5)
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else None
        resp.raise_for_status()
        return None

    # ---- reads ---------------------------------------------------------------
    def get_pull_head_sha(self, number: int) -> str:
        data = self._request("GET", f"/repos/{self.repo}/pulls/{number}")
        return data["head"]["sha"]

    def get_check_runs(self, sha: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/repos/{self.repo}/commits/{sha}/check-runs")
        return data.get("check_runs", [])

    def get_pull_files(self, number: int) -> list[str]:
        data = self._request("GET", f"/repos/{self.repo}/pulls/{number}/files?per_page=100")
        return [f.get("filename", "") for f in data]

    def get_issue(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self.repo}/issues/{number}")

    def get_pull_state(self, number: int) -> dict[str, Any]:
        """Live PR state so the board never lies: {state: open|closed, merged: bool}."""
        d = self._request("GET", f"/repos/{self.repo}/pulls/{number}")
        return {"state": d.get("state"), "merged": bool(d.get("merged")), "merged_at": d.get("merged_at")}

    def list_labeled_issues(self, label: str) -> list[dict[str, Any]]:
        # The issues endpoint also returns PRs; the caller filters those out.
        data = self._request("GET", f"/repos/{self.repo}/issues?labels={label}&state=open&per_page=100")
        return [d for d in data if "pull_request" not in d]

    # ---- writes --------------------------------------------------------------
    def create_issue(self, *, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return self._request("POST", f"/repos/{self.repo}/issues", json={"title": title, "body": body, "labels": labels})

    def merge_pull(self, number: int, *, method: str = "squash", title: Optional[str] = None) -> dict[str, Any]:
        """Human-initiated squash-merge (the approval gate). Never called automatically."""
        body: dict[str, Any] = {"merge_method": method}
        if title:
            body["commit_title"] = title
        return self._request("PUT", f"/repos/{self.repo}/pulls/{number}/merge", json=body)

    def close(self) -> None:
        self._http.close()
