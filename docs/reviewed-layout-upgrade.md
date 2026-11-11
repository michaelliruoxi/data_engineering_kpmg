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

Apply additive migrations 011 through 015 to the explicitly selected receiving
database before ingestion. V5 and V6 check all five recorded checksums before any
filing work. Migration 013 extends the existing table/source consistency guards,
checks exact report/chunk membership, and exposes actual stored parser receipts
through `finance.processing_revision`. A NULL historical revision remains NULL;
it is never filled from today's parser policy.

## Reader behavior and pre-migration inventory

The chosen contract keeps default source analysis on the single served set.
This deliberately narrows the older V4 analysis views: when `finance.filing`
serves the legacy family, `sec.analysis_ready_facts` and
`sec.analysis_ready_statement_cells` return no source rows, even if a passed V4
set exists. Returning both historical V4 and published corrected rows would
mix versions and could duplicate financial values. Historical source reads
must select an explicit `processing_set_id` from `sec.source_fact_catalog` or
`sec.source_statement_cells`.

| Consumer | Selection after the upgrade |
| --- | --- |
| `finance.*` | Current served family and set |
| `sec.analysis_ready_facts`, `sec.analysis_ready_statement_cells`, `sec.source_chunk_catalog` | Current served source set only |
| `sec.chunk_citations` | Served versioned chunks, plus unbound legacy reports |
| `sec.section_catalog`, `sec.statement_catalog`, `sec.fact_catalog`, `sec.chunk_catalog` | Explicit preferred legacy set, even while a corrected source set is served |
| `sec.source_chunk_catalog_history`, `sec.chunk_citations_history` | All retained versions for explicit historical queries |

Migration 014 repairs `sec.chunk_catalog` by reading historical citations within
its exact preferred legacy report/chunk scope. It does not change any preference,
stored artifact, or default source-analysis filter. Migrations 011–013 are left
unchanged because their shared-database application status was not verified.
The reader tests exercise a populated 011-to-current upgrade and explicit
legacy preference through corrected publication and rollback.

Before applying migration 012 or the full upgrade batch, record this read-only
count on the receiving database (requires migration 010 or later). A nonzero
result identifies filings whose older source-analysis rows disappear while
their legacy family remains served; review those consumers before migration.

```sql
BEGIN TRANSACTION READ ONLY;
SELECT count(*) AS legacy_served_filings_with_passed_source_set,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM sec.source_processing_sets v4
           WHERE v4.filing_id = served.filing_id
             AND v4.validation_status = 'passed'
             AND v4.versions->>'tables' = 'tables-v4'
       )) AS legacy_served_filings_with_passed_v4_set
FROM finance.filing served
WHERE served.processing_family = 'legacy'
  AND EXISTS (
      SELECT 1 FROM sec.source_processing_sets source_set
      WHERE source_set.filing_id = served.filing_id
        AND source_set.validation_status = 'passed'
  );
ROLLBACK;
```

The shared-database count has not been run: its connection configuration was
unavailable. The behavior above is the implementation decision for owner
confirmation before migration; no shared migration or serving change has run.

## Ingestion and acceptance

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

## Rollout sequence

Run each step on the ingest machine, in order. Steps 1, 3 and 5 are read-only.
The examples use the scoped queue from the commands above; substitute the
recorded scope, dates and capacity ceiling. On Windows use
`.\.venv\Scripts\python.exe` in place of `uv run --locked python`.

**1. Inventory the receiving database before migrating.** This runs the count
above and records it together with the migration ledger:

```sh
uv run --locked python scripts/check_upgrade_ready.py \
  --env-file .env.aws --database sec_filings \
  --output data/section-upgrade/evidence/before-migration.json
```

The evidence file lists every packaged migration as applied, pending or changed,
predicts what `sec-db migrate` would apply, counts stored processing sets by the
parser revisions they recorded (`stale_for_this_checkout` sets cannot be
replayed under their identity and need the V6 profile), counts the served
selections, and runs the reader-impact count. Exit 0 means nothing is pending
or the pending batch has no recorded concern. Exit 2 means the `assessment`
names a decision or a blocker: a changed or out-of-order ledger blocks
`sec-db migrate` itself; a nonzero reader-impact count, other active sessions or
a missing `sec_reader` role need an operator decision. The file is evidence, not
approval, and contains the database host name; keep it outside Git.

