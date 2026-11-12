"""Event JSONL ingestion into the append-only ledger. One transaction per file.

Only per-record facts are decided here (malformed, duplicate, normalised time, phone hash).
Orphan, linking and unknown-disposition status are derived at report time.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .ingest_leads import FileChangedError, FileResult
from .normalise import normalise_phone, parse_timestamp
from .pii import PiiVault

_DISPOSITION_EVENTS = ("disposition.set", "disposition.override")
_BOM = b"\xef\xbb\xbf"

_INSERT = (
    "INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at_utc, "
    "occurred_at_raw, phone_hash, disposition_code, payload, file_id, line_no) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (event_id) DO NOTHING"
)


def _text(v) -> str | None:
    """Identifier-like value: a non-empty string (trimmed), or a number as text; otherwise None."""
    if isinstance(v, str):
        v = v.strip()
        return v or None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def parse_event_line(line: bytes, vault: PiiVault) -> dict | None:
    """Row for the events table (without file_id/line_no), or None if the line is malformed."""
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    event_id = obj.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        return None
    event_type = _text(obj.get("event_type"))
    raw_ts = obj.get("occurred_at")
    epoch = parse_timestamp(raw_ts)
    if event_type is None or epoch is None:
        return None

    payload = obj.get("payload")
    payload = {k: v for k, v in payload.items() if k != "phone"} if isinstance(payload, dict) else {}
    disposition = None
    if event_type in _DISPOSITION_EVENTS and isinstance(payload.get("disposition_code"), str):
        disposition = payload["disposition_code"]

    canonical = normalise_phone(obj.get("phone")) if obj.get("phone") is not None else None
    return {
        "event_id": event_id,
        "event_type": event_type,
        "workflow_id": _text(obj.get("workflow_id")),
        "call_id": _text(obj.get("call_id")),
        "occurred_at_utc": epoch,
        "occurred_at_raw": raw_ts,
        "phone_hash": vault.phone_hash(canonical) if canonical else None,
        "disposition_code": disposition,
        "payload": json.dumps(payload, sort_keys=True, ensure_ascii=False),
    }


def ingest_event_file(conn: sqlite3.Connection, vault: PiiVault, path: Path,
                      drop_name: str, rel_path: str) -> FileResult:
    path = Path(path)
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    try:
        prior = conn.execute("SELECT sha256 FROM ingest_files WHERE drop_name = ? AND rel_path = ?",
                             (drop_name, rel_path)).fetchone()
        if prior is not None:
            conn.execute("ROLLBACK")
            if prior["sha256"] == sha:
                return FileResult("skipped", drop_name, rel_path, kind="events")
            raise FileChangedError(
                f"{drop_name}/{rel_path} was already ingested with different content; refusing to re-ingest")

        file_id = conn.execute("SELECT COALESCE(MAX(file_id), 0) + 1 FROM ingest_files").fetchone()[0]
        lines_total = malformed = duplicates = 0

        # bytes.splitlines only breaks on \n, \r and \r\n, never inside a JSON string (unlike str).
        for line_no, line in enumerate(data.splitlines(), start=1):
            if line_no == 1 and line.startswith(_BOM):
                line = line[len(_BOM):]
            if not line.strip():
                continue                                   # blank: not counted
            lines_total += 1
            ev = parse_event_line(line, vault)
            if ev is None:
                malformed += 1
                continue
            cur = conn.execute(_INSERT, (
                ev["event_id"], ev["event_type"], ev["workflow_id"], ev["call_id"], ev["occurred_at_utc"],
                ev["occurred_at_raw"], ev["phone_hash"], ev["disposition_code"], ev["payload"],
                file_id, line_no))
            if cur.rowcount == 0:
                duplicates += 1                            # event_id seen before: first occurrence wins

        conn.execute(
            "INSERT INTO ingest_files (file_id, kind, drop_name, rel_path, sha256, ingested_at, "
            "lines_total, malformed, duplicates) VALUES (?, 'events', ?, ?, ?, ?, ?, ?, ?)",
            (file_id, drop_name, rel_path, sha, datetime.now(timezone.utc).isoformat(timespec="seconds"),
             lines_total, malformed, duplicates))
        conn.execute("COMMIT")
    except FileChangedError:
        raise
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return FileResult("ingested", drop_name, rel_path, kind="events",
                      lines_total=lines_total, malformed=malformed, duplicates=duplicates)


def ingest_drop_events(conn: sqlite3.Connection, vault: PiiVault, drop_dir: Path) -> list[FileResult]:
    """Ingest drop/events/*.jsonl in filename order."""
    drop_dir = Path(drop_dir)
    events_dir = drop_dir / "events"
    if not events_dir.is_dir():
        return []
    return [
        ingest_event_file(conn, vault, f, drop_dir.name, f.relative_to(drop_dir).as_posix())
        for f in sorted(events_dir.glob("*.jsonl"), key=lambda p: p.name)
    ]
