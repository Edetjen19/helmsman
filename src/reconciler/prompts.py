"""Per-class Devin prompts. In M1/SIMULATE these aren't executed (the sim client ignores
prompt text), but they are the real contract and get promoted to per-class playbooks in M2.

The framing keeps Devin owning the whole task, root-cause -> patch -> tests -> PR -> fix its
own CI, not acting as a git/PR wrapper. Banned-API traps (DESIGN.md §3) are spelled out so
generated fixes don't trip pre-commit.
"""
from __future__ import annotations

_GUARDRAILS = (
    "Constraints for this repo: use `superset.utils.json` (never `import json`/`simplejson`); "
    "avoid `make_url`; new files need the ASF Apache-2.0 license header (none of these fixes add files); "
    "after autofixes, COMMIT the result so pre-commit passes on a clean tree. "
    "Pin your Python to 3.11. Run the cheapest verification before opening the PR. "
    "Open the PR with a Conventional-Commits title and a SUMMARY / TESTING INSTRUCTIONS body, and link `Fixes #<issue>`."
)

_CLASS_BODY = {
    "dependency-upgrade": (
        "This is a dependency-cap / EOL upgrade. Raise the cap, recompile with "
        "`./scripts/uv-pip-compile.sh`, then fix whatever breaks (e.g. an OpenAPI spec "
        "assertion), the part a script cannot do. Verify with the targeted unit tests."
    ),
    "deprecation-migration": (
        "This is a deprecated-API migration. Replace each site preserving exact semantics "
        "(naive-vs-aware datetime, epoch handling), a blind sed would introduce bugs. "
        "Verify with `ruff check` plus the targeted unit tests."
    ),
    "lint-graduation": (
        "This is a lint graduation (warn -> error). Apply the autofixer, bump the plugin "
        "version if needed, and keep the diff small and clean."
    ),
}


def build_prompt(remediation: dict) -> str:
    klass = remediation.get("klass") or "deprecation-migration"
    body = _CLASS_BODY.get(klass, _CLASS_BODY["deprecation-migration"])
    title = remediation.get("issue_title") or "tech-debt remediation"
    issue_url = remediation.get("issue_url") or ""
    issue_n = remediation.get("issue_number")
    return (
        f"You are remediating a tech-debt issue on the `Edetjen19/superset` fork.\n"
        f"Issue #{issue_n}: {title}\n{issue_url}\n\n"
        f"{body}\n\n{_GUARDRAILS}\n\n"
        f"Emit the structured-output verdict when done."
    )