**2. Back up, rehearse and migrate** as described in
[the deployment guide](aws-deployment.md#applying-future-schema-updates). Run
step 1 again afterwards with a new output path; `pending` must be empty.

**3. Preflight the queue with the profile it will use.** The preflight checks the
migrations and tables of the selected profile, so pass the same profile and
scope as the import:

```sh
uv run --locked python scripts/check_backfill_ready.py --tables-version tables-v6 \
  --universe data/section-upgrade/universe.json \
  --state data/section-upgrade/queue.json --since 2021-01-01 --until 2026-11-09 \
  --max-database-gib 14
```

Without options it checks the October 13 `tables-v3` queue against migrations
001–009, as before. That default result says nothing about a V6 run: a database
at 001–009 passes it and cannot start `tables-v6`. `scripts/run_backfill.ps1`
and `scripts/start_backfill.ps1` remain the `tables-v3` launchers for the
October 13 queue; start a V6 run with the `backfill_sec.py` commands above.
`--env-file` and `--database` select another target, such as a restored
rehearsal copy. Only a loopback database may omit `verify-full` TLS.

**4. Dry-run, then ingest a one-filing canary** with the two `backfill_sec.py`
commands above. Replay it with `--replay` and confirm the same IDs and counts.

**5. Audit what was ingested.** One read-only command audits every reviewed set
the queue ingested into this database and records what each filing serves now:

```sh
uv run --locked python scripts/publish_reviewed_queue.py plan \
  --state data/section-upgrade/queue.json --data-root . \
  --env-file .env.aws --database sec_filings \
  --output-dir data/section-upgrade/evidence/plan-001 --limit 1
```

It runs `review_section_upgrade.py validate` for each filing, writes one
evidence file per filing and `plan.json`, and prints the plan's SHA-256. Queue
entries that are review-required, stale, from another profile or ingested into
another database are counted under `skipped_queue_entries` and never planned.
A set that is served now and no longer passes is listed under
`served_sets_failing_audit`; the command does not withdraw it.

**6. Publish the reviewed plan.** Review `plan.json`, then pass its SHA-256:

```sh
uv run --locked python scripts/publish_reviewed_queue.py publish \
  --plan data/section-upgrade/evidence/plan-001/plan.json --plan-sha256 PLAN_SHA256 \
  --data-root . --env-file .env.aws --database sec_filings \
  --output-dir data/section-upgrade/evidence/publish-001
```

Each filing is published through `review_section_upgrade.py publish` with the
selection recorded in the plan as its expected selection, so a filing whose
selection changed since the plan is reported as `selection_conflict` and left
alone. A changed plan file, or a plan made for another database, publishes
nothing. An unacknowledged commit stops the batch with exit 3 and
`outcome_unknown`; inspect `finance.filing` and the publication history before
continuing. Publishing the same plan again leaves already-published filings
unchanged. External retrieval indexes are not refreshed.

**7. Withdraw a batch** with the summary that publication wrote. Each filing is
restored to its exact earlier selection, or left alone if that baseline changed:

```sh
uv run --locked python scripts/publish_reviewed_queue.py rollback \
  --published data/section-upgrade/evidence/publish-001/publish-summary.json \
  --env-file .env.aws --database sec_filings \
  --output-dir data/section-upgrade/evidence/rollback-001
```

Use a new `--output-dir` for every run; earlier evidence is never replaced.
Repeat steps 4 to 6 with a larger `--limit` only after the canary's plan,
publication and rollback evidence has been reviewed.

Verification limits: the batch command is tested against the real review
command with only the database layer replaced. The inventory's SQL statements
are executed only by the hosted workflow's PostgreSQL tests in
`tests/test_upgrade_readiness.py`; check that run for the revision in use. None
of these commands has run against the shared database.


## Parser receipts and unchanged storage labels

This follow-up changes report section output and source acceptance in **V4,
V5 and V6**, because they share the cleaner and numeric correspondence checks.
It does not rewrite XML fact values or change the table-cell transformation or
chunking algorithms. Changed section boundaries can change chunk boundaries and
section associations even with the same chunk algorithm.

| Profile | Report revision now required | Facts/source-check revision |
| --- | --- | --- |
| V4 (`report-v1`) | `legacy-visible-layout-boundaries-v4` | `numeric-tag-boundaries-v3` |
| V5 (`report-v2`) | `corrected-visible-layout-boundaries-v4` | `numeric-tag-boundaries-v3` |
| V6 (`report-v3`) | `reviewed-original-source-boundaries-v4` | `numeric-tag-boundaries-v3` |

The table revision remains `registrant-numeric-cell-validation-v2`; paragraph-v2
remains `token-boundary-overlap-v2`. Every selected primary statement must map to
a section typed `financial_statements`, independently of the heading wording.
Parsing enforces that invariant for all three profiles; stored corrected-set
acceptance checks the actual relational sections again.

Existing receipts and artifact rows remain immutable. In particular, every
already-ingested production `tables-v3` queue receipt from the prior parser is
stale after this change: the shared `report-v1` and numeric-check revisions
changed. Resumption retains historical `ingested` status and result, marks
`stale_receipt`, excludes it from current verified completion, and continues to
pending work. A regenerated manifest with byte-identical sources and unchanged
membership, versions, profile and parser revisions is different: it reuses the
same processing-set ID and preserves the first manifest digests. Rebuilding the
core `source_manifest.json` has the same provenance-only treatment.

A new set key does not create new report/table identities. Existing legacy
artifacts can be remediated with the fresh V6 storage tuple. Already-stored V6
artifacts that need different content require further explicit storage versions
or a separately reviewed data migration; this change does not invent such an
upgrade or overwrite their content. Migration 015 requires current revisions
for newly sealed corrected sets and new publication. Existing serving pointers
and proven historical rollback remain intact; stale receipts cannot pass fresh
stored acceptance or be newly published.

V5 is retained for historical compatibility, not selected for new remediation.
Its `original-heading-v1` configuration label is a historical compatibility
label: the current parser also applies the stronger source-boundary and selected
statement checks. Use the stored parser revisions for the implementation
identity and V6 for new remediation. No V5 profile or historical receipt is
silently relabelled or deleted.

## Operational status and evidence outcomes

`fully_imported` is deliberately an ingestion-completion label. This PR relaxes
that label to permit declared `tables=partial` and `reports=unknown` when all
four artifact classes are stored, the receipt passes, and no unaccounted cell
or source-quality issue remains. It does not relax source acceptance,
publication, semantic coverage or financial-value correctness. The status
consumer contract is also documented in [automated backfill](automated-backfill.md).

If publication or rollback loses its commit acknowledgement, the evidence has
`status=outcome_unknown`, `transaction_status=unknown`, and
`attempted_served_selection`; it does not claim the candidate is served. Inspect
the receiving database selection and publication history in a fresh read-only
transaction before retrying. If recording that outcome also fails, the command
emits a sanitized unknown-outcome message. A failed run removes its newly
reserved empty file. A known successful commit followed by evidence-write
failure still returns exit 3 with `committed_evidence_incomplete`.

## Manual withdrawal when the previous baseline changed

Automatic rollback intentionally refuses to guess a replacement baseline.
For a deliberate withdrawal, an operator must first review and approve the
current selection and the *new expected fallback*, and save a fresh inventory.
The following is an operator procedure, not an action performed by this PR:

1. Begin a transaction and call `sec.lock_processing_filing(filing_id)`.
2. Recheck `finance.filing` and the matching
   `sec.preferred_source_processing_sets` row. Stop if the current source-set ID
   differs from the reviewed ID.
3. Leave `sec.rollback_publication_event` empty and delete exactly that filing's
   matching source-preference row. Require exactly one deleted row. Do not
   disable triggers, change a legacy preference, or edit publication history.
4. Query `finance.filing` again. Commit only if its family and processing-set ID
   exactly match the newly approved fallback, including an explicitly approved
   empty selection; otherwise roll back the transaction.
5. Record the before/after selection and the appended history event. This is a
   new withdrawal (the existing trigger records a publish-kind event with a NULL
   new preference), not a claim to restore the old baseline. Refreshing external
   indexes is a separate action.

This manual procedure has not been executed on the shared database. Automatic
rollback's existing refusal and exact-history restoration tests remain in place.
