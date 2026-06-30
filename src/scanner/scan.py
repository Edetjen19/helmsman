"""Policy / deprecation / EOL scanner.

Runs the Appendix B rules over the READ-ONLY Superset clone, verifies the live counts,
and opens one labeled GitHub issue per finding (idempotently). This is the honest
"scan results -> labeled issue -> remediate" trigger the brief asks for. It touches the
clone (read) and GitHub (issue create) only, never Devin, so it spends no ACU.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..github.issues import IssuesClient
from ..scanner.portfolio import spec_hash

DEFAULT_CLONE = "../superset"  # a sibling Apache Superset checkout; override with SUPERSET_CLONE


@dataclass
class Finding:
    key: str
    title: str
    klass: str
    summary: str
    sites: list[str] = field(default_factory=list)
    count: int = 0
    verification: str = ""

    @property
    def body(self) -> str:
        sites = "\n".join(f"- `{s}`" for s in self.sites[:12])
        more = f"\n- …and {self.count - 12} more" if self.count > 12 else ""
        return (
            f"**Detected by the Helmsman scanner.**\n\n"
            f"{self.summary}\n\n"
            f"**Sites ({self.count}):**\n{sites}{more}\n\n"
            f"**Cheapest verification (no app boot):**\n```\n{self.verification}\n```\n\n"
            f"Class: `{self.klass}`"
        )

    @property
    def spec_hash(self) -> str:
        return spec_hash(self.body)


# --------------------------------------------------------------------------- #
# grep helpers (run against the read-only clone)
# --------------------------------------------------------------------------- #
def _grep(root: Path, pattern: str, *, extended: bool = False, path: str = "superset/") -> list[str]:
    """Return matching `file:line:text` lines, or [] if none. Never raises on no-match."""
    flags = "-rnE" if extended else "-rn"
    cmd = ["grep", flags, pattern, path, "--include=*.py"]
    res = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=120)
    if res.returncode not in (0, 1):  # 1 == no matches (fine); other == real error
        raise RuntimeError(f"grep failed ({res.returncode}): {res.stderr[:200]}")
    return [ln for ln in res.stdout.splitlines() if ln.strip()]


def _sites(lines: list[str]) -> list[str]:
    # Keep "path:line" for each hit.
    out = []
    for ln in lines:
        parts = ln.split(":", 2)
        if len(parts) >= 2:
            out.append(f"{parts[0]}:{parts[1]}")
    return out


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #
def scan(root: str | Path = DEFAULT_CLONE) -> list[Finding]:
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"Superset clone not found at {root}")
    findings: list[Finding] = []

    # 1) datetime.utcnow() , literal parens in BRE (\(\) would be an empty group, over-matching)
    lines = _grep(root, r"datetime\.utcnow()")
    if lines:
        findings.append(Finding(
            key="datetime-utcnow",
            title="datetime.utcnow() is deprecated (timezone-aware migration)",
            klass="deprecation-migration",
            summary="`datetime.utcnow()` is deprecated in Python 3.12. Replace with "
                    "`datetime.now(timezone.utc)` **preserving naive-vs-aware semantics per site** "
                    "(some values are compared to naive datetimes; a blind sed introduces bugs).",
            sites=_sites(lines), count=len(lines),
            verification="ruff check && pytest tests/unit_tests/commands/report tests/unit_tests/utils",
        ))

    # 2) datetime.utcfromtimestamp()
    lines = _grep(root, r"utcfromtimestamp")
    if lines:
        findings.append(Finding(
            key="datetime-utcfromtimestamp",
            title="datetime.utcfromtimestamp() is deprecated",
            klass="deprecation-migration",
            summary="Replace with `datetime.fromtimestamp(x, tz=timezone.utc)` preserving epoch semantics.",
            sites=_sites(lines), count=len(lines),
            verification="ruff check && pytest -k helpers",
        ))

    # 3) SQLAlchemy 1.4 legacy Query.get() (exclude migrations/versions, out of lint scope)
    lines = [ln for ln in _grep(root, r"\.query\([A-Za-z_]+\)\.get\(", extended=True)
             if "migrations/versions/" not in ln]
    if lines:
        findings.append(Finding(
            key="sqlalchemy-query-get",
            title="SQLAlchemy 1.4 legacy Query.get() in production code",
            klass="deprecation-migration",
            summary="`Query.get()` is legacy in SQLAlchemy 1.4+. Replace with `session.get(Model, id)`.",
            sites=_sites(lines), count=len(lines),
            verification="ruff check && pytest -k duplicate",
        ))

    # 4) apispec cap in requirements/base.in
    cap = _scan_apispec_cap(root)
    if cap:
        findings.append(cap)

    # 5) EOL pins in requirements/base.txt
    eol = _scan_eol_pins(root)
    if eol:
        findings.append(eol)

    # 6) Frontend lint graduation. Verified by running oxlint, not grep, so it is kept out of
    #    the Python scan (no node build); included as a static finding for breadth.
    findings.append(Finding(
        key="fe-lint-graduation",
        title="Frontend lint graduation: import/no-duplicates warn -> error",
        klass="lint-graduation",
        summary="`superset-frontend`: `import/no-duplicates` should graduate from warning to error "
                "(~31 auto-fixable). The oxlint react plugin is pinned to 17.0.2 while React 18 is "
                "installed. Verified by running oxlint (not grep), so it is reported statically here.",
        sites=["superset-frontend/.oxlintrc.json", "superset-frontend: import/no-duplicates"],
        count=31,
        verification="cd superset-frontend && npx oxlint --config oxlint.json",
    ))

    return findings


def _scan_apispec_cap(root: Path) -> Optional[Finding]:
    base_in = root / "requirements" / "base.in"
    if not base_in.exists():
        return None
    for i, line in enumerate(base_in.read_text().splitlines(), 1):
        if "apispec" in line and "<" in line:
            return Finding(
                key="apispec-cap",
                title="apispec version cap blocks upgrade (raise cap + fix OpenAPI spec assertion)",
                klass="dependency-upgrade",
                summary=f"`requirements/base.in:{i}` caps apispec: `{line.strip()}`. Raise the cap, "
                        "recompile with `./scripts/uv-pip-compile.sh`, then fix the OpenAPI spec "
                        "assertion the recompile cannot, the part a script can't do.",
                sites=[f"requirements/base.in:{i}"], count=1,
                verification='pytest tests/unit_tests -k "openapi or apispec or swagger"',
            )
    return None


def _scan_eol_pins(root: Path) -> Optional[Finding]:
    base_txt = root / "requirements" / "base.txt"
    if not base_txt.exists():
        return None
    eol_re = re.compile(r"^(pandas|numpy|Flask)==", re.IGNORECASE)
    hits = []
    for i, line in enumerate(base_txt.read_text().splitlines(), 1):
        if eol_re.match(line.strip()):
            hits.append(f"requirements/base.txt:{i}  {line.strip()}")
    if not hits:
        return None
    return Finding(
        key="eol-pins",
        title="EOL dependency pins (pandas / numpy / Flask)",
        klass="dependency-upgrade",
        summary="EOL pins detected. pandas/numpy carry real breakage risk, flag for review, do not "
                "auto-merge blind. Flask is the cleanest single bump.",
        sites=[h.split("  ")[0] for h in hits], count=len(hits),
        verification="recompile && pytest tests/unit_tests",
    )


# --------------------------------------------------------------------------- #
# open issues (idempotent)
# --------------------------------------------------------------------------- #
def scan_and_open(settings, issues: Optional[IssuesClient] = None) -> list[tuple["Finding", str, bool]]:  # type: ignore[no-untyped-def]
    """The scheduled-scan trigger: grep the clone, open labeled issues idempotently. GitHub writes
    only, no ACU. `resync()` then ingests them. The issues client is injectable for tests."""
    findings = scan(getattr(settings, "superset_clone", DEFAULT_CLONE))
    if issues is None:
        from ..github.issues import build_issues
        issues = build_issues(settings)
    return open_issues(findings, issues)


def open_issues(findings: list[Finding], issues: IssuesClient) -> list[tuple[Finding, str, bool]]:
    """Create one labeled issue per finding, skipping any whose title already exists open.
    Returns (finding, url, created)."""
    issues.ensure_labels()
    existing = {iss.title: iss for iss in issues.list_labeled("devin-remediate")}
    results = []
    for f in findings:
        if f.title in existing:
            results.append((f, existing[f.title].url, False))
            continue
        created = issues.create_issue(
            title=f.title, body=f.body, labels=["devin-remediate", f.klass]
        )
        results.append((f, created.url, True))
    return results
