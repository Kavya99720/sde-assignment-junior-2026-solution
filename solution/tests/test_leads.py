import json
import re
from pathlib import Path

import pytest

from solution.cli import main
from solution.db import connect, init_db
from solution.drops import find_drops
from solution.ingest_leads import FileChangedError, ingest_drop_leads
from solution.pii import PiiVault
from solution.schema import load_workflows
from solution.stats import lead_stats

SCHEMAS = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"
HDR = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language"


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    vault = PiiVault(b"test-key")
    wfs = load_workflows(SCHEMAS)
    root = tmp_path / "data"

    class Env:
        pass

    e = Env()
    e.conn, e.vault, e.wfs, e.root, e.db_path = conn, vault, wfs, root, tmp_path / "t.db"
    e.ingest = lambda drop="drop_1": ingest_drop_leads(conn, vault, wfs, root / drop)
    e.stats = lambda: lead_stats(conn)
    yield e
    conn.close()


def write_leads(root, lines, drop="drop_1", wf="motor_renewal", batch="batch_001", header=HDR):
    path = root / drop / "leads" / wf / f"{batch}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((header + "\r\n" + "\r\n".join(lines) + "\r\n").encode("utf-8"))
    return path


def get_lead(env, canonical_phone, wf="motor_renewal"):
    row = env.conn.execute("SELECT * FROM leads WHERE workflow_id = ? AND phone_hash = ?",
                           (wf, env.vault.phone_hash(canonical_phone))).fetchone()
    assert row is not None, f"no lead for {canonical_phone}"
    return {"batch_id": row["batch_id"], "ref": row["external_ref"], "last4": row["phone_last4"],
            "merged_rows": row["merged_rows"], "pii": env.vault.decrypt(row["pii_enc"]),
            "attrs": json.loads(row["attrs"])}


def rejections(env):
    return [tuple(r) for r in env.conn.execute(
        "SELECT row_number, reason FROM lead_rejections ORDER BY file_id, row_number")]


# ---------------------------------------------------------------- rejection

def test_reject_precedence_and_per_reason_counts(env):
    write_leads(env.root, [
        ",not-a-phone,P1,,,",          # 1 name missing beats bad phone
        "Ann,not-a-phone,,,,",         # 2 phone precedes reference: invalid_phone
        "Bob,,P3,,,",                  # 3 empty phone is missing, not invalid
        "Cy,9876543210,,,,",           # 4 reference missing
        "   ,9876543210,P5,,,",        # 5 whitespace-only name is missing
        "Di,9876543211,P6,,,",         # 6 valid
    ])
    (res,) = env.ingest()
    assert (res.rows_total, res.rows_rejected) == (6, 5)
    assert rejections(env) == [
        (1, "missing_required:customer_name"), (2, "invalid_phone"), (3, "missing_required:phone"),
        (4, "missing_required:policy_number"), (5, "missing_required:customer_name"),
    ]
    s = env.stats()
    assert s["rejections_by_reason"] == {
        "invalid_phone": 1, "missing_required:customer_name": 2,
        "missing_required:phone": 1, "missing_required:policy_number": 1}
    assert s["leads"] == 1 and s["lead_rows_rejected"] == 5


def test_rejected_rows_are_not_leads_and_keep_no_pii_columns(env):
    write_leads(env.root, ["Ann,12345,P1,,,"])
    env.ingest()
    assert env.stats()["leads"] == 0
    cols = [r[1] for r in env.conn.execute("PRAGMA table_info(lead_rejections)")]
    assert cols == ["file_id", "row_number", "reason"]


# ---------------------------------------------------------------- warnings

def test_field_warnings_are_per_invalid_optional_field(env):
    write_leads(env.root, [
        "Eve,9876543210,P1,31/02/2026,N/A,Hindi",   # 3 invalid optional fields -> 3 warnings
        "Fay,9876543211,P2,,,",                     # empty optional is just missing: 0
        "Gus,9876543212,P3,TBD,-,",                 # 2
        "Hal,9876543213,P4,2026-12-01,10,xx",       # 1 (bad language)
        ",9876543214,P5,TBD,N/A,Hindi",             # rejected row: its invalid fields are not counted
    ])
    (res,) = env.ingest()
    assert res.field_warnings == 6
    assert env.stats()["field_warnings"] == 6
    eve = get_lead(env, "+919876543210")["attrs"]
    assert eve["policy_expiry"] is None and eve["premium_amount"] is None
    assert eve["preferred_language"] == "en"       # invalid -> null -> default
    assert get_lead(env, "+919876543211")["attrs"]["preferred_language"] == "en"


