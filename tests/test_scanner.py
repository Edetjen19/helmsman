"""Scanner rules over a fixture tree + idempotent issue creation against a fake client."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.github.issues import Issue, IssuesClient
from src.scanner.scan import open_issues, scan


def _make_clone(root: Path) -> None:
    (root / "superset" / "commands" / "report").mkdir(parents=True)
    (root / "superset" / "migrations" / "versions").mkdir(parents=True)
    (root / "requirements").mkdir(parents=True)

    (root / "superset" / "commands" / "report" / "execute.py").write_text(
        "import datetime\n"
        "a = datetime.utcnow()\n"
        "b = datetime.utcnow()\n"
        "c = datetime.utcfromtimestamp(0)\n"
        "row = db.session.query(Model).get(1)\n"
    )
    # A Query.get() in migrations/versions must be EXCLUDED from the count.
    (root / "superset" / "migrations" / "versions" / "001_x.py").write_text(
        "row = session.query(Thing).get(2)\n"
    )
    (root / "requirements" / "base.in").write_text(
        "flask\napispec>=6.0.0,<6.7.0\nmarshmallow\n"
    )
    (root / "requirements" / "base.txt").write_text(
        "pandas==2.1.4\nnumpy==1.26.4\nFlask==2.3.3\nrequests==2.33.0\n"
    )


def test_scan_finds_expected(tmp_path):
    _make_clone(tmp_path)
    findings = {f.key: f for f in scan(tmp_path)}

    assert findings["datetime-utcnow"].count == 2
    assert findings["datetime-utcfromtimestamp"].count == 1
    # 1 production Query.get(); the migrations/versions one is excluded.
    assert findings["sqlalchemy-query-get"].count == 1
    assert findings["apispec-cap"].count == 1
    assert findings["eol-pins"].count == 3            # pandas + numpy + Flask
    assert findings["fe-lint-graduation"].klass == "lint-graduation"

    # Each finding carries a class label and a verification command in its body.
    for f in findings.values():
        assert f.klass in {"dependency-upgrade", "deprecation-migration", "lint-graduation"}
        assert "verification" in f.body.lower()


def test_scan_missing_clone_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan(tmp_path / "does-not-exist")


class FakeIssues(IssuesClient):
    def __init__(self):
        self.created: list[Issue] = []
        self.labels_ensured = False
        self._n = 7000

    def ensure_labels(self) -> None:
        self.labels_ensured = True

    def list_labeled(self, label: str) -> list[Issue]:
        return list(self.created)

    def create_issue(self, *, title, body, labels) -> Issue:
        self._n += 1
        iss = Issue(number=self._n, title=title, body=body, url=f"https://github.com/x/y/issues/{self._n}",
                    node_id=f"NODE_{self._n}", labels=labels)
        self.created.append(iss)
        return iss


def test_scan_and_open_trigger(tmp_path):
    """The scheduled-scan trigger composes scan + open_issues against an injected client."""
    from src.config import Settings
    from src.scanner.scan import scan_and_open

    _make_clone(tmp_path)
    settings = Settings(superset_clone=str(tmp_path), _env_file=None)
    fake = FakeIssues()
    results = scan_and_open(settings, fake)
    assert results and all(created for _, _, created in results)
    assert len(fake.created) == len(scan(tmp_path))
    # Idempotent: a second scheduled scan opens nothing new.
    again = scan_and_open(settings, fake)
    assert all(not created for _, _, created in again)


def test_open_issues_is_idempotent(tmp_path):
    _make_clone(tmp_path)
    findings = scan(tmp_path)
    fake = FakeIssues()

    first = open_issues(findings, fake)
    assert fake.labels_ensured is True
    assert all(created for _, _, created in first)
    assert len(fake.created) == len(findings)

    # Second run: every title already exists, so nothing new is created.
    second = open_issues(findings, fake)
    assert all(not created for _, _, created in second)
    assert len(fake.created) == len(findings)
