# Financial table lineage: implementation and review handoff

This adds table-side relationship checking, original-DOM section mapping and a
**draft** table-chunk adapter. It reuses existing tables/cells/facts, extraction
versions, sections and receiving-database UUIDs. It does not extract old tables,
replace the paragraph chunker, promote a processing set or deploy shared SQL.
PR #9 is merged; these are additional integration tasks.

## Scope and selection

`table_relations.read_snapshot()` requires an active READ ONLY transaction.
Supply `{contract: legacy|source-v4, id: <receiving-database UUID>}`. With no
explicit choice it uses only an existing `preferred_processing_sets` row from
the legacy contract. It **never** chooses the newest candidate, converts a v4
candidate to legacy preference, or treats successful code checks as human
approval. With no preference it returns candidates and an unverified result.
The selected row's original validation/coverage/configuration remain in the
snapshot. A passed relationship audit is not a promotion or independent review.

Checks compare each anchor cell with retained structure and original HTML grid;
the exact `(filing, XML source, facts version, occurrence key)` with the stored
fact and original XML; concept/context/unit/period/dimensions/nil/raw/numeric
values; original scale/sign/format; exact accepted link evidence/order/method;
and set-equivalent table memberships. Multi-fact cells, repeated fact references
and non-fact cells retain their distinct semantics. No one-to-one cell/fact
assumption is made. Missing sources and unsupported legacy cell structures
remain unverified, not silently passed.

For source-aware versions, source-only marker observations and hidden-nil DOM
bindings are checked separately. V4 review bindings, exact occurrences and
quality issues remain separate from accepted `cell_fact_links`. The adapter
never promotes a period-conflict source fact into an accepted fact link.

## Table → section

`table_sections.map_table_section()` parses the unchanged, hash-verified original
HTML using the existing lxml/XPath compatibility rules. It locates a unique
table and uses original DOM traversal intervals or explicit DOM containers.
The whole table subtree must fit within one section. It does not compare XPath
strings lexicographically, infer ancestry from string prefixes, match headings
by title alone, or interpret cleaned text offsets as HTML coordinates.

Navigation, TOC hyperlinks, hidden headings and marked running headers/footers
are excluded. Duplicate real boundaries/keys, missing boundaries and overlapping
containers remain ambiguous. Tables spanning a heading boundary are
`crosses_boundary`. Other failures include `unresolved_table`,
`unresolved_section`, `scope_mismatch`, `invalid_section_id` and
`source_hash_mismatch`. Evidence preserves candidates, rejected/missing locators,
DOM intervals, parser/mapping version and original hash. Section validation
status is carried through without asserting human validation.

Exports include accession, processing set, source/document set, table UUID/key/
XPath, section UUID/key/title/locator, status and evidence. Offline UUIDs stay
null, including any UUID placeholders in prior table exports. `section_key` is
an extractor key, **not** `report_sections.id`.

### Persistence proposal (not deployed)

The current schema has report sections and chunk section IDs, but no canonical
table→section relation. `proposals/table_section_mappings.sql` proposes one
small additive relation, keyed by existing `(table_id, report_id)` and pointing
to an existing section UUID. It is deliberately **not** a packaged migration.
Michael must review schema naming, grants, review states and deployment first.

`store_table_section_mapping()` is an opt-in write helper for that proposal. It
requires a caller-owned transaction, rereads all receiving DB identities,
rebuilds the DOM result, checks selection/document-set scope and rejects changed
immutable evidence. It creates no schema and never commits. SQL additionally
checks composite parent FKs and source hashes. PostgreSQL does not parse HTML:
use the helper rather than hand-authoring DOM evidence. The immutable evidence
records the explicit selection; different selection evidence for the same pair
is a conflict requiring review, not an automatic overwrite. The SQL proposal
does not assert shared deployment or approve a processing set.

CSV generation and persistent database association are separate acceptance
items. Only the isolated local test DB has the proposal installed so far.

## Draft shared table ID adapter — pending Michael/Ruby confirmation

Entry point: `table_chunk_links.resolve_table_chunks()`.
Contract label: `draft-financial-table-id-v1`. Every result explicitly carries
`pending_Michael_Ruby_confirmation`, even when validation passes.

| Canonical input | Meaning |
| --- | --- |
| `chunk_id`, `chunk_type` | Producer's existing chunk identity; type must be `table` |
| `table_ids` | Nonempty UUID list (scalar UUID also accepted), **existing `financial_tables.id` in the receiving DB** |
| `filing_id`, `accession` | Exact receiving filing and accession |
| `processing_set` | Explicit `{contract,id}` selection, or explicit null for unselected candidates |
| `source/document_set` | Actual v4 document-set UUID; explicit null for legacy |
| `source_document_id` | Original HTML receipt UUID, not XML receipt |
| `table_version` | Exact selected financial table extraction version |
| `source_locator` | Original HTML `{xpath: ...}` locating the table/fragment/container |
| `synthetic` | True only for labelled fixtures; opt-in required |

No second table identity is generated. `table_key`, `chunk_id` and
`parent_chunk_id` are never substituted for `table_id`. Local test UUIDs must
not be imported into the shared DB. Multiple chunks can reference one existing
table. An explicitly multi-table chunk emits one mapping per original table;
if any declared table is invalid, **the entire chunk is rejected**, not partly
linked. Scope, source hash and actual DOM containment must agree.

Input paths and output names are configurable; example configuration:

```json
{
  "input_fields": {
    "table_ids": "metadata.financial_table_ids",
    "processing_set": "metadata.processing_set"
  },
  "output_fields": {"table_id": "financial_table_id"}
}
```

