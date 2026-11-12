"""Command line: `python -m solution ingest <dir>` and `python -m solution stats`."""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

from .db import PiiKeyMismatch, check_pii_fingerprint, connect, init_db, load_dispositions
from .drops import find_drops
from .ingest_events import ingest_drop_events
from .ingest_leads import IngestError, ingest_drop_leads
from .pii import load_vault
from .schema import load_dispositions as read_dispositions
from .schema import load_workflows
from .stats import event_stats, format_event_stats, format_lead_stats, lead_stats

DEFAULT_SCHEMAS = Path(__file__).resolve().parents[1] / "assignment-a-data" / "schemas"


def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--db", default=os.environ.get("LEDGER_DB", "ledger.db"),
                   help="SQLite file (default: ledger.db, env LEDGER_DB)")
    p.add_argument("--schemas", default=str(DEFAULT_SCHEMAS),
                   help="folder with workflow schemas and dispositions.csv")
    return p


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m solution", description="Interaction ledger pipeline")
    sub = ap.add_subparsers(dest="command", required=True)
    ing = sub.add_parser("ingest", parents=[_common()], help="ingest one drop, or a folder of drop_* folders")
    ing.add_argument("path", help="a drop folder, or a parent containing drop_* folders")
    sub.add_parser("stats", parents=[_common()], help="print counts from the database")
    return ap


def cmd_ingest(args) -> int:
    schemas = Path(args.schemas)
    workflows = load_workflows(schemas)
    dispositions = read_dispositions(schemas / "dispositions.csv")
    vault = load_vault()
    conn = connect(args.db)
    try:
        init_db(conn)
        check_pii_fingerprint(conn, vault.fingerprint())
        load_dispositions(conn, dispositions)
        exit_code = 0
        for drop in find_drops(Path(args.path)):
            # leads first, then events: a drop's events can link to the drop's own leads
            results = ingest_drop_leads(conn, vault, workflows, drop)
            results += ingest_drop_events(conn, vault, drop)
            for r in results:
                where = f"{r.drop_name}/{r.rel_path}"
                if r.status == "ingested" and r.kind == "events":
                    stored = r.lines_total - r.malformed - r.duplicates
                    print(f"ingested  {where}  lines={r.lines_total} malformed={r.malformed} "
                          f"duplicates={r.duplicates} stored={stored}")
                elif r.status == "ingested":
                    print(f"ingested  {where}  rows={r.rows_total} rejected={r.rows_rejected} "
                          f"merged={r.rows_merged} warnings={r.field_warnings}")
                elif r.status == "skipped":
                    print(f"skipped   {where}  (already ingested, identical content)")
                else:
                    print(f"ERROR     {where}  no schema for this workflow folder; skipped", file=sys.stderr)
                    exit_code = 1
        return exit_code
    finally:
        conn.close()


def cmd_stats(args) -> int:
    if not Path(args.db).exists():
        print(f"error: database {args.db} does not exist", file=sys.stderr)
        return 1
    conn = connect(args.db)
    try:
        init_db(conn)
        print(format_lead_stats(lead_stats(conn)))
        print(format_event_stats(event_stats(conn)))
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return {"ingest": cmd_ingest, "stats": cmd_stats}[args.command](args)
    except (IngestError, PiiKeyMismatch, FileNotFoundError, csv.Error, UnicodeDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
