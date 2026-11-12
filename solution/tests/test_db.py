import sqlite3
from pathlib import Path

import pytest

from solution.db import PiiKeyMismatch, check_pii_fingerprint, connect, init_db, load_dispositions
from solution.schema import load_dispositions as read_dispositions

SCHEMAS = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"
H = "a" * 64


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    yield c
    c.close()


def _file(conn, kind="events", path="events/e1.jsonl", drop="drop_1"):
    wf, batch = ("motor_renewal", "batch_001") if kind == "leads" else (None, None)
    return conn.execute(
        "INSERT INTO ingest_files (kind, drop_name, rel_path, workflow_id, batch_id, sha256, ingested_at) "
        "VALUES (?,?,?,?,?,?,'2026-01-01T00:00:00')", (kind, drop, path, wf, batch, H)).lastrowid


def _event(conn, file_id, event_id="evt_1", phone_hash=H):
    conn.execute(
        "INSERT INTO events (event_id, event_type, workflow_id, call_id, occurred_at_utc, occurred_at_raw, "
        "phone_hash, payload, file_id, line_no) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (event_id, "call.initiated", "motor_renewal", "call_1", 1000, "raw", phone_hash, "{}", file_id, 1))


def test_init_is_idempotent(conn):
    init_db(conn)
    init_db(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"meta", "ingest_files", "dispositions", "leads", "lead_rejections", "events"} <= names


def test_events_update_rejected(conn):
    fid = _file(conn)
    _event(conn, fid)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET call_id = 'x' WHERE event_id = 'evt_1'")
    assert conn.execute("SELECT call_id FROM events").fetchone()[0] == "call_1"


def test_events_delete_rejected(conn):
    fid = _file(conn)
    _event(conn, fid)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_ingest_files_and_rejections_append_only(conn):
    fid = _file(conn, kind="leads", path="leads/motor_renewal/batch_001.csv")
    conn.execute("INSERT INTO lead_rejections VALUES (?,?,?)", (fid, 1, "invalid_phone"))
    for sql in ("UPDATE ingest_files SET malformed = 5", "DELETE FROM ingest_files",
                "UPDATE lead_rejections SET reason = 'x'", "DELETE FROM lead_rejections"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)


def test_event_id_unique(conn):
    fid = _file(conn)
    _event(conn, fid)
    with pytest.raises(sqlite3.IntegrityError):
        _event(conn, fid)
    conn.execute("INSERT OR IGNORE INTO events (event_id, event_type, occurred_at_utc, occurred_at_raw, "
                 "payload, file_id, line_no) VALUES ('evt_1','call.initiated',1,'r','{}',?,2)", (fid,))
    assert conn.execute("SELECT changes()").fetchone()[0] == 0


def test_event_phone_hash_check(conn):
    fid = _file(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _event(conn, fid, phone_hash="short")
    _event(conn, fid, event_id="evt_2", phone_hash=None)  # overrides carry no phone


def test_foreign_key_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _event(conn, file_id=999)


def test_file_identity_unique(conn):
    _file(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _file(conn)
    _file(conn, drop="drop_2")  # same rel_path in another drop is a different file


def test_leads_file_requires_workflow_and_batch(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ingest_files (kind, drop_name, rel_path, sha256, ingested_at) "
                     "VALUES ('leads','d','p',?, 'now')", (H,))


def test_lead_identity_unique(conn):
    fid = _file(conn, kind="leads", path="leads/motor_renewal/batch_001.csv")
    sql = ("INSERT INTO leads (workflow_id, phone_hash, batch_id, first_file_id, external_ref, phone_last4, "
           "pii_enc, attrs) VALUES (?,?,?,?,?,?,?,?)")
    conn.execute(sql, ("motor_renewal", H, "batch_001", fid, "P1", "3210", b"x", "{}"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("motor_renewal", H, "batch_002", fid, "P2", "3210", b"x", "{}"))
    conn.execute(sql, ("policy_feedback", H, "batch_001", fid, "O1", "3210", b"x", "{}"))  # other workflow ok


def test_pii_fingerprint(conn):
    check_pii_fingerprint(conn, "fp1")
    check_pii_fingerprint(conn, "fp1")  # same key: fine
    with pytest.raises(PiiKeyMismatch):
        check_pii_fingerprint(conn, "fp2")


def test_load_dispositions_idempotent(conn):
    d = read_dispositions(SCHEMAS / "dispositions.csv")
    load_dispositions(conn, d)
    load_dispositions(conn, d)
    assert conn.execute("SELECT COUNT(*) FROM dispositions").fetchone()[0] == 14
    row = conn.execute("SELECT is_terminal, is_success FROM dispositions "
                       "WHERE workflow_id='motor_renewal' AND code='renewed'").fetchone()
    assert tuple(row) == (1, 1)