Ruby needs to provide actual producer records (including two fragments from
one table and any multi-table case), producer/version name, the real ID field
and scalar/list semantics, all scope fields above, raw HTML hash/locator, and
the selected shared processing/document set. Michael must confirm the identity
and field contract. Synthetic fixtures validate only this draft adapter, never
real end-to-end producer acceptance. No table chunks are inferred from the
existing paragraph chunks or cleaned text offsets.

## Reproduce without database access

Reuse already exported tables and the prior section descriptor JSON:

```sh
PYTHONPATH=src python -m sec_pipeline.table_relations_cli offline \
  --tables /path/to/exports/income_statement.json /path/to/exports/balance_sheet.json /path/to/exports/cash_flow.json \
  --sections /path/to/prior/latest-sections.json \
  --html data/raw/sec/0001193125-26-191507/msft-20260331.htm \
  --accession 0001193125-26-191507 --output /path/to/private/offline-output
```

This reproduces section key/title/locator only. Both table and section database
UUIDs are null; it does not validate a shared processing selection.

## Shared read-only verification

Existing `PGPASSWORD`, `PGPASSFILE`/pgpass or libpq service settings are supported;
`--env-file /private/path` reuses `database.connection_settings()`'s existing
PG/POSTGRES configuration. Optional `--passfile` is a filename, never a secret.
Never place a password in command arguments, chat, reports or Git. If credentials
are unavailable, run **in your own interactive terminal** with hidden input:

```sh
PYTHONPATH=src python -m sec_pipeline.table_relations_cli shared \
  --accession 0001193125-26-191507 --source-root /path/to/archive-root \
  --ca /path/to/us-east-1-bundle.pem --prompt-password \
  --output /path/to/private/shared-output
```

The endpoint/database/user are pinned to team RDS / `sec_filings` / `sec_jazzy`;
SSL is always `verify-full` with the supplied AWS CA. Identity, SSL, readable
permissions and visible schema are recorded before snapshot queries. The
transaction is repeatable-read/read-only with a statement timeout. No schema,
permissions, sealing or table rows are changed. Hidden input refuses non-TTY
and echo fallback. Exports contain no connection password. Exit codes: 0 passed,
1 failed/unverified, 2 connection/snapshot unavailable.

If no real legacy preference exists, inspect the returned candidate rows with
Michael, then rerun with `--contract source-v4 --processing-set <actual UUID>`
(or legacy). Never select a recent candidate merely because it is recent.
Optional `--chunks producer.json --field-config fields.json` runs the draft
adapter; `--allow-synthetic` is only for explicitly labelled fixture records.

Saved snapshots can be checked again using `replay --snapshot snapshot.json
--source-root ... --output ...` and the same optional chunk arguments. Replay
is always labelled saved-snapshot replay, not a fresh shared read. Keep all
output and private snapshots outside Git.

## Isolated PostgreSQL acceptance

Create a new **empty** local `*_test` DB first. Do not reuse a populated test
database, shared database or application database. The runner refuses them:

```sh
PYTHONPATH=src python scripts/run_table_relations_local.py \
  --host 127.0.0.1 --port 55439 --database jazzy_relations_msft_test --user jazzy_relations \
  --source-root /path/to/archive-root --table-exports /path/to/existing/table-exports \
  --sections /path/to/prior/latest-sections.json --output /path/to/private/local-output \
  --test-section-proposal
```

It verifies the manifest, reuses the three original exports, rebinds references
to actual **local** receiving fact IDs, stores cells/report/sections and creates
an explicit **unvalidated local-only** selection (not a shared preference).
It never calls `extract_tables`. It commits only in the chosen local test DB,
then audits a read-only snapshot. Optional proposal persistence is committed
and replayed there; synthetic chunk results are exported to separate files.

```sh
PYTHONPATH=src pytest tests/test_table_relations.py -q
SEC_RELATIONS_DB_TEST=1 SEC_RELATIONS_DB_DATABASE=jazzy_relations_msft_test \
SEC_RELATIONS_SOURCE_ROOT=/path/to/archive-root PGHOST=127.0.0.1 PGPORT=55439 PGUSER=jazzy_relations \
PYTHONPATH=src pytest tests/test_table_relations_postgres.py -q
```

The PostgreSQL suite checks actual MSFT cell/fact rows, UUID guard failures,
scope/value/link defects in adversarial snapshots, stable export, explicit
selection, committed replay and rollback of a newly inserted report/mapping.
V4 and source-only synthetic tests are identified as such; the existing v4
PostgreSQL suites separately test source/review storage and seals.

## Remaining ownership / acceptance boundaries

| Owner | Required external evidence/action |
| --- | --- |
| Jazzy | Implementation, reproducible exports, targeted and local PostgreSQL tests, PR; shared read-only rerun when credentials are available |
| Bryce + Jazzy | Reconcile receiving fact UUIDs, XML receipt/hash/version, scope, period/unit/value evidence against the selected shared set |
| Ruby + Jazzy | Actual table-chunk samples, producer ID/scope fields and split/multi-table behavior; real producer→table integration test |
| Michael | Official processing/document-set selection, shared schema/grants/deployment snapshot, contract confirmation and reviewed migration rollout |
| salu | Independent review of sources, saved queries/results, proposed schema and real shared trace; not asserted by these tests |

See `financial-table-relations-queries.sql` for parameterized read-only SQL.
Do not merge, run shared migrations, change privileges or claim independent
acceptance as part of this table-side handoff.