# ---------------------------------------------------------------- blanks and rows

def test_blank_rows_not_counted_and_row_numbers_skip_them(env):
    write_leads(env.root, [
        ",,,,,",
        "Hal,9876543210,P1,,,",        # row 1
        "  , ,,,, ",
        ",,,,,",
        "Ivy,12345,P2,,,",             # row 2 (blanks above do not count)
    ])
    (res,) = env.ingest()
    assert res.rows_total == 2
    assert rejections(env) == [(2, "invalid_phone")]


def test_row_with_only_an_ignored_column_is_not_blank(env):
    write_leads(env.root, [",,,VIP"], header="customer_name,phone,policy_number,agent_notes")
    (res,) = env.ingest()
    assert res.rows_total == 1
    assert rejections(env) == [(1, "missing_required:customer_name")]


def test_alias_headers_and_extra_column(env):
    write_leads(
        env.root,
        ['HI,Jo Kim,POL/1,+91 98765 43210,01-Dec-2026,"₹1,20,000",VIP'],
        header="Language,Insured Name,Policy No,Mobile No,Expiry Date,Premium,agent_notes")
    env.ingest()
    lead = get_lead(env, "+919876543210")
    assert lead["ref"] == "POL/1" and lead["last4"] == "3210"
    assert lead["pii"] == {"customer_name": "Jo Kim", "phone": "+919876543210"}
    assert lead["attrs"] == {"policy_expiry": "2026-12-01", "premium_amount": "120000.00",
                             "preferred_language": "hi"}


def test_duplicate_mapped_columns_leftmost_wins(env):
    write_leads(env.root, ["Kay,9876543210,9111111111,P1"], header="customer_name,phone,contact,policy_number")
    env.ingest()
    assert get_lead(env, "+919876543210")["ref"] == "P1"
    assert env.stats()["leads"] == 1


def test_phone_formats_resolve_to_one_identity_within_workflow_only(env):
    write_leads(env.root, ["A,9876543210,P1,,,", "B,+91 98765 43210,P2,,,", "C,09876543210,P3,,,"])
    write_leads(env.root, ["D,91-98765-43210,O1,,,"], wf="policy_feedback", header="name,mobile,order id,item,order date,language")
    env.ingest()
    assert env.stats()["leads"] == 2          # one per workflow
    assert env.stats()["duplicate_lead_rows_merged"] == 2


# ---------------------------------------------------------------- merge

def test_merge_rules(env):
    write_leads(env.root, [
        "Lee,9876543210,P1,2026-12-01,1000,hi",
        "Mo,9876543211,P2,,,",
        "Nia,9876543212,P3,,,",
    ], batch="batch_001")
    write_leads(env.root, [
        "Lee Two,+91 98765 43210,P1B,,,Hindi",             # name/ref overwrite; blanks and bad lang do not
        "Mo,09876543211,P2,05/06/2026,\"2,500.5\",TA",     # fills blanks; language replaces the default
        "Nia,9876543212,P3,,,",                            # nothing new: default stays
        ",9876543210,PX,,9999,en",                         # rejected row must not merge anything
    ], batch="batch_002")
    r1, r2 = env.ingest()
    assert (r1.rows_merged, r2.rows_merged, r2.rows_rejected, r2.field_warnings) == (0, 3, 1, 1)

    lee = get_lead(env, "+919876543210")
    assert lee["batch_id"] == "batch_001"                  # first batch kept forever
    assert lee["pii"]["customer_name"] == "Lee Two" and lee["ref"] == "P1B"
    assert lee["attrs"] == {"policy_expiry": "2026-12-01", "premium_amount": "1000.00",
                            "preferred_language": "hi"}    # invalid optional did not overwrite
    mo = get_lead(env, "+919876543211")
    assert mo["batch_id"] == "batch_001"
    assert mo["attrs"] == {"policy_expiry": "2026-06-05", "premium_amount": "2500.50",
                           "preferred_language": "ta"}
    assert get_lead(env, "+919876543212")["attrs"]["preferred_language"] == "en"   # default after merge
    s = env.stats()
    assert s["leads"] == 3 and s["duplicate_lead_rows_merged"] == 3 and s["merged_rows_on_leads"] == 3


def test_duplicate_within_same_file(env):
    write_leads(env.root, ["A,9876543210,P1,,100,en", "A2,9876543210,P1,,200,"])
    (res,) = env.ingest()
    assert res.rows_merged == 1
    lead = get_lead(env, "+919876543210")
    assert lead["pii"]["customer_name"] == "A2" and lead["attrs"]["premium_amount"] == "200.00"


