"""Lead CSV ingestion: validate, normalise, reject, create/merge leads. One transaction per file."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .normalise import normalise_phone, parse_date, parse_decimal, parse_enum
from .pii import PiiVault
from .schema import Field, Workflow

log = logging.getLogger(__name__)


class IngestError(RuntimeError):
    pass


class FileChangedError(IngestError):
    """A file already ingested under this drop/path now has different content."""


@dataclass
class FileResult:
    status: str                # "ingested" | "skipped" | "unknown_workflow"
    drop_name: str
    rel_path: str
    rows_total: int = 0
    rows_rejected: int = 0
    rows_merged: int = 0
    field_warnings: int = 0
    kind: str = "leads"        # "leads" | "events"
    lines_total: int = 0       # events only
    malformed: int = 0
    duplicates: int = 0


def _parse_value(f: Field, raw: str):
    if f.type == "string":
        return raw
    if f.type == "phone":
        return normalise_phone(raw)
    if f.type == "date":
        d = parse_date(raw)
        return d.isoformat() if d else None
    if f.type == "decimal":
        d = parse_decimal(raw)
        return str(d) if d is not None else None
    if f.type == "enum":
        return parse_enum(raw, list(f.values))
    raise ValueError(f"unsupported field type {f.type!r} for {f.name}")


def validate_row(wf: Workflow, colmap: dict[str, int], row: list[str]):
    """Returns (values, reject_reason, warnings). Walks fields in schema order; first failure wins."""
    values: dict[str, str | None] = {}
    warnings = 0
    for f in wf.fields:
        idx = colmap.get(f.name)
        raw = row[idx].strip() if idx is not None and idx < len(row) else ""
        if raw == "":
            if f.required:
                return None, f"missing_required:{f.name}", 0
            values[f.name] = None
            continue
        parsed = _parse_value(f, raw)
        if parsed is None:
            if f.type == "phone":
                return None, "invalid_phone", 0
            if f.required:
                return None, f"invalid_value:{f.name}", 0
            warnings += 1
        values[f.name] = parsed
    return values, None, warnings


def _column_map(wf: Workflow, header: list[str], source: str) -> dict[str, int]:
    colmap: dict[str, int] = {}
    for idx, h in enumerate(header):
        f = wf.match_header(h)
        if f is None:
            continue
        if f.name in colmap:
            log.warning("%s: header %r also maps to %s; keeping the leftmost column", source, h, f.name)
            continue
        colmap[f.name] = idx
    return colmap


def _split(wf: Workflow, merged: dict) -> tuple[dict, dict, str]:
    pii = {f.name: merged.get(f.name) for f in wf.pii_fields}
    attrs = {f.name: merged.get(f.name) for f in wf.fields if not f.pii and not f.reference}
    return pii, attrs, merged[wf.reference_field.name]


def _upsert_lead(conn: sqlite3.Connection, vault: PiiVault, wf: Workflow, file_id: int,
                 batch_id: str, values: dict) -> bool:
    """Create the lead or merge into it. Returns True when it was a merge."""
    phone = values[wf.phone_field.name]
    phone_hash = vault.phone_hash(phone)
    existing = conn.execute(
        "SELECT lead_id, external_ref, pii_enc, attrs FROM leads WHERE workflow_id = ? AND phone_hash = ?",
        (wf.workflow_id, phone_hash)).fetchone()

    if existing is None:
        merged = dict(values)
    else:
        merged = {**vault.decrypt(existing["pii_enc"]), **json.loads(existing["attrs"]),
                  wf.reference_field.name: existing["external_ref"]}
        merged.update({k: v for k, v in values.items() if v is not None})   # last non-null wins
    for f in wf.fields:                                                      # defaults after merging
        if f.default is not None and merged.get(f.name) is None:
            merged[f.name] = f.default

    pii, attrs, ref = _split(wf, merged)
    pii_enc = vault.encrypt(pii)
    attrs_json = json.dumps(attrs, sort_keys=True, ensure_ascii=False)
    if existing is None:
        conn.execute(
            "INSERT INTO leads (workflow_id, phone_hash, batch_id, first_file_id, external_ref, phone_last4, "
            "pii_enc, attrs) VALUES (?,?,?,?,?,?,?,?)",
            (wf.workflow_id, phone_hash, batch_id, file_id, ref, phone[-4:], pii_enc, attrs_json))
        return False
    conn.execute(
        "UPDATE leads SET external_ref = ?, pii_enc = ?, attrs = ?, merged_rows = merged_rows + 1 "
        "WHERE lead_id = ?", (ref, pii_enc, attrs_json, existing["lead_id"]))
    return True


def ingest_lead_file(conn: sqlite3.Connection, vault: PiiVault, wf: Workflow, path: Path,
                     drop_name: str, rel_path: str) -> FileResult:
    path = Path(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    batch_id = path.stem
    conn.execute("BEGIN IMMEDIATE")
    try:
        prior = conn.execute("SELECT sha256 FROM ingest_files WHERE drop_name = ? AND rel_path = ?",
                             (drop_name, rel_path)).fetchone()
        if prior is not None:
            conn.execute("ROLLBACK")
            if prior["sha256"] == sha:
                return FileResult("skipped", drop_name, rel_path)
            raise FileChangedError(
                f"{drop_name}/{rel_path} was already ingested with different content; refusing to re-ingest")

        # Explicit id so rows can reference it before its ingest_files row is inserted (last).
        file_id = conn.execute("SELECT COALESCE(MAX(file_id), 0) + 1 FROM ingest_files").fetchone()[0]
        rows_total = rejected = merged = warnings = 0

        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            colmap = _column_map(wf, header, f"{drop_name}/{rel_path}") if header else {}
            for row in reader:
                if all(c.strip() == "" for c in row):
                    continue                                    # blank: skipped, not counted
                rows_total += 1
                values, reason, w = validate_row(wf, colmap, row)
                if reason is not None:
                    rejected += 1
                    conn.execute("INSERT INTO lead_rejections (file_id, row_number, reason) VALUES (?,?,?)",
                                 (file_id, rows_total, reason))
                    continue
                warnings += w
                if _upsert_lead(conn, vault, wf, file_id, batch_id, values):
                    merged += 1

        conn.execute(
            "INSERT INTO ingest_files (file_id, kind, drop_name, rel_path, workflow_id, batch_id, sha256, "
            "ingested_at, rows_total, rows_rejected, rows_merged, field_warnings) "
            "VALUES (?, 'leads', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, drop_name, rel_path, wf.workflow_id, batch_id, sha,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), rows_total, rejected, merged, warnings))
        conn.execute("COMMIT")
    except FileChangedError:
        raise
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return FileResult("ingested", drop_name, rel_path, rows_total, rejected, merged, warnings)


def ingest_drop_leads(conn: sqlite3.Connection, vault: PiiVault, workflows: dict[str, Workflow],
                      drop_dir: Path) -> list[FileResult]:
    """Ingest drop/leads/<workflow_id>/*.csv in filename order."""
    drop_dir = Path(drop_dir)
    results: list[FileResult] = []
    leads_root = drop_dir / "leads"
    if not leads_root.is_dir():
        return results
    for wf_dir in sorted(p for p in leads_root.iterdir() if p.is_dir()):
        rel_dir = wf_dir.relative_to(drop_dir).as_posix()
        wf = workflows.get(wf_dir.name)
        if wf is None:
            log.error("%s/%s: no schema for workflow %r; skipping this folder", drop_dir.name, rel_dir, wf_dir.name)
            results.append(FileResult("unknown_workflow", drop_dir.name, rel_dir))
            continue
        for f in sorted(wf_dir.glob("*.csv"), key=lambda p: p.name):
            results.append(ingest_lead_file(conn, vault, wf, f, drop_dir.name,
                                            f.relative_to(drop_dir).as_posix()))
    return results
