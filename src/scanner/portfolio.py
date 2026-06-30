"""The verified issue portfolio (DESIGN.md §2) as data.

In M2 the scanner greps the read-only Superset clone and opens these as real labeled GitHub
issues on the fork. For the M1 SIMULATE skeleton, the dev-seed endpoint injects them straight
as queued remediations so the fleet board has realistic, diverse work to drive.

`sim_outcome` only steers the SIMULATE clients (it picks which FSM path each issue walks):
the apispec hero shows the red->self-heal->green loop; the high-risk EOL bump shows a refusal;
the rest go straight green. On a real run this field is ignored.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class PortfolioIssue:
    key: str                # stable id -> dedupe (issue_id)
    issue_number: int       # synthetic number for the SIMULATE board
    title: str
    klass: str              # dependency-upgrade | deprecation-migration | lint-graduation
    evidence: str           # file:line evidence (the spec_hash input)
    verification: str       # the cheapest no-boot verification command
    sim_outcome: str = "green"

    @property
    def body(self) -> str:
        return (
            f"## Evidence\n{self.evidence}\n\n"
            f"## Cheapest verification (no app boot)\n```\n{self.verification}\n```\n\n"
            f"Labels: `devin-remediate`, `{self.klass}`"
        )

    @property
    def html_url(self) -> str:
        return f"https://github.com/Edetjen19/superset/issues/{self.issue_number}"


def spec_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


PORTFOLIO: list[PortfolioIssue] = [
    PortfolioIssue(
        key="portfolio-apispec-cap",
        issue_number=5,  # real fork issue #5 (apispec); the real green PR is #8
        title="apispec cap blocks upgrade (raise cap + fix OpenAPI spec assertion)",
        klass="dependency-upgrade",
        evidence="requirements/base.in:45 → `apispec>=6.0.0,<6.7.0`, resolves apispec==6.6.1 (base.txt:11).",
        verification='pytest tests/unit_tests -k "openapi or apispec or swagger"',
        sim_outcome="heal",  # hero: red CI -> self-heal -> green
    ),
    PortfolioIssue(
        key="portfolio-datetime-utcnow",
        issue_number=2,  # real fork issue #2
        title="datetime.utcnow() deprecated across 27 sites (preserve naive/aware semantics)",
        klass="deprecation-migration",
        evidence="27 occurrences; 21 in superset/commands/report/execute.py, plus utils/cache.py, utils/dates.py, daos/log.py.",
        verification="ruff check && pytest tests/unit_tests/commands/report tests/unit_tests/utils",
    ),
    PortfolioIssue(
        key="portfolio-datetime-utcfromtimestamp",
        issue_number=3,  # real fork issue #3
        title="datetime.utcfromtimestamp() deprecated, 2 sites",
        klass="deprecation-migration",
        evidence="superset/models/helpers.py:2605, superset/daos/query.py:51.",
        verification="ruff check && pytest -k helpers",
    ),
    PortfolioIssue(
        key="portfolio-sqlalchemy-query-get",
        issue_number=4,  # real fork issue #4
        title="SQLAlchemy 1.4 legacy Query.get() in 7 production sites",
        klass="deprecation-migration",
        evidence="security/manager.py:2427, cli/export_example.py:158, commands/dataset/duplicate.py:58, … (exclude migrations/versions).",
        verification="ruff check && pytest -k duplicate",
    ),
    PortfolioIssue(
        key="portfolio-eol-pins",
        issue_number=6,  # real fork issue #6
        title="EOL pins: pandas 2.1.4 / numpy 1.26.4 / Flask 2.3.3",
        klass="dependency-upgrade",
        evidence="pandas==2.1.4 (EOL), numpy==1.26.4 (bound allows 2.x), Flask==2.3.3 (EOL). pandas/numpy = high breakage risk.",
        verification="recompile && pytest tests/unit_tests",
        sim_outcome="refuse",  # high-risk: Devin declines to auto-merge blind
    ),
    PortfolioIssue(
        key="portfolio-fe-lint-graduation",
        issue_number=7,  # real fork issue #7
        title="Frontend lint graduation: import/no-duplicates warn→error (31 auto-fixable)",
        klass="lint-graduation",
        evidence="superset-frontend: import/no-duplicates warn→error (31 auto-fixable); oxlint react plugin pinned 17.0.2 vs React 18.",
        verification="cd superset-frontend && npx oxlint --config oxlint.json",
    ),
]
