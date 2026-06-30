"""Create the per-class Helmsman playbooks on the real Devin org. GATED, run manually.

This is a REAL Devin API write (no ACU, but it mutates the org). It refuses to run unless
SIMULATE=false and a cog_ key is present. The SIMULATE loop does not need playbooks (the same
guidance is carried inline in the prompt), so this is only for the real-run track.

    SIMULATE=false python -m scripts.create_playbooks         # prints what it would create
    SIMULATE=false python -m scripts.create_playbooks --apply # actually creates them
"""
from __future__ import annotations

import argparse

from src.config import get_settings
from src.devin.client import RealDevinClient
from src.devin.playbooks import PLAYBOOKS


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="actually create the playbooks (default: preview)")
    args = p.parse_args()

    settings = get_settings()
    if settings.simulate:
        raise SystemExit("Refusing to create playbooks while SIMULATE=true. Set SIMULATE=false first.")
    if not settings.devin_api_key.startswith("cog_"):
        raise SystemExit("No cog_ key present; cannot create playbooks.")

    for pb in PLAYBOOKS:
        print(f"- {pb.name}  (class={pb.klass}, {len(pb.instructions)} chars)")
    if not args.apply:
        print("\nPreview only. Re-run with --apply to create these on the Devin org.")
        return

    client = RealDevinClient(api_key=settings.devin_api_key, org_base=settings.org_base)
    for pb in PLAYBOOKS:
        obj = client.create_playbook(name=pb.name, instructions=pb.instructions)
        print(f"created: {pb.name} -> {obj.get('playbook_id') or obj.get('id') or obj}")


if __name__ == "__main__":
    main()
