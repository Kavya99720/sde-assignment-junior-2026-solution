"""Report tests on small hand-built worlds. Expected values are computed by hand in the comments."""
import csv
import hashlib
import io
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from solution.cli import main
from solution.db import connect, init_db, load_dispositions
from solution.ingest_events import ingest_drop_events
from solution.ingest_leads import ingest_drop_leads
from solution.pii import PiiVault
from solution.report import FILES, HEADERS, generate_reports
from solution.schema import load_dispositions as read_dispositions
from solution.schema import load_workflows

SCHEMAS = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"
HDR = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language"
PF_HDR = "name,mobile,order id,item,order date,language"
T0 = "2026-10-03 10:00:00"
IST0 = "2026-10-03T10:00:00+05:30"


def ph(n):
    return f"98000{n:05d}"


def plus(ts, seconds):
    return (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


class Reports:
    def __init__(self, out):
        self.out = Path(out)

    def text(self, name):
        return (self.out / name).read_text(encoding="utf-8")

    def rows(self, name):
        return list(csv.reader(io.StringIO(self.text(name), newline="")))[1:]

    def header(self, name):
        return list(csv.reader(io.StringIO(self.text(name), newline="")))[0]

    def dq(self):
        return json.loads(self.text("data_quality.json"))

    def leads(self):
        h = self.header("lead_report.csv")
        return {r[1]: dict(zip(h, r)) for r in self.rows("lead_report.csv")}


class World:
    def __init__(self, tmp_path):
        self.root = tmp_path / "data"
        self.db = tmp_path / "w.db"
        self.out = tmp_path / "reports"
        self.conn = connect(self.db)
        init_db(self.conn)
        load_dispositions(self.conn, read_dispositions(SCHEMAS / "dispositions.csv"))
        self.vault = PiiVault(b"test-key")
        self.wfs = load_workflows(SCHEMAS)
        self.n = 0

    # -- writing inputs
    def leads(self, lines, drop="drop_1", batch="batch_001", wf="motor_renewal", header=HDR):
        p = self.root / drop / "leads" / wf / f"{batch}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((header + "\n" + "\n".join(lines) + "\n").encode("utf-8"))

    def events(self, evs, drop="drop_1", name="events_0001.jsonl", reverse=False):
        evs = list(reversed(evs)) if reverse else evs
        p = self.root / drop / "events" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(("\n".join(e if isinstance(e, str) else json.dumps(e) for e in evs) + "\n").encode())

    def ev(self, typ, call, ts, phone=None, wf="motor_renewal", payload=None, eid=None):
        self.n += 1
        d = {"event_id": eid or f"e{self.n:04d}", "event_type": typ, "workflow_id": wf, "call_id": call,
             "occurred_at": ts, "payload": payload or {}}
        if phone is not None:
            d["phone"] = phone
        return d

    def call(self, call, phone, t, connected=False, failed=False, disp=None, ended=False, wf="motor_renewal"):
        out = [self.ev("call.initiated", call, t, phone, wf)]
        if failed:
            out.append(self.ev("call.telephony_failed", call, plus(t, 5), phone, wf, {"reason": "sip_503"}))
        if connected:
            out.append(self.ev("call.connected", call, plus(t, 10), phone, wf))
        if ended:
            out.append(self.ev("call.ended", call, plus(t, 30), phone, wf, {"duration_sec": 0, "end_reason": "no_answer"}))
        if disp is not None:
            out.append(self.ev("disposition.set", call, plus(t, 300), phone, wf, {"disposition_code": disp}))
        return out

    def override(self, call, ts, code, wf="motor_renewal", eid=None):
        return self.ev("disposition.override", call, ts, None, wf,
                       {"disposition_code": code, "overridden_by": "supervisor_1", "reason": "qa_review"}, eid)

    # -- running
    def ingest(self, drop="drop_1"):
        ingest_drop_leads(self.conn, self.vault, self.wfs, self.root / drop)
        ingest_drop_events(self.conn, self.vault, self.root / drop)

    def report(self):
        generate_reports(self.db, self.out)
        return Reports(self.out)


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.conn.close()


# ------------------------------------------------------------ effective disposition

@pytest.mark.parametrize("reverse", [False, True])
def test_effective_disposition_matrix(w, reverse):
    names = "ABCDEFG"
    w.leads([f"Lead {c},{ph(i + 1)},{c},,," for i, c in enumerate(names)])
    disp = {"A": "renewed", "B": "callback_requested", "C": "not_interested", "D": "callback_requested",
            "E": "RENEWD", "F": None, "G": "renewed"}
    evs = []
    for i, c in enumerate(names):
        evs += w.call(f"call_{c}", ph(i + 1), T0, connected=True, disp=disp[c])     # set lands at 10:05:10
    evs.append(w.override("call_B", "2026-10-03 10:02:00", "renewed"))               # earlier than the set: still wins
    evs.append(w.override("call_C", "2026-10-03 11:00:00", "renewed"))
    evs.append(w.override("call_C", "2026-10-03 12:00:00", "RENEWD"))                # unknown: falls back to renewed
    evs.append(w.override("call_D", "2026-10-03 11:00:00", "bogus"))                 # unknown: falls back to the set
    evs.append(w.override("call_F", "2026-10-03 11:00:00", "dnd", eid="tie_a"))
    evs.append(w.override("call_F", "2026-10-03 11:00:00", "renewed", eid="tie_b"))  # same time: higher event_id wins
    evs.append(w.ev("disposition.set", "call_G", "2026-10-03 10:06:00", ph(7), payload={"disposition_code": "not_interested"}))
    w.events(evs, reverse=reverse)
    w.ingest()
    r = w.report()

    def got(c):
        x = r.leads()[c]
        return (x["bucket"], x["total_calls"], x["connected_calls"], x["last_call_at"],
                x["current_disposition"], x["ever_success"])

    assert got("A") == ("completed", "1", "1", IST0, "renewed", "true")
    assert got("B") == ("completed", "1", "1", IST0, "renewed", "true")
    assert got("C") == ("completed", "1", "1", IST0, "renewed", "true")
    assert got("D") == ("in_progress_dialed", "1", "1", IST0, "callback_requested", "false")
    assert got("E") == ("in_progress_dialed", "1", "1", IST0, "", "false")           # unknown set ignored
    assert got("F") == ("completed", "1", "1", IST0, "renewed", "true")
    assert got("G") == ("completed", "1", "1", IST0, "not_interested", "false")      # latest set wins
    dq = r.dq()
    assert (dq["orphan_events"], dq["unknown_disposition_events"]) == (0, 3)         # RENEWD(C), bogus(D), RENEWD(E)


# ------------------------------------------------------------ linking across drops

def test_late_lead_and_early_override_across_drops(w):
    # drop_1: lead Y; an override for call_Y whose call events have not arrived; events of an unknown number.
    w.leads([f"Yan,{ph(21)},Y,,,"], drop="drop_1", batch="batch_001")
    w.events([
        w.override("call_Y", "2026-10-07 09:00:00", "renewed"),                       # orphan: no linked event for call_Y
        *w.call("call_X", ph(22), "2026-10-04 10:00:00")[:1],                          # orphan: lead X not uploaded yet
        w.ev("disposition.set", "call_X", "2026-10-04 10:05:00", ph(22), payload={"disposition_code": "RENEWD"}),
        w.override("call_X", "2026-10-04 12:00:00", "renewed"),
    ], drop="drop_1")
    w.ingest("drop_1")
    r1 = w.report()
    assert r1.dq()["orphan_events"] == 4 and r1.dq()["unknown_disposition_events"] == 0   # orphan wins over unknown
    assert r1.rows("lead_report.csv") == [
        ["motor_renewal", "Y", "batch_001", "+91******0021", "yet_to_reach_out", "0", "0", "", "", "false"]]
    assert r1.rows("daily_overview.csv") == []
    assert r1.rows("batch_summary.csv") == [["motor_renewal", "batch_001", "1", "1", "0", "0", "0"]]

    # drop_2: lead X arrives, and call_Y's own events arrive after its override.
    w.leads([f"Xan,{ph(22)},X,,,"], drop="drop_2", batch="batch_003")
    w.events([
        w.ev("call.initiated", "call_Y", "2026-10-05T19:00:00Z", ph(21)),             # 00:30 IST on the 6th
        w.ev("call.connected", "call_Y", "2026-10-05T19:00:10Z", ph(21)),
    ], drop="drop_2")
    w.ingest("drop_2")
    r2 = w.report()
    assert r2.dq()["orphan_events"] == 0
    assert r2.dq()["unknown_disposition_events"] == 1                                 # X's RENEWD is linked now
    assert r2.rows("lead_report.csv") == [
        ["motor_renewal", "X", "batch_003", "+91******0022", "completed", "1", "0",
         "2026-10-04T10:00:00+05:30", "renewed", "true"],       # override beats the ignored unknown set; not connected
        ["motor_renewal", "Y", "batch_001", "+91******0021", "completed", "1", "1",
         "2026-10-06T00:30:00+05:30", "renewed", "true"],
    ]
    assert r2.rows("daily_overview.csv") == [
        ["motor_renewal", "2026-10-04", "1", "0", "0.0000", "1"],
        ["motor_renewal", "2026-10-06", "1", "1", "1.0000", "1"],
    ]
    assert r2.rows("batch_summary.csv") == [
        ["motor_renewal", "batch_001", "1", "0", "0", "0", "1"],
        ["motor_renewal", "batch_003", "1", "0", "0", "0", "1"],
    ]


def test_override_on_call_with_linked_event_but_no_initiated(w):
    w.leads([f"Zed,{ph(31)},Z,,,"])
    w.events([w.ev("call.connected", "call_Z", T0, ph(31)), w.override("call_Z", plus(T0, 600), "renewed")])
    w.ingest()
    r = w.report()
    dq = r.dq()
    assert (dq["orphan_events"], dq["unknown_disposition_events"]) == (0, 0)          # linked, not orphans
    assert r.leads()["Z"]["bucket"] == "yet_to_reach_out"                              # but no counted call
    assert r.leads()["Z"]["ever_success"] == "false" and r.leads()["Z"]["current_disposition"] == ""
    assert r.rows("daily_overview.csv") == []


def test_unknown_code_on_an_orphan_counts_only_as_orphan(w):
    w.leads([f"Ann,{ph(1)},A,,,"])
    w.events([w.ev("disposition.set", "call_n", T0, ph(99), payload={"disposition_code": "RENEWD"}),
              w.ev("disposition.set", "call_k", T0, ph(1), payload={"disposition_code": "RENEWD"})])
    w.ingest()
    dq = w.report().dq()
    assert (dq["orphan_events"], dq["unknown_disposition_events"]) == (1, 1)


# ------------------------------------------------------------ calls and dates

def test_two_initiated_events_one_call_and_ist_midnight(w):
    w.leads([f"M,{ph(41)},M,,,", f"N,{ph(42)},N,,,", f"O,{ph(43)},O,,,"])
    w.events([
        w.ev("call.initiated", "call_M", "2026-10-03 10:00:00", ph(41)),
        w.ev("call.initiated", "call_M", "2026-10-03 09:00:00", ph(41)),              # redelivered with another id
        w.ev("call.connected", "call_M", "2026-10-03 10:00:10", ph(41)),
        w.ev("call.initiated", "call_N", "2026-10-05T18:29:59Z", ph(42)),             # 23:59:59 IST on the 5th
        w.ev("call.initiated", "call_O", "2026-10-05T19:00:00Z", ph(43)),             # 00:30:00 IST on the 6th
    ])
    w.ingest()
    r = w.report()
    m = r.leads()["M"]
    assert (m["total_calls"], m["connected_calls"], m["last_call_at"]) == ("1", "1", "2026-10-03T09:00:00+05:30")
    assert r.leads()["N"]["last_call_at"] == "2026-10-05T23:59:59+05:30"
    assert r.leads()["O"]["last_call_at"] == "2026-10-06T00:30:00+05:30"
    assert r.rows("daily_overview.csv") == [
        ["motor_renewal", "2026-10-03", "1", "1", "1.0000", "0"],
        ["motor_renewal", "2026-10-05", "1", "0", "0.0000", "0"],
        ["motor_renewal", "2026-10-06", "1", "0", "0.0000", "0"],
    ]


# ------------------------------------------------------------ buckets

def test_buckets_and_batch_summary(w):
    refs = ["P", "Q", "R", "S", "T", "U", "V", "W", "Xr"]
    w.leads([f"Lead {c},{ph(i + 1)},{c},,," for i, c in enumerate(refs)])
    p = {c: ph(i + 1) for i, c in enumerate(refs)}
    d1, d2 = "2026-10-03 10:00:00", "2026-10-04 10:00:00"
    evs = []
    evs += w.call("c_Q1", p["Q"], d1, failed=True)
    evs += w.call("c_R1", p["R"], d1, failed=True) + w.call("c_R2", p["R"], d2, failed=True)
    evs += w.call("c_S1", p["S"], d1, failed=True) + w.call("c_S2", p["S"], d2, connected=True)
    evs += w.call("c_T1", p["T"], d1, failed=True) + w.call("c_T2", p["T"], d2, connected=True, disp="not_interested")
    evs += w.call("c_U1", p["U"], d1, connected=True, disp="callback_requested")
    evs += w.call("c_V1", p["V"], d1, connected=True, disp="renewed")
    evs.append(w.override("c_V1", "2026-10-03 12:00:00", "callback_requested"))        # terminal -> non-terminal
    evs += w.call("c_W1", p["W"], d1, connected=True, disp="callback_requested")
    evs.append(w.override("c_W1", "2026-10-03 12:00:00", "dnd"))                       # non-terminal -> terminal
    evs += w.call("c_X1", p["Xr"], d1, ended=True)                                     # rang out
    w.events(evs)
    w.ingest()
    r = w.report()
    bucket = {ref: x["bucket"] for ref, x in r.leads().items()}
    assert bucket == {
        "P": "yet_to_reach_out", "Q": "in_progress_telephony_failed", "R": "in_progress_telephony_failed",
        "S": "in_progress_dialed", "T": "completed", "U": "in_progress_dialed",
        "V": "in_progress_dialed", "W": "completed", "Xr": "in_progress_dialed"}
    assert r.leads()["V"]["ever_success"] == "false" and r.leads()["W"]["ever_success"] == "false"
    assert r.leads()["R"]["total_calls"] == "2" and r.leads()["R"]["connected_calls"] == "0"
    assert r.rows("batch_summary.csv") == [["motor_renewal", "batch_001", "9", "1", "2", "4", "2"]]


# ------------------------------------------------------------ rollups

def test_daily_rollup_rates_and_distinct_success_leads(w):
    w.leads([f"Lead R{i},{ph(51 + i)},R{i},,," for i in range(6)])
    w.leads([f"Lead O1,{ph(57)},O1,,,"], wf="policy_feedback", header=PF_HDR)
    evs = []
    for k in range(16):                         # 2026-10-07: 16 calls on R0..R3, 9 connected
        lead, connected = k // 4, k < 9
        disp = "renewed" if k in (0, 1, 4) else None     # R0 twice + R1 once: two distinct success leads
        evs += w.call(f"c_a{k}", ph(51 + lead), f"2026-10-07 10:{k:02d}:00", connected=connected,
                      disp=disp if connected else None)
    for k in range(3):                          # 2026-10-08: 3 calls, 1 connected
        evs += w.call(f"c_b{k}", ph(55), f"2026-10-08 10:{k:02d}:00", connected=(k == 0))
    for k in range(3):                          # 2026-10-09: 3 calls, 2 connected
        evs += w.call(f"c_c{k}", ph(56), f"2026-10-09 10:{k:02d}:00", connected=(k < 2))
    evs += w.call("c_pf", ph(57), "2026-10-07 10:00:00", connected=True, disp="feedback_captured", wf="policy_feedback")
    w.events(evs)
    w.ingest()
    r = w.report()
    assert r.rows("daily_overview.csv") == [
        ["motor_renewal", "2026-10-07", "16", "9", "0.5625", "2"],
        ["motor_renewal", "2026-10-08", "3", "1", "0.3333", "0"],
        ["motor_renewal", "2026-10-09", "3", "2", "0.6667", "0"],
        ["policy_feedback", "2026-10-07", "1", "1", "1.0000", "1"],
    ]
    assert r.leads()["R0"]["total_calls"] == "4" and r.leads()["R0"]["ever_success"] == "true"


def test_current_disposition_skips_later_call_without_one(w):
    w.leads([f"Lead CC,{ph(71)},CC,,,", f"Lead DD,{ph(72)},DD,,,"])
    d1, d2 = "2026-10-03 10:00:00", "2026-10-04 10:00:00"
    w.events(
        w.call("c_cc1", ph(71), d1, connected=True, disp="callback_requested")
        + w.call("c_cc2", ph(71), d2, connected=True)
        + w.call("c_dd1", ph(72), d1, connected=True, disp="callback_requested")
        + w.call("c_dd2", ph(72), d2, connected=True, disp="payment_link_sent"))
    w.ingest()
    r = w.report()
    cc, dd = r.leads()["CC"], r.leads()["DD"]
    assert (cc["current_disposition"], cc["last_call_at"], cc["total_calls"], cc["connected_calls"]) == (
        "callback_requested", "2026-10-04T10:00:00+05:30", "2", "2")
    assert dd["current_disposition"] == "payment_link_sent"


# ------------------------------------------------------------ formatting and sorting

def test_exact_formatting_sorting_and_quoting(w):
    w.leads([f"Lead b2,{ph(61)},b-2,,,", f"Lead B1,{ph(62)},B-1,,,"], batch="batch_001")
    w.leads([f"Lead a3,{ph(63)},a-3,,,", f'Lead a4,{ph(64)},"A,4",,,'], batch="batch_002")
    w.leads([f"Lead o1,{ph(65)},o-1,,,"], wf="policy_feedback", header=PF_HDR)
    w.ingest()
    r = w.report()
    last4 = lambda n: ph(n)[-4:]
    assert r.text("lead_report.csv") == "\n".join([
        "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,"
        "current_disposition,ever_success",
        f'motor_renewal,"A,4",batch_002,+91******{last4(64)},yet_to_reach_out,0,0,,,false',   # codepoint: A < B < a < b
        f"motor_renewal,B-1,batch_001,+91******{last4(62)},yet_to_reach_out,0,0,,,false",
        f"motor_renewal,a-3,batch_002,+91******{last4(63)},yet_to_reach_out,0,0,,,false",
        f"motor_renewal,b-2,batch_001,+91******{last4(61)},yet_to_reach_out,0,0,,,false",
        f"policy_feedback,o-1,batch_001,+91******{last4(65)},yet_to_reach_out,0,0,,,false",
    ]) + "\n"
    assert r.text("batch_summary.csv") == "\n".join([
        "workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed",
        "motor_renewal,batch_001,2,2,0,0,0",
        "motor_renewal,batch_002,2,2,0,0,0",
        "policy_feedback,batch_001,1,1,0,0,0",
    ]) + "\n"
    for name in FILES:
        raw = (r.out / name).read_bytes()          # bytes: read_text would hide CRLF
        assert b"\r" not in raw and raw.endswith(b"\n")
        raw.decode("utf-8")


def test_data_quality_and_rejections_exact(w):
    w.leads(["Ann,9876543210,P1,31/02/2026,,",            # row 1 valid, 1 warning
             "Ann2,+91 98765 43210,P1,,,Hindi",           # row 2 merged duplicate, 1 warning
             ",9876543211,P2,,,",                         # row 3 missing name
             "Bob,123,P3,,,"],                            # row 4 invalid phone
            batch="batch_001")
    w.leads([",9876543212,P4,,,"], batch="batch_002")     # row 1 missing name
    w.events([
        w.ev("call.initiated", "call_1", T0, "9876543210", eid="e1"),
        "{bad",
        w.ev("call.initiated", "call_1", T0, "9876543210", eid="e1"),                   # duplicate
        w.ev("call.initiated", "call_9", T0, "9000000000"),                             # orphan: no such lead
        w.ev("disposition.set", "call_1", plus(T0, 300), "9876543210", payload={"disposition_code": "RENEWD"}),
        w.override("call_zzz", plus(T0, 900), "renewed"),                               # orphan: nothing to link through
    ])
    w.ingest()
    r = w.report()
    assert r.dq() == {
        "lead_rows_total": 5, "lead_rows_rejected": 3,
        "rejections_by_reason": {"invalid_phone": 1, "missing_required:customer_name": 2},
        "duplicate_lead_rows_merged": 1, "field_warnings": 2,
        "events_total": 6, "malformed_events": 1, "duplicate_events_dropped": 1,
        "orphan_events": 2, "unknown_disposition_events": 1,
    }
    assert list(r.dq()) == ["lead_rows_total", "lead_rows_rejected", "rejections_by_reason",
                            "duplicate_lead_rows_merged", "field_warnings", "events_total", "malformed_events",
                            "duplicate_events_dropped", "orphan_events", "unknown_disposition_events"]
    assert r.text("data_quality.json").startswith('{\n  "lead_rows_total": 5,\n')
    assert r.text("rejections.csv") == (
        "workflow_id,batch_id,row_number,reason\n"
        "motor_renewal,batch_001,3,missing_required:customer_name\n"
        "motor_renewal,batch_001,4,invalid_phone\n"
        "motor_renewal,batch_002,1,missing_required:customer_name\n")


# ------------------------------------------------------------ empty ledger, stability, CLI

EMPTY_DQ = {"lead_rows_total": 0, "lead_rows_rejected": 0, "rejections_by_reason": {},
            "duplicate_lead_rows_merged": 0, "field_warnings": 0, "events_total": 0, "malformed_events": 0,
            "duplicate_events_dropped": 0, "orphan_events": 0, "unknown_disposition_events": 0}


def _assert_empty_reports(out):
    for name, header in HEADERS.items():
        assert (out / name).read_text(encoding="utf-8") == ",".join(header) + "\n"
    assert json.loads((out / "data_quality.json").read_text(encoding="utf-8")) == EMPTY_DQ
    assert '"rejections_by_reason": {}' in (out / "data_quality.json").read_text(encoding="utf-8")


def test_empty_initialised_ledger_writes_header_only_files(tmp_path):
    db = tmp_path / "e.db"
    conn = connect(db)
    init_db(conn)
    conn.close()
    generate_reports(db, tmp_path / "r")
    _assert_empty_reports(tmp_path / "r")


def test_db_file_without_tables_is_reported_empty_and_untouched(tmp_path):
    db = tmp_path / "blank.db"
    db.write_bytes(b"")
    generate_reports(db, tmp_path / "r")
    _assert_empty_reports(tmp_path / "r")
    assert db.stat().st_size == 0


def test_report_is_repeatable_read_only_and_leaves_other_files(w, tmp_path):
    w.leads([f"Lead A,{ph(1)},A,,,", f"Lead B,{ph(2)},B,,,"])
    w.events(w.call("c1", ph(1), T0, connected=True, disp="renewed") + w.call("c2", ph(2), T0, failed=True))
    w.ingest()
    sha = lambda: hashlib.sha256(w.db.read_bytes()).hexdigest()
    before = sha()
    out2 = tmp_path / "again"
    w.out.mkdir()
    (w.out / "notes.txt").write_text("keep me")
    (w.out / "lead_report.csv").write_text("stale")
    generate_reports(w.db, w.out)
    generate_reports(w.db, out2)
    assert sha() == before                                              # report never writes to the ledger
    for name in FILES:
        assert (w.out / name).read_bytes() == (out2 / name).read_bytes()
    assert (w.out / "notes.txt").read_text() == "keep me"
    assert not list(w.out.glob("*.tmp")) and not list(out2.glob("*.tmp"))
    assert sorted(p.name for p in out2.iterdir()) == sorted(FILES)


def test_cli_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEDGER_PII_KEY", "cli-key")
    world = World(tmp_path)
    world.conn.close()
    world.leads([f"Lead A,{ph(1)},A,,,"])
    world.events(world.call("c1", ph(1), T0, connected=True, disp="renewed"))
    db, out = str(tmp_path / "cli.db"), str(tmp_path / "cli_reports")
    assert main(["ingest", str(world.root), "--db", db]) == 0
    capsys.readouterr()
    assert main(["report", "--out", out, "--db", db]) == 0
    printed = capsys.readouterr().out
    assert all(name in printed for name in FILES)
    rows = Reports(out).rows("lead_report.csv")
    assert rows == [["motor_renewal", "A", "batch_001", "+91******0001", "completed", "1", "1", IST0, "renewed", "true"]]


def test_cli_report_on_missing_db_exits_1(tmp_path, capsys):
    assert main(["report", "--out", str(tmp_path / "r"), "--db", str(tmp_path / "nope.db")]) == 1
    assert not (tmp_path / "r").exists()
