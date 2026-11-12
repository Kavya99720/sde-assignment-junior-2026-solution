"""Build the five report files from the ledger. Read-only on the ledger database."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from pathlib import Path

from . import report_sql as q
from .db import connect, init_db
from .normalise import ist_iso

HEADERS = {
    "batch_summary.csv": ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
                          "in_progress_telephony_failed", "in_progress_dialed", "completed"],
    "daily_overview.csv": ["workflow_id", "date", "total_calls", "connected_calls",
                           "connectivity_rate", "success_leads"],
    "lead_report.csv": ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                        "connected_calls", "last_call_at", "current_disposition", "ever_success"],
    "rejections.csv": ["workflow_id", "batch_id", "row_number", "reason"],
}
FILES = [*HEADERS, "data_quality.json"]


def _open_ledger(db_path: Path) -> sqlite3.Connection:
    """The ledger, or a blank in-memory one if the file has no ledger tables yet (empty report)."""
    conn = connect(db_path)
    has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ingest_files'").fetchone()
    if has_tables:
        return conn
    conn.close()
    blank = connect(":memory:")
    init_db(blank)
    return blank


def collect(db_path: Path) -> dict:
    """Run every query inside one read transaction and return plain Python values."""
    conn = _open_ledger(Path(db_path))
    try:
        conn.execute("BEGIN")
        conn.execute("SELECT COUNT(*) FROM ingest_files").fetchone()      # take the read snapshot now
        for stmt in q.SETUP:
            conn.execute(stmt)
        totals = tuple(conn.execute(q.Q_INGEST_TOTALS).fetchone())
        reasons = {r[0]: r[1] for r in conn.execute(q.Q_REASONS)}
        out = {
            "batch": [tuple(r) for r in conn.execute(q.Q_BATCH_SUMMARY)],
            "daily": [tuple(r) for r in conn.execute(q.Q_DAILY)],
            "leads": [tuple(r) for r in conn.execute(q.Q_LEAD_REPORT)],
            "rejections": [tuple(r) for r in conn.execute(q.Q_REJECTIONS)],
            "orphans": conn.execute(q.Q_ORPHANS).fetchone()[0],
            "unknown": conn.execute(q.Q_UNKNOWN).fetchone()[0],
            "totals": totals,
            "reasons": reasons,
        }
        conn.execute("ROLLBACK")
        return out
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def _batch_rows(rows):
    return [list(r) for r in rows]


def _daily_rows(rows):
    return [[wf, day, total, connected, f"{connected / total:.4f}", success]
            for wf, day, total, connected, success in rows]


def _lead_rows(rows):
    out = []
    for wf, ref, batch, last4, bucket, total, connected, last_at, code, ever in rows:
        out.append([wf, ref, batch, "+91******" + last4, bucket, total, connected,
                    ist_iso(last_at) if last_at is not None else "",
                    code if code is not None else "",
                    "true" if ever else "false"])
    return out


def data_quality(data: dict) -> dict:
    lead_total, lead_rej, merged, warnings, ev_total, malformed, dups = data["totals"]
    return {
        "lead_rows_total": lead_total,
        "lead_rows_rejected": lead_rej,
        "rejections_by_reason": {k: data["reasons"][k] for k in sorted(data["reasons"])},
        "duplicate_lead_rows_merged": merged,
        "field_warnings": warnings,
        "events_total": ev_total,
        "malformed_events": malformed,
        "duplicate_events_dropped": dups,
        "orphan_events": data["orphans"],
        "unknown_disposition_events": data["unknown"],
    }


def _write_atomic(path: Path, write) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            write(fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    def write(fh):
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    _write_atomic(path, write)


def generate_reports(db_path: Path, out_dir: Path) -> list[Path]:
    """Write the five report files into out_dir (overwriting those five only). Returns their paths."""
    data = collect(Path(db_path))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tables = {
        "batch_summary.csv": _batch_rows(data["batch"]),
        "daily_overview.csv": _daily_rows(data["daily"]),
        "lead_report.csv": _lead_rows(data["leads"]),
        "rejections.csv": [list(r) for r in data["rejections"]],
    }
    paths = []
    for name, header in HEADERS.items():
        _write_csv(out_dir / name, header, tables[name])
        paths.append(out_dir / name)
    dq_path = out_dir / "data_quality.json"            # written last
    _write_atomic(dq_path, lambda fh: fh.write(
        json.dumps(data_quality(data), indent=2, ensure_ascii=False) + "\n"))
    paths.append(dq_path)
    return paths
