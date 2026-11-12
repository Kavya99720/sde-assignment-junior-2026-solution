"""Counts for the `stats` command."""
from __future__ import annotations

import sqlite3


def lead_stats(conn: sqlite3.Connection) -> dict:
    files = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(rows_total),0), COALESCE(SUM(rows_rejected),0), "
        "COALESCE(SUM(rows_merged),0), COALESCE(SUM(field_warnings),0) "
        "FROM ingest_files WHERE kind = 'leads'").fetchone()
    reasons = conn.execute(
        "SELECT reason, COUNT(*) FROM lead_rejections GROUP BY reason ORDER BY reason").fetchall()
    return {
        "lead_files_ingested": files[0],
        "lead_rows_total": files[1],
        "lead_rows_rejected": files[2],
        "duplicate_lead_rows_merged": files[3],
        "field_warnings": files[4],
        "rejections_by_reason": {r[0]: r[1] for r in reasons},
        "leads": conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
        "rejection_rows": conn.execute("SELECT COUNT(*) FROM lead_rejections").fetchone()[0],
        "merged_rows_on_leads": conn.execute("SELECT COALESCE(SUM(merged_rows),0) FROM leads").fetchone()[0],
    }


def event_stats(conn: sqlite3.Connection) -> dict:
    files = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(lines_total),0), COALESCE(SUM(malformed),0), "
        "COALESCE(SUM(duplicates),0) FROM ingest_files WHERE kind = 'events'").fetchone()
    return {
        "event_files_ingested": files[0],
        "events_total": files[1],
        "malformed_events": files[2],
        "duplicate_events_dropped": files[3],
        "events_stored": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
    }


def format_event_stats(s: dict) -> str:
    lines = [
        f"event files ingested:       {s['event_files_ingested']}",
        f"events total (lines):       {s['events_total']}",
        f"malformed events:           {s['malformed_events']}",
        f"duplicate events dropped:   {s['duplicate_events_dropped']}",
        f"events stored in ledger:    {s['events_stored']}",
    ]
    if s["events_total"] - s["malformed_events"] - s["duplicate_events_dropped"] != s["events_stored"]:
        lines.append("WARNING: stored events disagree with per-file counters")
    return "\n".join(lines)


def format_lead_stats(s: dict) -> str:
    lines = [
        f"lead files ingested:        {s['lead_files_ingested']}",
        f"lead rows total:            {s['lead_rows_total']}",
        f"lead rows rejected:         {s['lead_rows_rejected']}",
        "rejections by reason:" + ("" if s["rejections_by_reason"] else " (none)"),
        *[f"  {k}: {v}" for k, v in s["rejections_by_reason"].items()],
        f"unique leads:               {s['leads']}",
        f"duplicate rows merged:      {s['duplicate_lead_rows_merged']}",
        f"field warnings:             {s['field_warnings']}",
    ]
    if (s["rejection_rows"] != s["lead_rows_rejected"]
            or s["merged_rows_on_leads"] != s["duplicate_lead_rows_merged"]):
        lines.append("WARNING: table counts disagree with per-file counters")
    return "\n".join(lines)
