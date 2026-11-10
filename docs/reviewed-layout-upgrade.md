# Reviewed source-layout upgrade

The independent-review corrections use a separate V6 profile. Use this profile for
new remediation runs; V4 and V5 remain supported for identifying historical
artifacts. Reusing a processing-set name does not upgrade stored content.

| Artifact | Previous corrected profile | Reviewed profile |
| --- | --- | --- |
| Profile | `corrected-layout-v5` | `reviewed-layout-v6` |
| Reports and section identities | `report-v2` | `report-v3` |
| Tables, cells and review issue keys | `tables-v5` | `tables-v6` |
| Chunk algorithm | `paragraph-v1` | `paragraph-v2` |
| Default processing-set key | `automated-v5` | `automated-v6` |
| Facts | `facts-v1` | `facts-v1`, exact immutable payload agreement required |

V6 creates fresh report/table identities for the revised source boundaries,
registrant checks and numeric-cell classification. Paragraph V2 snaps overlap
starts to token boundaries. Historical Paragraph V1 behavior remains available.
All three supported document-set version tuples are retained. Old receipts
remain unchanged; the current parser refuses to reuse a stale receipt.

Apply additive migrations 011, 012 and 013 to the explicitly selected receiving
database before ingestion. V6 checks all three recorded checksums before any
filing work. Migration 013 extends the existing table/source consistency guards,
checks exact report/chunk membership, and exposes actual stored parser receipts
through `finance.processing_revision`. A NULL historical revision remains NULL;
it is never filled from today's parser policy.

Use a dedicated scoped universe and queue, reconciled original source archives,
and the established database capacity limit. For an existing offline queue:

```sh
uv run --locked python scripts/backfill_sec.py --dry-run --offline \
  --universe data/section-upgrade/universe.json \
  --state data/section-upgrade/queue.json --since 2021-01-01 --until 2026-11-09 \
  --data-root . --tables-version tables-v6 --limit 1

uv run --locked python scripts/backfill_sec.py --ingest --offline --validated-only \
  --universe data/section-upgrade/universe.json \
  --state data/section-upgrade/queue.json --since 2021-01-01 --until 2026-11-09 \
  --data-root . --tables-version tables-v6 --limit 1 \
  --env-file .env.aws --database sec_filings --max-database-gib 14
```

These dates and capacity reproduce the earlier investigation examples; use the
actual recorded scope and configured ceiling. This command does not create or
assert complete SEC discovery. Legacy stale receipts are retained for explicit
migration, while pending work can continue. Re-acquiring a manifest with the
same stable source membership does not overwrite its first acquisition receipt.

Follow the validation, publication and rollback steps in
[the corrected-section runbook](corrected-section-upgrade.md) using the returned
V6 processing-set UUID. V6 remains unpublished until explicitly selected. Inspect
`finance.processing_revision` alongside `finance.filing` to see the actual served
set and recorded parser revisions. Publication does not refresh external
retrieval indexes; rebuild and verify those consumers separately.

Do not interpret a successful local parser run as a completed receiving-database
migration or verified historical extraction. Preserve missing-source and semantic
review outcomes, and run transactional canaries before expanding the scope.
