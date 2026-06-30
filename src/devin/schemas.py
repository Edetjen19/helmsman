"""Devin v3 request/response models + success derivation (DESIGN.md §6).

Schemas are permissive (extra fields ignored) so a v3 response with more keys than we
model never breaks polling. Success is derived from structured_output + pull_requests,
NEVER from status alone, a finished session with no PR is a failure.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# The structured-output schema we pass on every create (Draft 7, self-contained, <=64KB).
STRUCTURED_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "remediated": {"type": "boolean"},
        "refused": {"type": "boolean"},
        "refusal_reason": {"type": "string"},
        "change_class": {"type": "string"},
        "root_cause": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests_added": {"type": "boolean"},
        "verification_ran": {"type": "string"},
        "pr_url": {"type": "string"},
        "residual_risk": {"type": "string"},
    },
    "required": ["remediated", "refused", "root_cause", "pr_url"],
}


class StructuredOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    remediated: bool = False
    refused: bool = False
    refusal_reason: str = ""
    change_class: str = ""
    root_cause: str = ""
    files_changed: list[str] = Field(default_factory=list)
    tests_added: bool = False
    verification_ran: str = ""
    pr_url: str = ""
    residual_risk: str = ""


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    pr_url: str = ""
    pr_state: Optional[str] = None  # open | merged | closed | null


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    prompt: str
    max_acu_limit: int
    tags: list[str] = Field(default_factory=list)
    title: str = ""
    repos: list[str] = Field(default_factory=list)
    playbook_id: Optional[str] = None
    knowledge_ids: list[str] = Field(default_factory=list)
    structured_output_schema: Optional[dict[str, Any]] = None
    structured_output_required: bool = True
    resumable: bool = True
    # devin_mode left default ('normal'); 'fast' is ~2x speed / 4x cost, never default to it.


class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    session_id: str = ""
    status: str = ""
    status_detail: Optional[str] = None
    structured_output: Optional[StructuredOutput] = None
    pull_requests: list[PullRequest] = Field(default_factory=list)
    acus_consumed: float = 0.0
    url: str = ""
    tags: list[str] = Field(default_factory=list)
    title: str = ""
    category: Optional[str] = None
    subcategory: Optional[str] = None

    @property
    def pr_url(self) -> Optional[str]:
        return self.pull_requests[0].pr_url if self.pull_requests else None

    @property
    def pr_state(self) -> Optional[str]:
        return self.pull_requests[0].pr_state if self.pull_requests else None


class Outcome(str, Enum):
    IN_FLIGHT = "in_flight"
    NEEDS_INPUT = "needs_input"
    SUCCESS = "success"
    REFUSED = "refused"
    FAILURE = "failure"


_TERMINAL_STATUS = {"exit"}
_FAIL_STATUS = {"error"}
_FAIL_DETAIL = {"usage_limit_exceeded", "out_of_credits", "error"}
_INFLIGHT_STATUS = {"new", "claimed", "running", "resuming"}


def derive_outcome(s: SessionResponse) -> Outcome:
    """Map a v3 SessionResponse to a control-plane outcome (DESIGN.md §6.2).

    Order matters: refusal and explicit success/failure are checked before the
    generic in-flight bucket, because a finished-but-PR-less session is a FAILURE.
    """
    so = s.structured_output
    detail = s.status_detail

    if so is not None and so.refused:
        return Outcome.REFUSED

    if s.status in _FAIL_STATUS or (detail in _FAIL_DETAIL):
        return Outcome.FAILURE

    finished = s.status in _TERMINAL_STATUS or detail == "finished"
    if finished:
        # Success requires the verdict AND a real PR. Never trust status alone.
        if so is not None and so.remediated and s.pr_url:
            return Outcome.SUCCESS
        return Outcome.FAILURE

    if detail in ("waiting_for_user", "waiting_for_approval"):
        return Outcome.NEEDS_INPUT

    if s.status in _INFLIGHT_STATUS:
        return Outcome.IN_FLIGHT

    # Suspended (inactivity etc.) but resumable, treat as in-flight; the reconciler
    # nudges it via a message if it has work to do.
    return Outcome.IN_FLIGHT
