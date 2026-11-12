-- Plain SQLite DDL: no STRICT, generated columns, JSON functions or WAL, so it runs on old SQLite builds.
-- Re-runnable: everything is IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per ingested file. Written last in the file's transaction; its presence means "done".
CREATE TABLE IF NOT EXISTS ingest_files (
    file_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT    NOT NULL CHECK (kind IN ('leads', 'events')),
    drop_name      TEXT    NOT NULL,
    rel_path       TEXT    NOT NULL,                -- posix style
    workflow_id    TEXT,
    batch_id       TEXT,
    sha256         TEXT    NOT NULL CHECK (length(sha256) = 64),
    ingested_at    TEXT    NOT NULL,
    rows_total     INTEGER NOT NULL DEFAULT 0 CHECK (rows_total >= 0),     -- leads: non-blank data rows
    rows_rejected  INTEGER NOT NULL DEFAULT 0 CHECK (rows_rejected >= 0),
    rows_merged    INTEGER NOT NULL DEFAULT 0 CHECK (rows_merged >= 0),    -- duplicate valid rows merged
    field_warnings INTEGER NOT NULL DEFAULT 0 CHECK (field_warnings >= 0),
    lines_total    INTEGER NOT NULL DEFAULT 0 CHECK (lines_total >= 0),    -- events: non-blank lines
    malformed      INTEGER NOT NULL DEFAULT 0 CHECK (malformed >= 0),
    duplicates     INTEGER NOT NULL DEFAULT 0 CHECK (duplicates >= 0),
    UNIQUE (drop_name, rel_path),
    CHECK (kind <> 'leads' OR (workflow_id IS NOT NULL AND batch_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id TEXT    NOT NULL,
    code        TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
    is_success  INTEGER NOT NULL CHECK (is_success IN (0, 1)),
    PRIMARY KEY (workflow_id, code)
);

-- Mutable by design (duplicate rows merge into it). The events table is the ledger.
CREATE TABLE IF NOT EXISTS leads (
    lead_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id   TEXT    NOT NULL,
    phone_hash    TEXT    NOT NULL CHECK (length(phone_hash) = 64),   -- HMAC of canonical phone
    batch_id      TEXT    NOT NULL,                                   -- first file stem, never changes
    first_file_id INTEGER NOT NULL REFERENCES ingest_files (file_id),
    external_ref  TEXT    NOT NULL CHECK (length(external_ref) > 0),
    phone_last4   TEXT    NOT NULL CHECK (length(phone_last4) = 4),
    pii_enc       BLOB    NOT NULL,                                   -- encrypted pii fields
    attrs         TEXT    NOT NULL,                                   -- JSON of non-PII schema fields
    merged_rows   INTEGER NOT NULL DEFAULT 0 CHECK (merged_rows >= 0),
    UNIQUE (workflow_id, phone_hash)
);
CREATE INDEX IF NOT EXISTS ix_leads_batch ON leads (workflow_id, batch_id);

-- No PII: reason and position only.
CREATE TABLE IF NOT EXISTS lead_rejections (
    file_id    INTEGER NOT NULL REFERENCES ingest_files (file_id),
    row_number INTEGER NOT NULL CHECK (row_number >= 1),
    reason     TEXT    NOT NULL CHECK (length(reason) > 0),
    PRIMARY KEY (file_id, row_number)
);

-- The append-only interaction ledger. Linking, orphan status and effective dispositions are
-- derived at report time and never written back here.
CREATE TABLE IF NOT EXISTS events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT    NOT NULL UNIQUE CHECK (length(event_id) > 0),
    event_type      TEXT    NOT NULL CHECK (length(event_type) > 0),
    workflow_id     TEXT,
    call_id         TEXT,
    occurred_at_utc INTEGER NOT NULL,                                 -- epoch seconds
    occurred_at_raw TEXT    NOT NULL,
    phone_hash      TEXT CHECK (phone_hash IS NULL OR length(phone_hash) = 64),
    disposition_code TEXT,                                            -- extracted from payload at ingest
    payload         TEXT    NOT NULL,                                 -- JSON, phone removed
    file_id         INTEGER NOT NULL REFERENCES ingest_files (file_id),
    line_no         INTEGER NOT NULL CHECK (line_no >= 1)
);
CREATE INDEX IF NOT EXISTS ix_events_phone ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS ix_events_call  ON events (workflow_id, call_id);
CREATE INDEX IF NOT EXISTS ix_events_type  ON events (event_type);

-- Append-only enforcement.
CREATE TRIGGER IF NOT EXISTS trg_events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_ingest_files_no_update BEFORE UPDATE ON ingest_files
BEGIN SELECT RAISE(ABORT, 'ingest_files is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ingest_files_no_delete BEFORE DELETE ON ingest_files
BEGIN SELECT RAISE(ABORT, 'ingest_files is append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_lead_rejections_no_update BEFORE UPDATE ON lead_rejections
BEGIN SELECT RAISE(ABORT, 'lead_rejections is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_lead_rejections_no_delete BEFORE DELETE ON lead_rejections
BEGIN SELECT RAISE(ABORT, 'lead_rejections is append-only'); END;
