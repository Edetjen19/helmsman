"""Per-class Devin playbooks (DESIGN.md §6 / M2).

A playbook is reusable, named guidance attached to a session via `playbook_id`. We define one
per remediation class. Creating them on the real Devin org is a real API write (no ACU, but a
mutation), so it is GATED behind `scripts/create_playbooks.py` and never run automatically -
the SIMULATE loop carries the same guidance inline via the prompt (see reconciler/prompts.py).
"""
from __future__ import annotations

from dataclasses import dataclass

_GUARDRAILS = (
    "Repo guardrails: use `superset.utils.json` (never `import json`/`simplejson`); avoid `make_url`; "
    "new files need the ASF Apache-2.0 license header (these fixes add none); after autofixes, COMMIT "
    "the result so pre-commit passes on a clean tree. Pin Python to 3.11. PR title must match "
    "Conventional Commits; PR body needs SUMMARY and TESTING INSTRUCTIONS; link `Fixes #<issue>`. "
    "Own the whole task: root-cause, patch, tests, PR, and fix your own CI failures. Emit the "
    "structured-output verdict when done."
)


@dataclass(frozen=True)
class Playbook:
    klass: str
    name: str
    instructions: str


PLAYBOOKS: list[Playbook] = [
    Playbook(
        klass="dependency-upgrade",
        name="Helmsman: dependency-upgrade",
        instructions=(
            "Goal: lift a dependency cap / EOL pin and make the tree green.\n"
            "Steps: raise the cap in requirements/*.in, recompile with `./scripts/uv-pip-compile.sh`, "
            "then fix whatever breaks that the recompile cannot (commonly an OpenAPI spec assertion). "
            "For high-risk majors (pandas, numpy) do NOT bump blind: if the change is unsafe, decline and "
            "set refused=true with a reason. Verify with the issue's targeted unit tests before opening the PR.\n\n"
            + _GUARDRAILS
        ),
    ),
    Playbook(
        klass="deprecation-migration",
        name="Helmsman: deprecation-migration",
        instructions=(
            "Goal: replace a deprecated API across all sites without changing behavior.\n"
            "Replace each occurrence preserving exact semantics: `datetime.utcnow()` -> "
            "`datetime.now(timezone.utc)` ONLY where the value is timezone-aware; where it is compared to "
            "naive datetimes, keep it naive. `datetime.utcfromtimestamp(x)` -> "
            "`datetime.fromtimestamp(x, tz=timezone.utc)`. `Query.get()` -> `session.get(Model, id)`. "
            "A blind sed introduces aware/naive comparison bugs, that is the whole point of doing this with an agent. "
            "Verify with `ruff check` plus the targeted unit tests.\n\n"
            + _GUARDRAILS
        ),
    ),
    Playbook(
        klass="lint-graduation",
        name="Helmsman: lint-graduation",
        instructions=(
            "Goal: graduate a lint rule from warning to error with a clean, small diff.\n"
            "Apply the autofixer (e.g. oxlint --fix), bump the plugin version to match the installed major "
            "if needed, and keep the change minimal. Verify the linter is clean before opening the PR.\n\n"
            + _GUARDRAILS
        ),
    ),
]


def playbook_for(klass: str) -> Playbook | None:
    return next((p for p in PLAYBOOKS if p.klass == klass), None)
