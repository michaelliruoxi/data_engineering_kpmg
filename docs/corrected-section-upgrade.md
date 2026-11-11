# Corrected section upgrade

For the November code-review fixes, use the newer [reviewed-layout upgrade](reviewed-layout-upgrade.md),
with `tables-v6`, `report-v3`, and `paragraph-v2`. This guide records the earlier v5 profile.

PR #20 repairs the section detector, but rerunning it under existing immutable
report/table identities is not a data migration. This upgrade gives corrected
outputs distinct identities, verifies their original headings, and requires an
explicit publication step before `finance.filing` selects them.

| Artifact | Existing identity | Corrected identity |
| --- | --- | --- |
| Facts | `facts-v1` | `facts-v1`, reused only if the entire payload agrees |
| Tables and cells | `tables-v4` | `tables-v5`, schema 3 |
| Reports and sections | `report-v1` | `report-v2` |
| Chunks | `paragraph-v1` | `paragraph-v1` under new report IDs |
| Queue processing set | `filing-import-v4` | `automated-v5` |

The chunking algorithm is unchanged; report identity separates old and new
offsets, section UUIDs, chunks, and citations. Quality issue keys also receive a
`tables-v5:` prefix because their immutable bindings refer to different cells.
Historical facts, sources, review issues and processing sets remain intact.

## 1. Establish the code and source baseline

Use current main, including PR #20's layout fixes and PR #23's numeric-tag
boundary checks. Keep PR #22's separate historical-table work intact. Capture
the exact commit, manifest hashes, receiving database, current selections,
database size, and other active sessions. Reconcile source URL/hash differences
before ingesting; do not replace registered source receipts to make a rerun pass.

The initial investigation found **13** missing section keys in the seven listed
latest filings, and 77 missing keys across 36 of 233 archived filings. All 233
original HTML files pass the corrected independent heading audit. This is
offline parser evidence, not evidence that 233 complete document sets are
available or that receiving-database rows have been repaired.

For a single receiving-database accession:

```sh
uv run --locked python scripts/review_section_upgrade.py inventory \
  --accession 0001193125-26-323660 --env-file .env.aws --database sec_filings \
  --output data/section-upgrade/before-msft.json
```

This command is read-only and includes source receipts and migration checksums.
It does not stop other workers. Confirm the established writer has stopped and
that the database has room for both versions before starting a separate upgrade
queue. Do not change the global queue or weaken its capacity limit.

## 2. Apply the additive schema migration

Apply packaged migrations using the established `sec-db migrate` command and
the explicitly selected receiving-database configuration. Migration 011:

- Accepts exactly the existing and corrected version tuples.
- Preserves source, fact, cell, report and chunk consistency checks.
- Requires the corrected parser receipts and original-heading check.
- Adds an explicit source-set preference and append-only publication history.
- Keeps corrected sets out of `finance.filing` until published.

The corrected ingestion wrapper verifies the migration's applied checksum
before writing. No previously applied migration is edited. The finance reader
views are replaced atomically within the migration transaction.

## 3. Rebuild in a dedicated, resumable scope

Use `scripts/backfill_sec.py` with a separately scoped universe/queue, the
original complete document-set archives, and `--tables-version tables-v5`.
A confirmed subset uses the supported `explicit-scope` universe schema, rather
than a truncated `current-sp500` snapshot. Create it once from the reviewed
shortlist (CIKs are mandatory; ticker labels may be absent):

```sh
uv run --locked python - <<'PY'
import json
from pathlib import Path
from sec_pipeline.universe import scoped_snapshot
config = json.loads(Path("config/shortlist.json").read_text())
assert config["scope_status"] == "user_confirmed"
target = Path("data/section-upgrade/universe.json")
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("x") as output:
    json.dump(scoped_snapshot(config["companies"], as_of="2026-11-10"), output, indent=2)
PY
```

The queue binds the exact universe file hash and filing-date window. Editing
its scope requires a new queue. The full S&P 500 schema retains its 490–510 issuer
guard and original HTML/hash validation.

