"""Every Devin create must carry the structured-output schema (so the verdict is machine
readable) and the correlation tags (so the fleet board and list/filter work)."""
from __future__ import annotations

from src.bootstrap import build_reconciler
from src.devin.schemas import STRUCTURED_OUTPUT_SCHEMA
from src.devin.simulate import SimulatedDevinClient


class RecordingClient(SimulatedDevinClient):
    def __init__(self):
        super().__init__()
        self.last_create = None

    def create_session(self, req):
        self.last_create = req
        return super().create_session(req)


def test_create_carries_schema_and_tags(settings, store):
    reconciler = build_reconciler(settings, store)
    rec = RecordingClient()
    reconciler.devin = rec

    store.get_or_create_remediation(
        issue_id="c-1", spec_hash="h", issue_number=4242,
        klass="deprecation-migration", sim_outcome="green",
    )
    reconciler.tick()  # dispatch pass creates the session

    req = rec.last_create
    assert req is not None
    assert req.structured_output_schema == STRUCTURED_OUTPUT_SCHEMA
    assert req.structured_output_required is True
    assert req.max_acu_limit == settings.max_acu_limit
    assert req.repos == [settings.github_repo]
    assert "devin-remediate" in req.tags
    assert "issue-4242" in req.tags
    assert "class-deprecation-migration" in req.tags
    assert any(t.startswith("sim-outcome:") for t in req.tags)


def test_structured_output_schema_shape():
    assert STRUCTURED_OUTPUT_SCHEMA["type"] == "object"
    assert set(STRUCTURED_OUTPUT_SCHEMA["required"]) == {"remediated", "refused", "root_cause", "pr_url"}
