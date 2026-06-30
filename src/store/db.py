"""SQLite store. One file, two processes (web + worker) share it via WAL mode.

A fresh connection is opened per operation (sqlite connect is cheap) so the store is
safe to call from FastAPI's threadpool and the worker loop without sharing handles.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import FsmState, TERMINAL_STATES, now_iso

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # For :memory: we must keep one shared connection alive (each connect() would
        # otherwise get a separate empty database).
        self._mem_conn: Optional[sqlite3.Connection] = None
        if db_path == ":memory:":
            self._mem_conn = self._new_connection()
        self.init_db()

    # ---- connection plumbing -------------------------------------------------
    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._mem_conn is not None:
            yield self._mem_conn
            self._mem_conn.commit()
            return
        conn = self._new_connection()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Idempotent column adds for databases created before a column existed."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(remediations)")}
        if "last_healed_sha" not in cols:
            conn.execute("ALTER TABLE remediations ADD COLUMN last_healed_sha TEXT")
        if "note" not in cols:
            conn.execute("ALTER TABLE remediations ADD COLUMN note TEXT")

    def is_empty(self) -> bool:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM remediations").fetchone()["c"] == 0

    def load_real_results(self, payload: dict[str, Any]) -> int:
        """Replace the store's contents with a committed real-results snapshot (the dashboard's
        data source). Clears the four tables then inserts remediations + their sessions + events +
        burn-down snapshots with their REAL recorded timestamps. Returns the remediation count."""
        rems = payload.get("remediations", [])
        snaps = payload.get("metric_snapshots", [])
        with self._conn() as conn:
            for t in ("events", "sessions", "metrics_snapshots", "remediations"):
                conn.execute(f"DELETE FROM {t}")
            for r in rems:
                ts = r.get("created_at") or now_iso()
                cur = conn.execute(
                    """INSERT INTO remediations
                         (issue_number, issue_id, spec_hash, issue_title, issue_url, klass, fsm_state,
                          pr_url, pr_number, pr_state, head_sha, heal_attempts, refusal_reason,
                          last_error, note, labeled_at, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r.get("issue_number"), r["issue_id"], r["spec_hash"], r.get("issue_title", ""),
                     r.get("issue_url", ""), r.get("klass", ""), r["fsm_state"], r.get("pr_url"),
                     r.get("pr_number"), r.get("pr_state"), r.get("head_sha"), r.get("heal_attempts", 0),
                     r.get("refusal_reason", ""), r.get("last_error", ""), r.get("note", ""),
                     r.get("labeled_at") or ts, ts, r.get("updated_at") or ts),
                )
                rid = cur.lastrowid
                for s in r.get("sessions", []):
                    sts = s.get("created_at") or ts
                    conn.execute(
                        """INSERT INTO sessions
                             (session_id, remediation_id, kind, status, status_detail, acus_consumed,
                              pr_url, pr_state, structured_output, session_url, is_active, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (s["session_id"], rid, s.get("kind", "remediate"), s.get("status", ""),
                         s.get("status_detail", ""), s.get("acus_consumed", 0.0), s.get("pr_url"),
                         s.get("pr_state"), s.get("structured_output"), s.get("session_url", ""),
                         s.get("is_active", 0), sts, s.get("updated_at") or sts),
                    )
                for e in r.get("events", []):
                    conn.execute(
                        "INSERT INTO events (remediation_id, session_id, type, detail, created_at) VALUES (?,?,?,?,?)",
                        (rid, e.get("session_id"), e["type"], e.get("detail", ""), e.get("created_at") or ts),
                    )
            for sn in snaps:
                conn.execute(
                    """INSERT INTO metrics_snapshots (ts, open_count, merged_count, in_flight, failed_count, acus_total)
                       VALUES (?,?,?,?,?,?)""",
                    (sn.get("ts") or now_iso(), sn.get("open_count", 0), sn.get("merged_count", 0),
                     sn.get("in_flight", 0), sn.get("failed_count", 0), sn.get("acus_total", 0.0)),
                )
        return len(rems)

    # ---- remediations --------------------------------------------------------
    def get_or_create_remediation(
        self,
        *,
        issue_id: str,
        spec_hash: str,
        issue_number: Optional[int] = None,
        issue_title: str = "",
        issue_url: str = "",
        klass: str = "",
        labeled_at: Optional[str] = None,
        sim_outcome: str = "green",
    ) -> tuple[dict[str, Any], bool]:
        """Idempotent on (issue_id, spec_hash). Returns (row, created)."""
        ts = now_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO remediations
                    (issue_number, issue_id, spec_hash, issue_title, issue_url, klass,
                     fsm_state, sim_outcome, labeled_at, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (issue_id, spec_hash) DO NOTHING
                """,
                (
                    issue_number, issue_id, spec_hash, issue_title, issue_url, klass,
                    FsmState.QUEUED.value, sim_outcome, labeled_at or ts, ts, ts,
                ),
            )
            created = cur.rowcount > 0
            row = conn.execute(
                "SELECT * FROM remediations WHERE issue_id=? AND spec_hash=?",
                (issue_id, spec_hash),
            ).fetchone()
        return dict(row), created

    def get_remediation(self, rid: int) -> Optional[dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM remediations WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None

    def list_remediations(self, states: Optional[list[FsmState]] = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM remediations"
        params: tuple = ()
        if states:
            placeholders = ",".join("?" for _ in states)
            q += f" WHERE fsm_state IN ({placeholders})"
            params = tuple(s.value for s in states)
        q += " ORDER BY id"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params).fetchall()]

    def active_remediations(self) -> list[dict[str, Any]]:
        """Non-terminal, non-parked work the reconciler should advance this tick."""
        terminal = TERMINAL_STATES | {FsmState.AWAITING_MERGE}
        q = "SELECT * FROM remediations WHERE fsm_state NOT IN ({}) ORDER BY id".format(
            ",".join("?" for _ in terminal)
        )
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, tuple(s.value for s in terminal)).fetchall()]

    def update_remediation(self, rid: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as conn:
            conn.execute(f"UPDATE remediations SET {sets} WHERE id=?", (*fields.values(), rid))

    # ---- sessions ------------------------------------------------------------
    def create_session(
        self,
        *,
        session_id: str,
        remediation_id: int,
        kind: str = "remediate",
        session_url: str = "",
        status: str = "new",
    ) -> dict[str, Any]:
        ts = now_iso()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO sessions
                       (session_id, remediation_id, kind, status, session_url, is_active, created_at, updated_at)
                   VALUES (?,?,?,?,?,1,?,?)""",
                (session_id, remediation_id, kind, status, session_url, ts, ts),
            )
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return dict(row)

    def update_session(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as conn:
            conn.execute(f"UPDATE sessions SET {sets} WHERE session_id=?", (*fields.values(), session_id))

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def active_session_for(self, remediation_id: int) -> Optional[dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE remediation_id=? AND is_active=1 ORDER BY id DESC LIMIT 1",
                (remediation_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_active_sessions(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM sessions WHERE is_active=1").fetchall()]

    def sessions_for(self, remediation_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM sessions WHERE remediation_id=? ORDER BY id", (remediation_id,)
                ).fetchall()
            ]

    # ---- events --------------------------------------------------------------
    def add_event(
        self,
        type: str,
        *,
        remediation_id: Optional[int] = None,
        session_id: Optional[str] = None,
        detail: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events (remediation_id, session_id, type, detail, created_at) VALUES (?,?,?,?,?)",
                (remediation_id, session_id, type, detail, now_iso()),
            )

    def last_event(self, remediation_id: int, type: str) -> Optional[dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE remediation_id=? AND type=? ORDER BY id DESC LIMIT 1",
                (remediation_id, type),
            ).fetchone()
        return dict(row) if row else None

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def events_by_type(self, type: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM events WHERE type=? ORDER BY id", (type,)
                ).fetchall()
            ]

    # ---- metrics -------------------------------------------------------------
    def add_metric_snapshot(
        self, *, open_count: int, merged_count: int, in_flight: int, failed_count: int, acus_total: float
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO metrics_snapshots
                       (ts, open_count, merged_count, in_flight, failed_count, acus_total)
                   VALUES (?,?,?,?,?,?)""",
                (now_iso(), open_count, merged_count, in_flight, failed_count, acus_total),
            )

    def metric_snapshots(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM metrics_snapshots ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def total_acus(self) -> float:
        with self._conn() as conn:
            row = conn.execute("SELECT COALESCE(SUM(acus_consumed),0) AS t FROM sessions").fetchone()
        return float(row["t"])
