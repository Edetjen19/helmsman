"""GitHub issues access for the scanner (create) and the reconciler resync (list).

Behind an interface so tests inject a fake and never shell out. The real impl uses the
`gh` CLI (authed as the fork owner). These are GitHub calls only, never Devin, so they
spend no ACU and are safe outside the one rehearsed real run.
"""
from __future__ import annotations

import abc
import json
import subprocess
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class Issue:
    number: int
    title: str
    body: str
    url: str
    node_id: str
    labels: list[str]


# Labels the scanner manages on the fork: name -> (hex color, description).
LABELS: dict[str, tuple[str, str]] = {
    "devin-remediate": ("5319e7", "Autonomous remediation candidate (Helmsman)"),
    "dependency-upgrade": ("0e8a16", "Dependency cap / EOL upgrade"),
    "deprecation-migration": ("fbca04", "Deprecated-API migration"),
    "lint-graduation": ("1d76db", "Lint rule graduation (warn -> error)"),
}

REMEDIATE_LABEL = "devin-remediate"
CLASS_LABELS = [name for name in LABELS if name != REMEDIATE_LABEL]


class IssuesClient(abc.ABC):
    @abc.abstractmethod
    def ensure_labels(self) -> None: ...

    @abc.abstractmethod
    def list_labeled(self, label: str) -> list[Issue]: ...

    @abc.abstractmethod
    def create_issue(self, *, title: str, body: str, labels: list[str]) -> Issue: ...


class GhIssues(IssuesClient):
    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _gh(self, *args: str, capture: bool = True) -> str:
        out = subprocess.run(
            ["gh", *args], capture_output=capture, text=True, timeout=60, check=True
        )
        return out.stdout

    def ensure_labels(self) -> None:
        for name, (color, desc) in LABELS.items():
            # --force makes this idempotent (creates or updates, never errors on exists).
            self._gh(
                "label", "create", name, "--repo", self.repo,
                "--color", color, "--description", desc, "--force",
            )

    def list_labeled(self, label: str) -> list[Issue]:
        raw = self._gh(
            "issue", "list", "--repo", self.repo, "--label", label, "--state", "open",
            "--json", "number,title,body,url,id,labels", "--limit", "100",
        )
        data = json.loads(raw or "[]")
        return [_to_issue(d) for d in data]

    def create_issue(self, *, title: str, body: str, labels: list[str]) -> Issue:
        args = ["issue", "create", "--repo", self.repo, "--title", title, "--body", body]
        for lbl in labels:
            args += ["--label", lbl]
        url = self._gh(*args).strip().splitlines()[-1].strip()
        number = _number_from_url(url)
        # Fetch the node id so resync dedupe keys on a stable identifier.
        view = json.loads(self._gh("issue", "view", str(number), "--repo", self.repo, "--json", "id,number,url,title,body,labels"))
        return _to_issue(view)


class RestIssues(IssuesClient):
    """Issues access via the token GitHub REST client. Works in-container (no `gh` binary),
    so the worker's resync can run inside docker. Label management is a no-op here (labels are
    ensured once by the scanner's host run / GhIssues)."""

    def __init__(self, rest) -> None:  # type: ignore[no-untyped-def]
        self.rest = rest

    def ensure_labels(self) -> None:
        # REST label creation is possible but unnecessary for resync (read path); the scanner's
        # host run already ensures labels. Kept a no-op to avoid needless writes.
        return

    def list_labeled(self, label: str) -> list[Issue]:
        return [_to_issue_rest(d) for d in self.rest.list_labeled_issues(label)]

    def create_issue(self, *, title: str, body: str, labels: list[str]) -> Issue:
        return _to_issue_rest(self.rest.create_issue(title=title, body=body, labels=labels))


def _to_issue_rest(d: dict[str, Any]) -> Issue:
    return Issue(
        number=d["number"],
        title=d.get("title", "") or "",
        body=d.get("body", "") or "",
        url=d.get("html_url", "") or "",
        node_id=d.get("node_id", "") or f"gh-{d['number']}",
        labels=[l.get("name", "") for l in d.get("labels", []) if isinstance(l, dict)],
    )


def build_issues(settings):  # type: ignore[no-untyped-def]
    """Pick the issues client for resync: REST (in-container, token) preferred, gh CLI fallback."""
    from .rest import GitHubRest

    rest = GitHubRest.from_settings(settings)
    return RestIssues(rest) if rest is not None else GhIssues(settings.github_repo)


def _to_issue(d: dict[str, Any]) -> Issue:
    return Issue(
        number=d["number"],
        title=d.get("title", ""),
        body=d.get("body", "") or "",
        url=d.get("url", ""),
        node_id=d.get("id", "") or f"gh-{d['number']}",
        labels=[l["name"] for l in d.get("labels", []) if isinstance(l, dict)],
    )


def _number_from_url(url: str) -> int:
    return int(url.rstrip("/").split("/")[-1])
