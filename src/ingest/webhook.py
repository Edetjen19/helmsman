"""Normalize a GitHub `issues.labeled` webhook into remediation kwargs.

We only enqueue issues that carry the `devin-remediate` label. The class label
(dependency-upgrade | deprecation-migration | lint-graduation) sets `klass`. The dedupe key
is (issue node_id, spec_hash-of-body), so a re-delivered webhook collapses onto one row.
"""
from __future__ import annotations

from typing import Any, Optional

from ..scanner.portfolio import spec_hash
from ..store.models import now_iso

REMEDIATE_LABEL = "devin-remediate"
CLASS_LABELS = {"dependency-upgrade", "deprecation-migration", "lint-graduation"}


def parse_issue_labeled(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return remediation kwargs if this event should enqueue work, else None."""
    if payload.get("action") != "labeled":
        return None
    issue = payload.get("issue") or {}
    labels = {lbl.get("name") for lbl in issue.get("labels", []) if isinstance(lbl, dict)}
    if REMEDIATE_LABEL not in labels:
        return None

    klass = next((c for c in CLASS_LABELS if c in labels), "")
    body = issue.get("body") or ""
    issue_id = issue.get("node_id") or f"issue-{issue.get('number')}"

    return {
        "issue_id": issue_id,
        "spec_hash": spec_hash(body or str(issue.get("number"))),
        "issue_number": issue.get("number"),
        "issue_title": issue.get("title", ""),
        "issue_url": issue.get("html_url", ""),
        "klass": klass,
        "labeled_at": now_iso(),
    }