def test_files_processed_in_filename_order(env):
    write_leads(env.root, ["Second,9876543210,P2,,,"], batch="batch_002")
    write_leads(env.root, ["First,9876543210,P1,,,"], batch="batch_001")
    r = env.ingest()
    assert [x.rel_path for x in r] == ["leads/motor_renewal/batch_001.csv", "leads/motor_renewal/batch_002.csv"]
    lead = get_lead(env, "+919876543210")
    assert lead["batch_id"] == "batch_001" and lead["pii"]["customer_name"] == "Second"


# ---------------------------------------------------------------- idempotency / safety

def test_reingest_same_file_changes_nothing(env):
    write_leads(env.root, ["A,9876543210,P1,,,", "A2,9876543210,P1,,,", ",1,P2,,,"])
    first = env.ingest()
    assert first[0].status == "ingested"
    before = list(env.conn.iterdump())
    stats_before = env.stats()
    second = env.ingest()
    assert [r.status for r in second] == ["skipped"]
    assert list(env.conn.iterdump()) == before
    assert env.stats() == stats_before


def test_changed_file_is_refused(env):
    path = write_leads(env.root, ["A,9876543210,P1,,,"])
    env.ingest()
    before = list(env.conn.iterdump())
    write_leads(env.root, ["A,9876543210,P1,,,", "B,9876543211,P2,,,"])
    with pytest.raises(FileChangedError):
        env.ingest()
    assert list(env.conn.iterdump()) == before
    assert path.exists()


