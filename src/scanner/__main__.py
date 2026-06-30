"""Scanner CLI. `python -m src.scanner` (dry-run) | `--create` (open issues on the fork).

Dry-run by default: it prints findings and touches nothing. `--create` ensures the labels
and opens one labeled issue per finding, idempotently (skips titles that already exist open).
"""
from __future__ import annotations

import argparse
import json
import os

from ..config import get_settings
from ..github.issues import GhIssues
from .scan import DEFAULT_CLONE, open_issues, scan


def main() -> None:
    settings = get_settings()
    p = argparse.ArgumentParser(description="Helmsman policy/deprecation scanner")
    p.add_argument("--root", default=os.environ.get("SUPERSET_CLONE", settings.superset_clone or DEFAULT_CLONE))
    p.add_argument("--repo", default=settings.github_repo)
    p.add_argument("--create", action="store_true", help="open labeled issues via gh (needs gh in this env)")
    p.add_argument("--emit-json", metavar="PATH",
                   help="write findings as JSON for a host-side gh creator (no gh needed here)")
    args = p.parse_args()

    findings = scan(args.root)
    print(f"\nScanned {args.root}")
    print(f"{'CLASS':<22} {'COUNT':>6}  TITLE")
    print("-" * 88)
    for f in findings:
        print(f"{f.klass:<22} {f.count:>6}  {f.title}")
    print(f"\n{len(findings)} findings.\n")

    if args.emit_json:
        payload = [
            {"key": f.key, "title": f.title, "klass": f.klass, "count": f.count,
             "labels": ["devin-remediate", f.klass], "body": f.body}
            for f in findings
        ]
        with open(args.emit_json, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"Wrote {len(payload)} findings to {args.emit_json}")

    if args.create:
        results = open_issues(findings, GhIssues(args.repo))
        for f, url, created in results:
            print(f"  [{'created' if created else 'exists '}] {url}  ({f.title})")
    elif not args.emit_json:
        print("Dry-run. Pass --emit-json PATH (then run scripts/create_issues.sh) to open them on", args.repo)


if __name__ == "__main__":
    main()
