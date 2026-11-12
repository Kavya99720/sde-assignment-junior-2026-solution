"""Reports on generated data: bulk == incremental, repeatable, key-independent, read-only, sane."""
import csv
import hashlib
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from solution.cli import main
from solution.report import FILES

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "assignment-a-data" / "tools" / "generate_data.py"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows(folder, name):
    text = (Path(folder) / name).read_text(encoding="utf-8")
    return list(csv.reader(io.StringIO(text, newline="")))[1:]


def dq(folder):
    return json.loads((Path(folder) / "data_quality.json").read_text(encoding="utf-8"))


def run(*argv):
    assert main(list(argv)) == 0


@pytest.fixture(scope="module", params=[42, 7], ids=["seed42", "seed7"])
def runs(request, tmp_path_factory):
    seed = request.param
    base = tmp_path_factory.mktemp(f"seed{seed}")
    data = base / "data"
    subprocess.run([sys.executable, str(GENERATOR), "--seed", str(seed), "--scale", "small", "--out", str(data)],
                   check=True, capture_output=True)
    mp = pytest.MonkeyPatch()
    mp.setenv("LEDGER_PII_KEY", f"equiv-key-a-{seed}")
    try:
        inc = base / "inc.db"
        run("ingest", str(data / "drop_1"), "--db", str(inc))
        run("report", "--out", str(base / "after_drop1"), "--db", str(inc))
        run("ingest", str(data / "drop_2"), "--db", str(inc))
        sha_before = sha(inc)
        run("report", "--out", str(base / "inc"), "--db", str(inc))
        sha_after = sha(inc)
        run("report", "--out", str(base / "inc_again"), "--db", str(inc))

        bulk = base / "bulk.db"
        run("ingest", str(data), "--db", str(bulk))
        run("report", "--out", str(base / "bulk"), "--db", str(bulk))

        mp.setenv("LEDGER_PII_KEY", f"equiv-key-b-{seed}")
        other = base / "other.db"
        run("ingest", str(data), "--db", str(other))
        run("report", "--out", str(base / "other_key"), "--db", str(other))
    finally:
        mp.undo()
    return SimpleNamespace(base=base, sha_before=sha_before, sha_after=sha_after,
                           **{n: base / n for n in ("after_drop1", "inc", "inc_again", "bulk", "other_key")})


def test_bulk_equals_incremental_byte_for_byte(runs):
    for name in FILES:
        assert (runs.inc / name).read_bytes() == (runs.bulk / name).read_bytes(), name


def test_report_twice_is_byte_equal(runs):
    for name in FILES:
        assert (runs.inc / name).read_bytes() == (runs.inc_again / name).read_bytes(), name


def test_reports_do_not_depend_on_the_pii_key(runs):
    for name in FILES:
        assert (runs.bulk / name).read_bytes() == (runs.other_key / name).read_bytes(), name


def test_report_does_not_modify_the_ledger(runs):
    assert runs.sha_before == runs.sha_after


def test_no_temp_files_and_exactly_five_outputs(runs):
    assert sorted(p.name for p in runs.inc.iterdir()) == sorted(FILES)


def test_orphans_resolve_once_their_leads_arrive(runs):
    after_1, final = dq(runs.after_drop1), dq(runs.inc)
    assert final["orphan_events"] > 0                                   # never-uploaded numbers stay orphans
    assert after_1["orphan_events"] > final["orphan_events"]            # drop_1 events of batch_003 leads
    assert len(rows(runs.after_drop1, "lead_report.csv")) < len(rows(runs.inc, "lead_report.csv"))


def test_invariants(runs):
    leads, batches = rows(runs.inc, "lead_report.csv"), rows(runs.inc, "batch_summary.csv")
    daily, rejections, quality = rows(runs.inc, "daily_overview.csv"), rows(runs.inc, "rejections.csv"), dq(runs.inc)

    assert sum(int(b[2]) for b in batches) == len(leads)
    assert all(int(b[2]) == sum(int(x) for x in b[3:7]) for b in batches)
    assert [b[:2] for b in batches] == sorted(b[:2] for b in batches)

    assert quality["lead_rows_total"] - quality["lead_rows_rejected"] - quality["duplicate_lead_rows_merged"] == len(leads)
    assert len(rejections) == quality["lead_rows_rejected"] == sum(quality["rejections_by_reason"].values())
    assert list(quality["rejections_by_reason"]) == sorted(quality["rejections_by_reason"])
    assert quality["malformed_events"] > 0 and quality["duplicate_events_dropped"] > 0
    assert quality["unknown_disposition_events"] > 0 and quality["field_warnings"] > 0

    assert sum(int(d[2]) for d in daily) == sum(int(r[5]) for r in leads)
    assert sum(int(d[3]) for d in daily) == sum(int(r[6]) for r in leads)
    assert all(re.fullmatch(r"\d\.\d{4}", d[4]) for d in daily)
    assert [d[:2] for d in daily] == sorted(d[:2] for d in daily)

    assert [r[:2] for r in leads] == sorted(r[:2] for r in leads)
    assert {r[4] for r in leads} == {"yet_to_reach_out", "in_progress_telephony_failed",
                                     "in_progress_dialed", "completed"}
    for r in leads:
        assert re.fullmatch(r"\+91\*{6}\d{4}", r[3])
        assert r[9] in ("true", "false")
        assert (r[7] == "") == (r[5] == "0")                            # last_call_at empty iff no calls
        assert r[7] == "" or r[7].endswith("+05:30")
        assert (r[4] == "yet_to_reach_out") == (r[5] == "0")
        assert int(r[6]) <= int(r[5])
