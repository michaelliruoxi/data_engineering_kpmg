# GraphRAG financial table QA

The priority scope covers original 10-K and 10-Q filings for GOOGL, AMZN, WMT,
INTC, CAH, and WDC, with filing dates from 2021-01-01 through 2026-12-31
inclusive. Alphabet is identified by CIK 0001652044 even when the displayed
ticker is empty or GOOG. The Acquisition discovery file is the coverage
denominator. Use CIK plus accession to reconcile it to receiving records.
With no discovery file, the reported coverage basis is database holdings only.

## Capture and audit

Run from the repository in the locked project environment. Use the approved AWS
CA and private libpq settings or add `--prompt-password` in your own terminal.

```sh
PYTHONPATH=src python scripts/validate_historical_tables.py --help
PYTHONPATH=src python scripts/validate_historical_tables.py \
  --graphrag-priority --manifest /private/acquisition/discovery.json \
  --source-root /private/archive --ca /private/aws-ca.pem --prompt-password \
  --output /private/new-graph-table-capture
```

Omit `--manifest` when the Acquisition accession list is unavailable. Repeat
`--source-root` for approved archives. Add
`--processing-set ACCESSION=legacy:UUID` or
`--processing-set ACCESSION=source-v4:UUID` for each known receiving set.
Remaining unpinned accessions retain all candidates. A preferred or finance
browsing selection does not confer GraphRAG deployment approval. Preserve
`shared-inventory.json`, source receipts, and each accession/set
`snapshot.json` from the same read-only, repeatable-read capture. Record the
receiving database ID, Git revision, and query time in the local run notes.
Keep captures and originals out of Git.

The runner reports observed receiving consistency separately from original
HTML/XML checks, and distinguishes sets with no tables. A missing receipt or
bytes prevents source reconstruction. Stored display values, hashes, and context
references alone cannot verify the printed cells, signs, periods, or formats.

## Verify labels on actual downstream outputs

Ask the publisher for the released table, cell, and filing view/API export,
including an independent export time and canonical field mapping. Preserve the
raw exports. For each receiving set, normalize the real outputs into a JSON
object with arrays `filings`, `tables`, and `cells`. The filing array preserves
duplicate or missing rows. Table and cell exports preserve all rows from the
declared scope, including unexpected IDs and duplicates. Required fields are
`filing_id`, `fiscal_year`, and `fiscal_period`. Table/cell rows also carry
`id`, `processing_set_id`, and `processing_family`, and cell rows carry
`table_id`. Do not create these outputs with a QA-authored join of the base tables.

The optional verified DEI JSON assertion is keyed by `filing_id`,
`processing_set_id`, and `processing_family`, with `fiscal_year`,
`fiscal_period`, and `verified_dei: true` only after Facts owner review.
Retain the source document hash and original DEI fact/context locators with it.
Compare the labels under the agreed published representation (including year
type). The document fiscal focus does not change fact periods or comparative
column dates.

```sh
PYTHONPATH=src python scripts/check_fiscal_table_outputs.py \
  --snapshot /private/current/snapshot.json --downstream /private/export.json \
  --verified-dei /private/verified-dei.json --output /private/labels-check.json
```

## Verify before/after lineage

Ask Michael for the fiscal-only release reference, actual receiving database,
UTC release time, and a mapping of deployed filing IDs and processing set IDs.
Use a real pre-release capture or independently attributable historical export,
and a post-release capture from the same database and receiving set. A current
unclassified capture may be kept as evidence while deployment is unconfirmed.

Each capture receipt contains `database_id`, `deployment_ref`,
`captured_at_utc`, and `phase` (`before` or `after`). The after receipt also
contains `deployed_at_utc`. Record UTC timezone offsets. The database and release
reference must match and timestamps must bracket the deployment. Retain the
original receipt and the capture provenance with each snapshot.

```sh
PYTHONPATH=src python scripts/check_fiscal_table_lineage.py \
  --before /private/before/snapshot.json --before-capture /private/before/capture.json \
  --after /private/after/snapshot.json --after-capture /private/after/capture.json \
  --output /private/lineage-check.json
```

The comparison checks stable receiving IDs, parent relations, fact links and
evidence, memberships, stored source locators/hashes, and scoped fact periods.
Filing-level fiscal metadata is outside the comparison. A version/processing
set change, gap in lineage evidence, or missing baseline remains inconclusive.
The verification commands return 0 for a pass, 1 for a difference or
inconsistency, and 2 for incomplete or unverified evidence.