Run discovery/download, then a dry run, then a one-filing canary:

```sh
uv run --locked python scripts/backfill_sec.py --dry-run --offline \
  --universe data/section-upgrade/universe.json \
  --state data/section-upgrade/queue.json --since 2021-01-01 --until 2026-11-09 \
  --data-root . --tables-version tables-v5 --limit 1

uv run --locked python scripts/backfill_sec.py --ingest --offline --validated-only \
  --universe data/section-upgrade/universe.json \
  --state data/section-upgrade/queue.json --since 2021-01-01 --until 2026-11-09 \
  --data-root . --tables-version tables-v5 --limit 1 \
  --env-file .env.aws --database sec_filings --max-database-gib 14
```

The dates above reproduce the investigation scope; use the recorded scope's
actual discovery cutoff for a later run. Queue/universe files must already
exist and agree on scope; these examples do not invent SEC discovery results.
Use the established capacity ceiling if it differs from the example.

Ingestion reconstructs the source outputs again before sealing. A source
conflict, changed immutable fact, ambiguous heading inventory, or failed source
check blocks acceptance. NetApp source defects retain their existing checks;
new section identities do not authorize fixing numbers or context dates in the
original filing. Review-required data cannot be published.

## 4. Verify actual stored outputs

Record the returned processing-set UUID. Re-run the canary with `--replay` and
confirm identical stored IDs and counts, then validate the actual receiving
database:

```sh
uv run --locked python scripts/review_section_upgrade.py validate \
  --accession 0001193125-26-323660 --processing-set-id CORRECTED_SET_UUID \
  --data-root . --env-file .env.aws --database sec_filings \
  --output data/section-upgrade/accepted-msft.json
```

Validation checks original source hashes and lengths, fresh report reconstruction,
independent expected Part/Item keys, unique original locators, relational section
descriptors, exact chunk slices and section UUIDs, and table/cell/fact/section
relationships. Failed or unavailable evidence cannot produce a passing result.
Primary headings must pass; supplemental documents keep their explicit unknown
semantic inventory. Detected labels are not relabeled as human-validated.

The acceptance output explicitly leaves retrieval-index refresh and actual
table-chunk producer verification **not verified**. Rebuild any consumer's index
from the new report/chunk IDs and validate its returned citations before switching
that consumer. Paragraph chunks are not evidence of a table-chunk producer.

## 5. Publish, verify readers, and retain rollback

After reviewing the canary, `publish` repeats the stored-output validation and
requires the previous selection recorded in step 1:

```sh
uv run --locked python scripts/review_section_upgrade.py publish \
  --accession 0001193125-26-323660 --processing-set-id CORRECTED_SET_UUID \
  --expected-family source --expected-set-id PREVIOUS_SET_UUID \
  --data-root . --env-file .env.aws --database sec_filings \
  --output data/section-upgrade/published-msft.json
```

Use `--expected-family legacy` for a legacy selection, or `--expected-family none`
without an expected UUID for an unprocessed filing. A changed selection stops
publication. The helper confirms that `finance.filing` actually selects the new
set before committing. This switches the finance SQL selection; it does not
refresh an external vector index or change its active collection.

```sh
uv run --locked python scripts/review_section_upgrade.py rollback \
  --accession 0001193125-26-323660 --processing-set-id CORRECTED_SET_UUID \
  --env-file .env.aws --database sec_filings \
  --output data/section-upgrade/rollback-msft.json
```

Rollback restores the prior preference and verifies the exact prior served
family/UUID. If the fallback changed meanwhile, it rolls back its own attempted
change and leaves the current selection intact. Publication history is retained.
Expand beyond the canary only after this evidence passes.

## Execution boundary in this investigation

Implementation and offline tests run in an isolated checkout. The original
checkout's capacity changes and other conversations' work are preserved.
The documented private database configuration was not available, external
download hosts could not be resolved, and no local PostgreSQL service was
available. Live schema migration, fresh shared-database inventory, source
reconciliation, historical ingestion, publication and retrieval refresh have
therefore not been performed. Do not report them as complete from offline tests.
