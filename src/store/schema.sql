-- Helmsman store. Restart-safe: the reconciler reads desired/actual state from here
-- every tick, so nothing in-flight lives only in memory.

CREATE TABLE IF NOT EXISTS remediations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER,
    issue_id     TEXT NOT NULL,           -- GitHub node id (stable dedupe key)
    spec_hash    TEXT NOT NULL,           -- sha256 of the issue spec (body+evidence)
    issue_title  TEXT,
    issue_url    TEXT,
    klass        TEXT,                    -- dependency-upgrade | deprecation-migration | lint-graduation
    fsm_state    TEXT NOT NULL,
    pr_url       TEXT,
    pr_number    INTEGER,
    pr_state     TEXT,
    head_sha     TEXT,
    heal_attempts INTEGER NOT NULL DEFAULT 0,
    last_healed_sha TEXT,            -- PR head sha we last self-healed (per-commit cooldown)
    refusal_reason TEXT,
    last_error   TEXT,
    note         TEXT,                    -- free-form note (e.g. a deferral rationale for backlog issues)
    sim_outcome  TEXT,                    -- SIMULATE-only knob: 'green' (default) | 'heal' | 'refuse' | 'fail'
    labeled_at   TEXT,                    -- when the issue was labeled (for MTTR)
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    -- Local dedupe: v3 has no server idempotency, so the DB enforces "one remediation
    -- per (issue, spec)". Re-delivered webhooks / resync ticks collapse onto this row.
    UNIQUE (issue_id, spec_hash)
);

CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL UNIQUE,   -- devin-... (or devin-sim-... in SIMULATE)
    remediation_id INTEGER NOT NULL REFERENCES remediations(id),
    kind          TEXT NOT NULL DEFAULT 'remediate',  -- remediate | heal
    status        TEXT,
    status_detail TEXT,
    acus_consumed REAL NOT NULL DEFAULT 0,
    pr_url        TEXT,
    pr_state      TEXT,
    structured_output TEXT,               -- JSON verdict from Devin
    session_url   TEXT,                   -- human-facing session URL
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_remediation ON sessions(remediation_id);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(is_active);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    remediation_id INTEGER,
    session_id    TEXT,
    type          TEXT NOT NULL,
    detail        TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_remediation ON events(remediation_id);

-- Time series for the burn-down chart (one row appended per reconcile tick).
CREATE TABLE IF NOT EXISTS metrics_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    open_count   INTEGER NOT NULL,    -- remediations not yet merged
    merged_count INTEGER NOT NULL,
    in_flight    INTEGER NOT NULL,
    failed_count INTEGER NOT NULL,
    acus_total   REAL NOT NULL DEFAULT 0
);