def test_failure_mid_file_rolls_back_everything(env, monkeypatch):
    write_leads(env.root, [",1,P0,,,", "A,9876543210,P1,,,", "B,9876543211,P2,,,"])
    real = env.vault.encrypt
    calls = {"n": 0}

    def flaky(data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real(data)

    monkeypatch.setattr(env.vault, "encrypt", flaky)
    with pytest.raises(RuntimeError):
        env.ingest()
    assert not env.conn.in_transaction
    for table in ("leads", "lead_rejections", "ingest_files"):
        assert env.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    monkeypatch.undo()
    (res,) = env.ingest()
    assert (res.status, res.rows_total, res.rows_rejected) == ("ingested", 3, 1)


def test_unknown_workflow_folder_is_skipped_and_reported(env):
    write_leads(env.root, ["A,9876543210,P1,,,"])
    write_leads(env.root, ["X,9876543211,P9,,,"], wf="mystery")
    results = env.ingest()
    statuses = {r.rel_path: r.status for r in results}
    assert statuses["leads/mystery"] == "unknown_workflow"
    assert statuses["leads/motor_renewal/batch_001.csv"] == "ingested"
    assert env.conn.execute("SELECT COUNT(*) FROM ingest_files").fetchone()[0] == 1


def test_bom_and_ragged_rows(env):
    path = env.root / "drop_1" / "leads" / "motor_renewal" / "batch_001.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(("﻿" + HDR + "\n" + "A,9876543210,P1\n").encode("utf-8"))   # short row, BOM, LF
    (res,) = env.ingest()
    assert res.rows_total == 1 and res.rows_rejected == 0
    assert get_lead(env, "+919876543210")["attrs"]["preferred_language"] == "en"


def test_empty_and_header_only_files(env):
    p = env.root / "drop_1" / "leads" / "motor_renewal"
    p.mkdir(parents=True)
    (p / "batch_001.csv").write_bytes(b"")
    (p / "batch_002.csv").write_bytes((HDR + "\n").encode())
    r = env.ingest()
    assert [x.rows_total for x in r] == [0, 0]
    assert env.stats()["lead_files_ingested"] == 2


# ---------------------------------------------------------------- PII at rest

def test_no_name_or_raw_phone_in_db_bytes(env):
    names = ["Zelda Quintero", "Xavier Ostrowski", "Wanda Pellegrino", "Rejected Rosalind"]
    raw_phones = ["+91 91234 56780", "09123456781", "91-91234-56782", "(+91) 9123456783"]
    write_leads(env.root, [
        f"{names[0]},{raw_phones[0]},POL/9,,,",
        f"{names[1]},{raw_phones[1]},POL/8,,,",
        f"{names[2]},{raw_phones[2]},POL/7,,,",
        f"{names[0]} Jr,{raw_phones[3]},POL/6,,,",
        f"{names[3]},98765,POL/5,,,",              # rejected: name must not be stored either
    ])
    write_leads(env.root, [f"Yolanda Merged,{raw_phones[0]},POL/9,,,"], batch="batch_002")   # merge path
    env.ingest()
    env.conn.close()
    blobs = [p.read_bytes() for p in env.db_path.parent.glob("t.db*")]
    data = b"".join(blobs)
    assert b"POL/9" in data                                # control: the scan sees real content
    needles = names + ["Zelda", "Quintero", "Ostrowski", "Pellegrino", "Rosalind", "Yolanda", "Merged"]
    for raw in raw_phones:
        digits = "".join(c for c in raw if c.isdigit())
        ten = digits[-10:]
        needles += [raw, ten, "+91" + ten, f"{ten[:5]} {ten[5:]}", f"{ten[:5]}-{ten[5:]}", "0" + ten]
    for n in needles:
        assert n.encode("utf-8") not in data, f"plaintext PII found in db bytes: {n!r}"


# ---------------------------------------------------------------- drops and CLI

def test_find_drops_natural_order(tmp_path):
    for n in ("drop_10", "drop_2", "drop_1", "notes"):
        (tmp_path / n).mkdir()
    assert [p.name for p in find_drops(tmp_path)] == ["drop_1", "drop_2", "drop_10"]
    assert [p.name for p in find_drops(tmp_path / "drop_2")] == ["drop_2"]


def test_cli_ingest_parent_then_stats(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEDGER_PII_KEY", "cli-key")
    root = tmp_path / "data"
    write_leads(root, ["A,9876543210,P1,,,", "Z,1,P2,,,"], drop="drop_2", batch="batch_003")
    write_leads(root, ["B,9876543211,P3,,,"], drop="drop_10", batch="batch_004")
    write_leads(root, ["C,9876543212,P4,,,"], drop="drop_1", batch="batch_001")
    db = str(tmp_path / "cli.db")

    assert main(["ingest", str(root), "--db", db]) == 0
    out = capsys.readouterr().out
    order = [out.index(f"{d}/leads") for d in ("drop_1", "drop_2", "drop_10")]
    assert order == sorted(order)
    assert sum(l.startswith("ingested") for l in out.splitlines()) == 3

    assert main(["ingest", str(root), "--db", db]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert sum(l.startswith("skipped") for l in lines) == 3
    assert not any(l.startswith("ingested") for l in lines)

    assert main(["stats", "--db", db]) == 0
    out = capsys.readouterr().out
    assert re.search(r"unique leads:\s+3\b", out) and "invalid_phone: 1" in out


def test_cli_single_drop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEDGER_PII_KEY", "cli-key")
    write_leads(tmp_path / "data", ["A,9876543210,P1,,,"])
    assert main(["ingest", str(tmp_path / "data" / "drop_1"), "--db", str(tmp_path / "x.db")]) == 0
    assert "ingested  drop_1/leads/motor_renewal/batch_001.csv" in capsys.readouterr().out


def test_cli_changed_file_and_unknown_workflow_exit_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEDGER_PII_KEY", "cli-key")
    root = tmp_path / "data"
    write_leads(root, ["A,9876543210,P1,,,"])
    db = str(tmp_path / "x.db")
    assert main(["ingest", str(root), "--db", db]) == 0
    write_leads(root, ["A,9876543210,P1,,,", "B,9876543211,P2,,,"])
    assert main(["ingest", str(root), "--db", db]) == 1
    assert "different content" in capsys.readouterr().err

    write_leads(tmp_path / "data2", ["X,9876543210,P1,,,"], wf="mystery")
    assert main(["ingest", str(tmp_path / "data2"), "--db", str(tmp_path / "y.db")]) == 1
    assert "no schema" in capsys.readouterr().err


def test_cli_refuses_db_made_with_another_key(tmp_path, monkeypatch, capsys):
    root = tmp_path / "data"
    write_leads(root, ["A,9876543210,P1,,,"])
    db = str(tmp_path / "x.db")
    monkeypatch.setenv("LEDGER_PII_KEY", "key-a")
    assert main(["ingest", str(root), "--db", db]) == 0
    monkeypatch.setenv("LEDGER_PII_KEY", "key-b")
    assert main(["ingest", str(root), "--db", db]) == 1
    assert "mismatch" in capsys.readouterr().err


def test_cli_stats_on_missing_db(tmp_path, capsys):
    assert main(["stats", "--db", str(tmp_path / "nope.db")]) == 1
