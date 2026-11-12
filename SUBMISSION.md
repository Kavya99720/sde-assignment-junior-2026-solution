# Submission

## Candidate
- **Name:** Kavyashree CV
- **Assignment chosen:** A (Data): Interaction Ledger & Reporting Pipeline
- **Time spent (honest estimate):** ~4h

## How to run
Python 3.11+ (developed on 3.13). Only `cryptography` was added to `requirements.txt`; `fastapi`, `uvicorn` and `httpx` were already there from the starter and are not used by this solution.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python -m solution ingest data/drop_1
python -m solution ingest data/drop_2          # or: python -m solution ingest data   (all drops in order)
python -m solution report --out reports/
python -m solution stats                       # ingest counters, optional

python -m pytest solution -q                   # full test suite (219 tests)
```

- `--db` (default `ledger.db`, env `LEDGER_DB`) and `--schemas` (default `assignment-a-data/schemas`) go after the subcommand.
- PII key: set `LEDGER_PII_KEY`, otherwise `solution/.pii_key` is generated on first ingest (git-ignored, with a warning). Keep it with the database: a different key cannot match phones against existing data. A fingerprint stored in the DB makes this fail loudly (`PiiKeyMismatch`) instead of silently.
- Re-running `ingest` on a drop already loaded prints `skipped` for each file and changes nothing. `report` only reads the database.

## Design
Two layers: **ingest** writes raw facts, **report** derives everything.

**Schema (SQLite)**
- `ingest_files`: one row per ingested file (drop, relative path, sha256, kind, per-file counters). It is the idempotency anchor and the source of the `data_quality.json` counters.
- `leads`: unique on `(workflow_id, phone_hash)`. Holds `batch_id` (first valid file stem, permanent), `external_ref`, `phone_last4`, non-PII attributes, and the PII as an encrypted blob. Mutable on purpose, because duplicate rows merge into it.
- `lead_rejections`: `(file, row_number, reason)` only. No PII.
- `events`: the append-only interaction ledger, with `event_id` UNIQUE, UTC epoch timestamps, `phone_hash`, `call_id`, payload JSON and `disposition_code`. No raw phone and no raw event JSON is stored.
- `dispositions`: the catalogue loaded from `dispositions.csv`. `meta`: PII-key fingerprint.
- Triggers block UPDATE and DELETE on `events`, `ingest_files` and `lead_rejections`. Overrides are new events; nothing is ever edited.

**Ingest** is per file, in one `BEGIN IMMEDIATE` transaction, with the `ingest_files` row written last. A crash rolls the file back and a re-run redoes it. Same drop, path and sha256 is skipped; the same path with different content is refused (exit 1). Within a drop, leads are ingested before events. Event lines are checked as malformed, then duplicate (first `event_id` wins), then stored.

**Report** computes linking, orphans, calls, effective dispositions and buckets as SQL **at report time** over the stored facts, using TEMP tables and views, so the ledger file is untouched. This is why an event that was an orphan after `drop_1` counts once its lead arrives in `drop_2`, and why ingesting drop by drop equals ingesting everything at once: the derived state is a pure function of the stored facts. Report reads one snapshot, and each output file is written via a temp file plus `os.replace`.

**PII at rest**
- Matching key: `phone_hash = HMAC-SHA256(key, "+91XXXXXXXXXX")`. It is keyed, because a plain hash of a 10-digit mobile number can be brute-forced.
- `phone_last4` is stored only to produce `phone_masked`.
- Name and full phone are stored Fernet-encrypted. Hash key, Fernet key and fingerprint are derived from the master key with separate HKDF labels.
- A test scans the database file bytes for every name and every raw phone format from the source files.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Linking, orphans, unknown dispositions and buckets are computed at report time | Late leads and out-of-order events need no re-linking, and the ledger stays append-only | Compute at ingest, which needs a re-link pass and mutable state |
| Event-to-lead match on `(workflow_id, HMAC(normalised phone))` | Handles all six phone formats | Match on the raw phone string |
| Override links through `call_id` to the lead of the call's other events; an override with no linked sibling is an orphan | The spec says overrides carry no phone | Require a phone on the override |
| Effective disposition: any override beats any set; latest by `(occurred_at, event_id)`; unknown codes are ignored and fall back to the next valid one | Matches the spec wording and is deterministic on ties | Ingest order as tie-break, which would differ between bulk and incremental |
| Orphan check comes before the unknown-disposition check, and both are evaluated on the final ledger | Spec order; makes counts independent of drop order | Count unknown dispositions at ingest |
| A call exists only if a linked `call.initiated` exists; two initiated events for one call count once, dated by the earliest | Spec: calls are counted only when initiated is present | Count initiated events |
| Naive timestamps are IST; everything stored as UTC epoch seconds; report dates are IST | Spec. The generator only emits whole seconds | Treat naive as UTC |
| CSV read positionally with `csv.reader`; leftmost column wins if two columns map to one field; BOM stripped | `DictReader` silently keeps the last duplicate header | `DictReader` |
| Blank row = every cell empty, including unmapped columns | Spec says every cell | Only mapped columns |
| Rejection walks fields in schema order; whitespace-only = missing | Spec | Collapse reasons |
| Optional invalid value becomes null plus one warning per field, only for non-rejected rows; null never overwrites on merge | Spec | Warn on empty values |
| Merge: later valid row overwrites non-null fields (including the reference field); `batch_id` stays with the first file; defaults apply after merge | Spec | Keep the first reference value |
| Dates parsed by regex and a month-name map, validated with `datetime.date` | `strptime` `%b` depends on locale | `strptime` |
| `connectivity_rate` is float `f"{x:.4f}"` | A Python reference most likely does the same | Decimal half-up (differs only on exact ties) |
| `batch_summary` lists only batches with at least one lead; `rejections_by_reason` lists observed reasons only, sorted | Derived from stored leads and rejections | Zero-filled template, or one row per file |
| Ties on equal `external_ref` are ordered by `lead_id` | Deterministic | Unspecified |
| Extra malformed rules: non-object JSON, empty or non-string `event_id`/`event_type`, unparseable or non-string `occurred_at`, undecodable UTF-8 line | The ledger needs a type and a time; one bad line must not abort a file. The spec only names invalid JSON or a missing `event_id`, and the generator produces none of the extras | Treat as orphans |
| `ON CONFLICT(event_id) DO NOTHING` rather than `INSERT OR IGNORE` | OR IGNORE would also hide CHECK failures and miscount them as duplicates | `INSERT OR IGNORE` |
| No STRICT tables, generated columns or WAL | Portable across SQLite versions | Newer SQLite features |
| Events are read as bytes and split with `bytes.splitlines()` | A valid JSON line may contain U+2028; `str.splitlines()` would cut it | `read().splitlines()` on text |
| Drops must be ingested in order (`ingest <parent>` sorts `drop_*` naturally) | "First valid row wins" and `batch_id` depend on order | Order-independent merge |
| `report` leaves non-contract files in the output folder alone and writes empty-but-valid files on an empty DB | Least surprise | Fail on an empty DB |

## Failure modes considered
- **Re-run of a drop:** skipped by `(drop, path, sha256)`. Tested.
- **Crash mid-file:** one transaction per file, so a re-run redoes it. Tested.
- **Changed file at an already-ingested path:** refused with exit 1; the ingest stops so later files cannot merge out of order.
- **Wrong or lost PII key:** detected through the fingerprint in `meta`.
- **Dirty leads:** six phone formats, invalid phones, missing required fields, mixed date and currency formats, renamed and re-ordered columns, extra columns, blank rows, duplicates across batches.
- **Dirty events:** malformed lines, duplicate `event_id` across drops, overrides with no phone, unknown disposition codes, events arriving before their lead, numbers never uploaded.
- **Append-only:** triggers block UPDATE and DELETE on the ledger. Tested.
- **Not handled / known limits:**
  - Drops ingested out of order give a different `batch_id` and merge outcome than the specified order.
  - Two ingest processes at once are serialised by `BEGIN IMMEDIATE` but not tested.
  - A call whose events resolve to different leads keeps only events matching its `call.initiated` lead (impossible in generated data).
  - Whole files are read into memory for events.
  - If `report` crashes between file writes, old and new files can be mixed until it is re-run.
  - Losing the PII key makes encrypted PII unreadable.
  - Two files in one workflow with the same stem merge into one batch, as the spec defines `batch_id`.

## Testing
- 219 pytest tests, one command: `python -m pytest solution -q`.
- **Rules most likely to break**, tested with hand-computed fixtures: phone normalisation (all formats, `9123456789` stays valid), dates, decimals (lakh grouping), headers, timestamps (including the IST midnight boundary), lead merge and rejection precedence, effective disposition (override order, ties, unknown fallback), buckets, orphans arriving late, call de-duplication.
- **Invariants on generated data** (seeds 42 and 7): ingesting drop by drop gives byte-identical reports to ingesting everything at once; running `report` twice is byte-identical; a different PII key gives identical reports; `report` does not change the database; bucket columns sum to `total_leads`.
- **Privacy:** a byte scan of the database for raw names and phones.
- **Not tested:** the large dataset (see Results), concurrent ingests, and PostgreSQL. I have no independent reference implementation, so the SQL and the spec reading share one author. The seed 42 output does match the sample values in the spec (for example `0.5625` and a `last_call_at` of `2026-10-14T17:55:24+05:30`), which is encouraging but not proof.

## Results
- **Seed 42:** 2057 lead rows, 108 rejected, 57 merged, 1892 leads, 122 field warnings. 8745 event lines: 8 malformed, 237 duplicates, 8500 stored, 462 orphans, 18 unknown dispositions.
- **Seed 7:** 2065 lead rows, 114 rejected, 65 merged, 133 field warnings. 8834 event lines: 8 malformed, 265 duplicates, 8561 stored, 479 orphans, 11 unknown dispositions.
- About 5% of stored events are orphans (rejected leads and never-uploaded numbers), and a few thousand leads have no calls at all.
- **Timings (`--scale large`):** Not measured (the large dataset was not run).

## If I had another day
1. An independent pure-Python reference implementation that computes the five reports straight from the raw files, diffed against the SQL path across many seeds.
2. Measure and tune the large dataset (batched inserts, streaming event reads).
3. Truly incremental reporting: cache per-call and per-lead derived rows and recompute only what new events touch.
4. Production notes for 100k leads/day: scheduled drop pickup, PostgreSQL, key management in a KMS, alerting on rejected or orphan rates, backfills by replaying drops from an immutable store.
5. Order-independent lead merge so out-of-order drops are safe.
