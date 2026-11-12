import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from solution.cli import main
from solution.db import connect, init_db
from solution.ingest_events import ingest_drop_events
from solution.ingest_leads import FileChangedError, ingest_drop_leads
from solution.pii import PiiVault
from solution.schema import load_workflows
from solution.stats import event_stats

SCHEMAS = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"
LEAD_HDR = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language"
BASE = {"event_id": "evt_1", "event_type": "call.initiated", "workflow_id": "motor_renewal",
        "call_id": "call_1", "occurred_at": "2026-10-03T10:01:00+05:30", "phone": "9876543210",
        "payload": {}}


def ev(_drop=(), **kw):
    d = {**BASE, **kw}
    for k in _drop:
        d.pop(k)
    return d


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)

    class Env:
        pass

    e = Env()
    e.conn, e.vault, e.root, e.db_path = conn, PiiVault(b"test-key"), tmp_path / "data", tmp_path / "t.db"
    e.ingest = lambda drop="drop_1": ingest_drop_events(conn, e.vault, e.root / drop)
    e.stats = lambda: event_stats(conn)
    e.rows = lambda: [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY seq")]
    yield e
    conn.close()


def write_events(root, lines, drop="drop_1", name="events_0001.jsonl"):
    """lines: dicts (dumped as JSON) or ready-made strings."""
    path = root / drop / "events" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(l if isinstance(l, str) else json.dumps(l, ensure_ascii=False) for l in lines)
    path.write_bytes((body + "\n").encode("utf-8"))
    return path


def epoch(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------- malformed

@pytest.mark.parametrize("line", [
    "{not json",
    '{"event_id": "evt_9", "event_type": "call.ini',                 # truncated, as the generator does
    "[1, 2]",
    '"just a string"',
    "123",
    "null",
    json.dumps(ev(_drop=("event_id",))),
    json.dumps(ev(event_id="")),
    json.dumps(ev(event_id="   ")),
    json.dumps(ev(event_id=None)),
    json.dumps(ev(event_id=123)),
    json.dumps(ev(event_id=["evt_1"])),
    json.dumps(ev(occurred_at="yesterday")),
    json.dumps(ev(occurred_at="2026-10-03")),                         # date only: no time
    json.dumps(ev(occurred_at=None)),
    json.dumps(ev(occurred_at=1759456260)),                           # numeric, not a timestamp string
    json.dumps(ev(_drop=("occurred_at",))),
    json.dumps(ev(_drop=("event_type",))),
    json.dumps(ev(event_type="")),
])
def test_malformed_variants_counted_and_skipped(env, line):
    write_events(env.root, [line])
    (res,) = env.ingest()
    assert (res.lines_total, res.malformed, res.duplicates) == (1, 1, 0)
    assert env.rows() == []


def test_invalid_utf8_line_is_malformed_not_a_crash(env):
    path = env.root / "drop_1" / "events" / "events_0001.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'\xff\xfe{"event_id": "x"}\n' + json.dumps(ev()).encode() + b"\n")
    (res,) = env.ingest()
    assert (res.lines_total, res.malformed) == (2, 1)
    assert len(env.rows()) == 1


def test_missing_phone_workflow_call_are_not_malformed(env):
    write_events(env.root, [ev(_drop=("phone", "workflow_id", "call_id", "payload"))])
    (res,) = env.ingest()
    assert res.malformed == 0
    (row,) = env.rows()
    assert row["phone_hash"] is None and row["workflow_id"] is None and row["call_id"] is None
    assert row["payload"] == "{}"


# ---------------------------------------------------------------- duplicates

def test_duplicate_within_one_file_first_wins(env):
    write_events(env.root, [ev(call_id="first"), ev(call_id="second"), ev(event_id="evt_2")])
    (res,) = env.ingest()
    assert (res.lines_total, res.malformed, res.duplicates) == (3, 0, 1)
    rows = env.rows()
    assert [r["event_id"] for r in rows] == ["evt_1", "evt_2"]
    assert rows[0]["call_id"] == "first"


def test_duplicate_across_drops(env):
    write_events(env.root, [ev(), ev(event_id="evt_2")], drop="drop_1")
    write_events(env.root, [ev(), ev(event_id="evt_3")], drop="drop_2")
    (r1,) = env.ingest("drop_1")
    (r2,) = env.ingest("drop_2")
    assert (r1.duplicates, r2.duplicates) == (0, 1)
    assert [r["event_id"] for r in env.rows()] == ["evt_1", "evt_2", "evt_3"]
    s = env.stats()
    assert (s["events_total"], s["duplicate_events_dropped"], s["events_stored"]) == (4, 1, 3)


def test_malformed_line_does_not_reserve_an_event_id(env):
    write_events(env.root, ['{"event_id": "evt_1", "occurred_at": "garbage"}', ev()])
    (res,) = env.ingest()
    assert (res.malformed, res.duplicates) == (1, 0)
    assert len(env.rows()) == 1


# ---------------------------------------------------------------- timestamps

def test_equal_instants_in_every_form_store_the_same_utc_epoch(env):
    forms = ["2026-10-03T04:31:00Z", "2026-10-03T10:01:00+05:30",
             "2026-10-03 10:01:00",            # naive means IST
             "2026-10-03T10:01:00"]            # naive, T-separated: also IST
    write_events(env.root, [ev(event_id=f"e{i}", occurred_at=f) for i, f in enumerate(forms)])
    env.ingest()
    rows = env.rows()
    assert {r["occurred_at_utc"] for r in rows} == {epoch(2026, 10, 3, 4, 31)}
    assert [r["occurred_at_raw"] for r in rows] == forms        # raw text kept as delivered


def test_other_offset_and_midnight_crossing(env):
    write_events(env.root, [ev(event_id="a", occurred_at="2026-10-05T19:00:00Z"),
                            ev(event_id="b", occurred_at="2026-10-06T00:30:00+05:30"),
                            ev(event_id="c", occurred_at="2026-10-05T20:00:00+01:00")])
    env.ingest()
    a, b, c = env.rows()
    assert a["occurred_at_utc"] == b["occurred_at_utc"] == epoch(2026, 10, 5, 19, 0)
    assert c["occurred_at_utc"] == epoch(2026, 10, 5, 19, 0)


# ---------------------------------------------------------------- phone

SIX_FORMATS = ["9876543210", "+91 98765 43210", "09876543210", "91-98765-43210",
               "(+91) 9876543210", "+919876543210"]


def test_event_phone_hash_matches_lead_phone_hash_in_every_format(env):
    lead_dir = env.root / "drop_1" / "leads" / "motor_renewal"
    lead_dir.mkdir(parents=True)
    (lead_dir / "batch_001.csv").write_bytes(
        (LEAD_HDR + "\r\nAnn,+91 98765 43210,P1,,,\r\n").encode("utf-8"))
    ingest_drop_leads(env.conn, env.vault, load_workflows(SCHEMAS), env.root / "drop_1")
    lead_hash = env.conn.execute("SELECT phone_hash FROM leads").fetchone()[0]

    write_events(env.root, [ev(event_id=f"e{i}", phone=p) for i, p in enumerate(SIX_FORMATS)])
    env.ingest()
    rows = env.rows()
    assert len(rows) == 6
    assert {r["phone_hash"] for r in rows} == {lead_hash}


def test_invalid_or_missing_phone_gives_null_hash_but_event_is_kept(env):
    bad = ["N/A", "12345", "", "+1 415 555 0134", "022-2345-6789"]
    write_events(env.root, [ev(event_id=f"e{i}", phone=p) for i, p in enumerate(bad)]
                 + [ev(event_id="e_none", phone=None)])
    (res,) = env.ingest()
    assert res.malformed == 0
    assert all(r["phone_hash"] is None for r in env.rows()) and len(env.rows()) == 6


def test_events_for_unknown_numbers_and_workflows_are_stored_unjudged(env):
    write_events(env.root, [ev(phone="9000000001"), ev(event_id="e2", workflow_id="no_such_workflow")])
    env.ingest()
    rows = env.rows()
    assert len(rows) == 2 and rows[1]["workflow_id"] == "no_such_workflow"
    assert env.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


# ---------------------------------------------------------------- dispositions and payload

def test_override_without_phone_has_null_hash_and_code(env):
    write_events(env.root, [
        ev(event_id="s1", event_type="disposition.set", payload={"disposition_code": "renewed"}),
        ev(event_id="o1", event_type="disposition.override", _drop=("phone",),
           payload={"disposition_code": "dnd", "overridden_by": "supervisor_3", "reason": "qa_review"}),
    ])
    env.ingest()
    s, o = env.rows()
    assert s["phone_hash"] is not None and s["disposition_code"] == "renewed"
    assert o["phone_hash"] is None and o["disposition_code"] == "dnd"
    assert json.loads(o["payload"]) == {"disposition_code": "dnd", "overridden_by": "supervisor_3",
                                        "reason": "qa_review"}


def test_unknown_type_and_unknown_code_are_stored_as_is(env):
    write_events(env.root, [
        ev(event_id="u1", event_type="call.teleported"),
        ev(event_id="u2", event_type="disposition.set", payload={"disposition_code": "RENEWD"}),
        ev(event_id="u3", event_type="disposition.set", payload={"disposition_code": "call_back"}),
    ])
    (res,) = env.ingest()
    assert res.malformed == 0
    r1, r2, r3 = env.rows()
    assert r1["event_type"] == "call.teleported" and r1["disposition_code"] is None
    assert r2["disposition_code"] == "RENEWD" and r3["disposition_code"] == "call_back"


def test_disposition_code_only_filled_for_disposition_events(env):
    write_events(env.root, [
        ev(event_id="x1", event_type="call.ended", payload={"disposition_code": "renewed", "duration_sec": 5}),
        ev(event_id="x2", event_type="disposition.set", payload={}),                   # no code at all
        ev(event_id="x3", event_type="disposition.set", payload={"disposition_code": 7}),
    ])
    env.ingest()
    assert [r["disposition_code"] for r in env.rows()] == [None, None, None]


def test_payload_stored_as_json_without_any_phone_key(env):
    write_events(env.root, [ev(event_type="call.telephony_failed",
                               payload={"reason": "carrier_rejected", "phone": "9876543210"})])
    env.ingest()
    (row,) = env.rows()
    assert json.loads(row["payload"]) == {"reason": "carrier_rejected"}
    assert "9876543210" not in row["payload"]


# ---------------------------------------------------------------- lines, files

def test_blank_lines_not_counted_and_line_numbers_are_physical(env):
    path = env.root / "drop_1" / "events" / "events_0001.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(("\n\n   \n" + json.dumps(ev()) + "\n\n" + json.dumps(ev(event_id="evt_2")) + "\r\n").encode())
    (res,) = env.ingest()
    assert (res.lines_total, res.malformed, res.duplicates) == (2, 0, 0)
    assert [r["line_no"] for r in env.rows()] == [4, 6]


def test_line_separator_inside_json_string_does_not_split_the_line(env):
    write_events(env.root, [ev(payload={"note": "a b\u0085c"})])
    (res,) = env.ingest()
    assert (res.lines_total, res.malformed) == (1, 0)


def test_bom_on_first_line_tolerated(env):
    path = env.root / "drop_1" / "events" / "events_0001.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(ev()).encode() + b"\n")
    (res,) = env.ingest()
    assert res.malformed == 0 and len(env.rows()) == 1


def test_files_ingested_in_filename_order(env):
    write_events(env.root, [ev(call_id="from_b")], name="events_0002.jsonl")
    write_events(env.root, [ev(call_id="from_a")], name="events_0001.jsonl")
    r = env.ingest()
    assert [x.rel_path for x in r] == ["events/events_0001.jsonl", "events/events_0002.jsonl"]
    assert env.rows()[0]["call_id"] == "from_a" and r[1].duplicates == 1


def test_file_counters_recorded_in_ingest_files(env):
    write_events(env.root, ["{bad", ev(), ev(), "", ev(event_id="evt_2")])
    env.ingest()
    row = env.conn.execute("SELECT kind, lines_total, malformed, duplicates, workflow_id, batch_id "
                           "FROM ingest_files").fetchone()
    assert tuple(row) == ("events", 4, 1, 1, None, None)


# ---------------------------------------------------------------- idempotency / safety

def test_reingest_same_file_is_skipped_and_changes_nothing(env):
    write_events(env.root, ["{bad", ev(), ev(), ev(event_id="evt_2", phone="N/A")])
    assert env.ingest()[0].status == "ingested"
    before = list(env.conn.iterdump())
    stats = env.stats()
    again = env.ingest()
    assert [r.status for r in again] == ["skipped"]
    assert list(env.conn.iterdump()) == before and env.stats() == stats


def test_changed_file_is_refused(env):
    write_events(env.root, [ev()])
    env.ingest()
    before = list(env.conn.iterdump())
    write_events(env.root, [ev(), ev(event_id="evt_2")])
    with pytest.raises(FileChangedError):
        env.ingest()
    assert list(env.conn.iterdump()) == before


def test_failure_mid_file_rolls_back(env, monkeypatch):
    write_events(env.root, ["{bad", ev(), ev(event_id="evt_2"), ev(event_id="evt_3")])
    real, calls = env.vault.phone_hash, {"n": 0}

    def flaky(p):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return real(p)

    monkeypatch.setattr(env.vault, "phone_hash", flaky)
    with pytest.raises(RuntimeError):
        env.ingest()
    assert not env.conn.in_transaction
    for table in ("events", "ingest_files"):
        assert env.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    monkeypatch.undo()
    (res,) = env.ingest()
    assert (res.status, res.lines_total, res.malformed) == ("ingested", 4, 1)
    assert len(env.rows()) == 3


def test_ledger_rows_cannot_be_changed_after_ingest(env):
    write_events(env.root, [ev()])
    env.ingest()
    with pytest.raises(Exception, match="append-only"):
        env.conn.execute("DELETE FROM events")


# ---------------------------------------------------------------- PII at rest

def test_no_raw_phone_in_db_bytes(env):
    phones = ["+91 91234 56780", "09123456781", "91-91234-56782", "(+91) 9123456783", "9123456784",
              "+919123456785", "N/A-98765-43219"]
    lines = [ev(event_id=f"e{i}", phone=p, call_id=f"call_{i}") for i, p in enumerate(phones)]
    lines.append(json.dumps(ev(event_id="mal", phone="+91 91234 56789")).replace('"event_id"', '"x"'))  # malformed line
    lines.append(ev(event_id="dup0", phone="+91 91234 56780"))
    lines.append(ev(event_id="dup0", phone="+91 91234 56780"))                                       # duplicate
    write_events(env.root, lines)
    env.ingest()
    env.conn.close()
    data = b"".join(p.read_bytes() for p in env.db_path.parent.glob("t.db*"))
    assert b"call_3" in data                              # control: the scan sees real content
    needles = list(phones) + ["+91 91234 56789", "91234 56789", "9123456789"]
    for raw in phones[:6]:
        ten = "".join(c for c in raw if c.isdigit())[-10:]
        needles += [ten, "+91" + ten, f"{ten[:5]} {ten[5:]}", f"{ten[:5]}-{ten[5:]}", "0" + ten]
    for n in needles:
        assert n.encode() not in data, f"raw phone found in db bytes: {n!r}"


# ---------------------------------------------------------------- CLI

def test_cli_ingests_leads_then_events_and_stats_show_events(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEDGER_PII_KEY", "cli-key")
    root = tmp_path / "data"
    lead_dir = root / "drop_1" / "leads" / "motor_renewal"
    lead_dir.mkdir(parents=True)
    (lead_dir / "batch_001.csv").write_bytes((LEAD_HDR + "\nAnn,9876543210,P1,,,\n").encode())
    write_events(root, ["{bad", ev(), ev(), ev(event_id="evt_2")], drop="drop_1")
    write_events(root, [ev(event_id="evt_2"), ev(event_id="evt_3")], drop="drop_2")
    db = str(tmp_path / "cli.db")

    assert main(["ingest", str(root), "--db", db]) == 0
    out = capsys.readouterr().out
    assert out.index("drop_1/leads/motor_renewal/batch_001.csv") < out.index("drop_1/events/events_0001.jsonl")
    assert "lines=4 malformed=1 duplicates=1 stored=2" in out
    assert "lines=2 malformed=0 duplicates=1 stored=1" in out

    assert main(["ingest", str(root), "--db", db]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sum(l.startswith("skipped") for l in lines) == 3
    assert not any(l.startswith("ingested") for l in lines)

    assert main(["stats", "--db", db]) == 0
    out = capsys.readouterr().out
    assert re.search(r"events total \(lines\):\s+6\b", out)
    assert re.search(r"malformed events:\s+1\b", out)
    assert re.search(r"duplicate events dropped:\s+2\b", out)
    assert re.search(r"events stored in ledger:\s+3\b", out)
    assert "WARNING" not in out


def test_cli_changed_event_file_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEDGER_PII_KEY", "cli-key")
    root = tmp_path / "data"
    write_events(root, [ev()])
    db = str(tmp_path / "x.db")
    assert main(["ingest", str(root), "--db", db]) == 0
    write_events(root, [ev(), ev(event_id="evt_2")])
    assert main(["ingest", str(root), "--db", db]) == 1
    assert "different content" in capsys.readouterr().err
