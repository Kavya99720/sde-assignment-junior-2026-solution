"""SQLite connection, DDL, disposition loading and the PII-key fingerprint guard."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .schema import Disposition

SCHEMA_VERSION = "1"
_DDL_PATH = Path(__file__).with_name("ddl.sql")


class PiiKeyMismatch(RuntimeError):
    """The database was created with a different PII key than the one now in use."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Autocommit connection (callers open transactions explicitly) with foreign keys enforced."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL_PATH.read_text(encoding="utf-8"))
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))


def check_pii_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> None:
    """Record the key fingerprint on first use; raise PiiKeyMismatch if it ever differs."""
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('pii_key_fingerprint', ?)", (fingerprint,))
    stored = conn.execute("SELECT value FROM meta WHERE key = 'pii_key_fingerprint'").fetchone()[0]
    if stored != fingerprint:
        raise PiiKeyMismatch(
            "PII key does not match this database (fingerprint mismatch). "
            "Use the original key, or ingest into a fresh database."
        )


def load_dispositions(conn: sqlite3.Connection, dispositions: Iterable[Disposition]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO dispositions (workflow_id, code, label, is_terminal, is_success) "
        "VALUES (?,?,?,?,?)",
        [(d.workflow_id, d.code, d.label, int(d.is_terminal), int(d.is_success)) for d in dispositions],
    )
