# AI Usage Disclosure

## Tools used
- Claude in the Claude desktop app (chat): planning, reviewing plans and code summaries, drafting prompts, and drafting the SUBMISSION.md and AI_USAGE.md text.
- Claude Code in VS Code: wrote the code and tests under `solution/`, working from plans I had reviewed.

## What I used them for
- Reading the assignment and listing its ambiguities.
- Drafting the design: schema, PII approach, idempotency, report-time derivation.
- Writing the code (`normalise.py`, `schema.py`, `db.py` and `ddl.sql`, `pii.py`, lead and event ingestion, the report SQL and writers, the CLI) and the tests, one build step at a time.
- Drafting this write-up and the decision table.

## What I wrote or decided myself
- I chose Assignment A and set up the private repo, the environment and the `.gitignore` rules.
- I worked step by step: a plan first, then I reviewed it with a second Claude chat before approving, then one build step at a time. I approved or refused every file edit and ran every test and command myself in my own terminal.
- I made the calls on the open questions: how to handle the PII key, encrypting name and phone rather than dropping them, float formatting for `connectivity_rate`, observed-only `rejections_by_reason`, and supporting `ingest <parent>`.
- I checked the generator output with `Select-String` before deciding that timestamp flooring and header handling were safe.
- I ran the pipeline on a second seed (7) and checked the counters against each other by hand.

## Where the AI was wrong or unhelpful
- The first plan used `STRICT` tables, WAL mode, `json_valid` checks and a generated column. These depend on a newer SQLite and WAL adds extra files. I had them replaced with plain columns and `CHECK` constraints.
- The plan swapped the whole report directory by renaming it, which fails on Windows when the target exists. It now replaces each file with `os.replace`.
- The plan had no safeguard if the PII key changed. A new key would silently stop phones from matching. A key fingerprint check was added.
- `INSERT OR IGNORE` on the events table would also swallow CHECK failures and count them as duplicates. It was changed to `ON CONFLICT(event_id) DO NOTHING`.
- Claude Code once asked to edit a starter file (`README.md`), which the assignment says must stay unmodified. I refused.
- I noticed these by reviewing each plan and summary against the spec and my environment, not by trusting the output.

## How I verified AI-generated code
- 219 automated tests, run by me in my own terminal with `python -m pytest solution -q`.
- Manual runs of the real commands on generated seeds 42 and 7: ingest, re-ingest (every file "skipped"), ingest of the whole parent folder into a fresh database, and `report`.
- Cross-checking the counters: events total equals malformed plus duplicates plus stored; batch buckets sum to `total_leads`; the batch totals equal the unique-lead count; rejection reasons sum to the rejected total.
- Byte-equality tests: drop by drop versus all at once, report run twice, and a different PII key.
- A byte scan of the database for plaintext names and phones.
- I read the key parts (phone and date rules, effective disposition, buckets) before the interview, and I can explain them and change them live.
