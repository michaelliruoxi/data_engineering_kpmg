# Historical table relationship validation

This tool audits Jazzy's table-side relationships without shared database writes.
It reuses `table_relations`, `table_cells`, v4 occurrence contracts, original-DOM
section mapping, and the existing SEC acquisition client. It is not a second
table identity system, a backfill runner, or an independent sign-off.

## Confirmed shortlist and bounded discovery snapshot

The requested companies are AAPL, CASY, MCK, MDT, MSFT, MU, NKE, NTAP, PH and TTWO.
`config/shortlist.json` now confirms original 10-K/10-Q filings dated
**2021-01-01 through 2026-12-31**, inclusively. The tracked
`docs/validation/report-sections-2021-2026.json` supplies **233 discovered
accessions**, with latest filing date 2026-10-28. It is a discovery/section
snapshot, not proof of every table or of future filings through year-end.

The legacy `validate_historical_tables.py` runner still queries all stored
original filings for the requested CIKs without inferring a year filter.
Supply repeatable `--manifest` inputs to union known un-ingested filings from
Jace's `discovery.json`, source manifests, filing arrays, inventories or backfill
queues. Unknown missing historical accessions remain unknown, not zero.

The legacy default continues excluding old Microsoft sample
`0001193125-26-191507`; use `--include-prior-sample` for an independent recheck.
The confirmed-shortlist workflow below always includes it when present in the
discovery snapshot. It never infers the meaning of "17" or drops un-ingested
filings. Unknown future discoveries remain outside this snapshot's completeness.

### Full discovery-snapshot reconciliation

First obtain a receiving snapshot with the existing reader, **in your own
interactive terminal**. No password is stored or placed in command arguments:

```sh
PYTHONPATH=src python scripts/validate_historical_tables.py \
  --ca /private/path/to/us-east-1-bundle.pem --prompt-password \
  --manifest docs/validation/report-sections-2021-2026.json \
  --include-prior-sample --source-root /path/to/existing/archive \
  --output /private/new-readonly-capture
```

Then use the scope-aware orchestration, reusing exactly those receiving IDs:

```sh
PYTHONPATH=src python scripts/validate_shortlist_tables.py \
  --config config/shortlist.json \
  --section-audit docs/validation/report-sections-2021-2026.json \
  --snapshots /private/new-readonly-capture \
  --source-root /path/to/existing/archive --output /private/new-table-validation
```

This command checks snapshot/config agreement, all expected accession rows,
per-set receiving identity, actual original hashes and same-version table/cell
reconstruction (including headers, periods and refs). It reuses V1/V2 extraction
and V3/V4 transforms, without storage writes. It records Git baseline plus tested
source-file digests, actual DB query time/identity and processing IDs.
Optional `--ticker`, repeatable `--manifest`, `--processing-set` and actual
`--table-chunks` inputs are supported. Ordinary paragraph chunks are not promoted
to table chunks. Draft producer links are not published when table checks fail.

Do not replace a database hash with a section-snapshot hash. Different recorded
hashes for the same URL are unresolved provenance conflicts, not proof of which
bytes or financial values are wrong. With no original bytes, both receipts must
be retained for Acquisition/Michael. Real-byte hash mismatches remain errors.

Additional outputs:

- `validation_errors.csv`: confirmed validation errors, not absent evidence.
- `validation_evidence_gaps.csv`: missing originals, absent receiving records,
  unsupported/incomplete checks and review-required cells.
- `validation_provenance_conflicts.csv`: disagreements between recorded receipts,
  separate from both real-byte failures and missing-original evidence.
- `source_reconciliation.csv`: each known HTML/XML receipt, original paths,
  original URLs, both hashes when applicable, local existence and observed hash.
  Unknown XML receipts have no guessed URL, hash or database UUID.
- `review_context_evidence.json`: original XML fact/context XPaths and exact
  context text when hash-verified XML exists; otherwise explicit unavailable
  status. NTAP dates are never silently corrected.
- `actual_trace_examples.json`: receiving table/cell/fact/source traces with
  unique section association or explicit ambiguity and actual-chunk status.
- `run_metadata.json`, `run_status.json`: tested source version and scope,
  timestamp, set results, completion/blocked counts; no credentials.

The section audit's pass is never copied into table-validation status. Stored
relationship consistency, original-source semantics, section mapping, producer
acceptance and deployment remain distinct. `2` means incomplete/unverified;
`1` means a confirmed validation error. Neither constitutes independent approval.

## Shared read-only run

Use the project environment (`uv sync --locked --group dev`), an existing private
libpq configuration or your own hidden terminal input. Never place a password
in a command, report or Git. This runner does not parse or retain team credential
attachments. A locally authorized in-memory credential bridge is outside Git.

```sh
PYTHONPATH=src python scripts/validate_historical_tables.py \
  --ca /private/path/to/us-east-1-bundle.pem --prompt-password \
  --source-root /path/to/existing/archive-root \
  --manifest /private/path/to/discovery.json \
  --output /private/path/to/historical-validation
```

The endpoint / database / user are pinned to team RDS / `sec_filings` /
`sec_jazzy`, with `verify-full` and the existing AWS CA. The transaction is
REPEATABLE READ READ ONLY, with `default_transaction_read_only=on` and a statement
timeout. Every candidate snapshot uses a read-only savepoint, so a failed query
rolls back without poisoning all remaining queries. No migrations, preferences,
grants, ingestions, sealing changes or table updates are executed.

Default behavior checks **each available processing set separately**, preserving
all version candidates and their source/document scopes. It does not promote the
newest set. `finance.filing` selection_reason, validation_status and coverage are
recorded as browsing evidence only, never as formal team approval. To pin a
specific receiving-DB set (after confirming it belongs to this accession):

```sh
# Use the same connection/source/output options as above.
PYTHONPATH=src python scripts/validate_historical_tables.py \
  --ticker AAPL --processing-set ACCESSION=legacy:ACTUAL_DB_UUID \
  --ca /private/path/to/us-east-1-bundle.pem --prompt-password \
  --output /private/path/to/pinned-validation
```

For source-set contracts use `ACCESSION=source-v4:ACTUAL_DB_UUID`. Overrides are
strictly validated against this filing's inventory, not against arbitrary IDs.
They pin an audit, not an owner-approved final scope.

## Original source requirements and optional downloads

`source_requirements.csv` records accession, official source URL, stored
local_path, SHA-256, role, attempted local resolutions, existence and hash status.
DB paths may refer to another import machine. Only roots explicitly passed to
the runner are searched; paths escaping those roots are refused.
`missing_sources_minimal.csv` contains only missing/mismatching HTML/XML for
this run, excluding the old sample. Index / XSD receipts remain in the full list.

Downloads are optional and never reload the database. Configure your authorized
real SEC contact as `EDGAR_IDENTITY` in your local terminal (not in Git). Then:

```sh
PYTHONPATH=src python scripts/validate_historical_tables.py \
  --replay /private/path/to/historical-validation \
  --download-missing --download-root /private/path/to/new-archive \
  --identity-approved --source-root /path/to/existing/archive-root \
  --output /private/path/to/source-recheck
```

The existing `SecClient` serializes requests at >=1 second, verifies HTTPS,
accepts only official SEC URLs, and stops on access/rate blocks. Missing contact
identity stops before any request; no contact email is invented. Existing files
are never overwritten. Downloaded bytes must match the original receipt hash;
mismatches are retained under `hash-differences` and are not accepted as originals.
No schema, rows, stored hashes or archived originals are replaced.

## Output and interpretation

- `validation_summary.csv`: one observed filing per row. The processing_set_id
  column enumerates pinned candidates, not a hidden default selection. Filing
  counts use distinct DB identities across all candidates; per-version counts
  are in `processing_candidates.csv`. Do not sum overlapping versions.
- `validation_issues.csv`: each failure / evidence gap, owner and disposition.
- `company_coverage.csv`: denominator and missing-history uncertainty per company.
- `candidate_manifest.json`: candidate provenance, metadata conflicts, no year guess.
- `processing_candidates.csv/json`: actual family, set, version, coverage, status.
- `table_section_mapping.csv/json`: original mapping or explicit unresolved row;
  unavailable original HTML produces no fabricated section UUID.
- `trace_examples_by_company.json`: real stored table -> cell -> fact -> source
  receipts, with display/evidence fields and available **unmapped** section
  candidates. Without original sources these are partial traces, not end-to-end
  acceptance. `trace_examples.json` additionally preserves per-set examples.
- `ACCESSION/FAMILY--UUID/snapshot.json` and `checks.json`: private reproducible
  evidence, not committed. `shared-inventory.json` records timestamp, identity,
  SSL, schema, permissions, candidates and finance browsing evidence.

`database_relationship_status=passed_stored_evidence_only` means every checked
candidate's stored cells, links, memberships and scoped fact/occurrence evidence
agree. It is not proof of original headers, displayed amounts or XPath targets.
`original_source_semantics_status` is separate. Missing bytes, unresolved numeric
cells, review-required bindings and unconfirmed version choices prevent final
acceptance. No forced three-record count: coverage reuses physical statement
segment selection, so continuation tables and legitimate repeats remain possible.

Exit **1**: confirmed validation failures; **2**: incomplete, blocked or scope
pending. Zero is reserved for a future fully confirmed acceptance workflow; this
candidate-only runner never claims final acceptance. Query/credential failures
write sanitized phase receipts (DNS/TCP/SSL/authentication/permission); exception
text and full connection strings are withheld.

Replay without network or credentials:

```sh
PYTHONPATH=src python scripts/validate_historical_tables.py \
  --replay /private/path/to/historical-validation \
  --source-root /path/to/existing/archive-root \
  --output /private/path/to/replay
PYTHONPATH=src pytest tests/test_historical_table_validation.py \
  tests/test_table_relations.py tests/test_table_relations_cli.py -q
```

Replay is explicitly labelled not a new live shared query. Source hashes,
processing IDs and deterministic CSV content remain inspectable. Execution
timestamps and absolute output locations are not expected to be byte-identical.
Each replayed snapshot must match the requested accession, filing ID and processing
selection. If any candidate cannot be read or audited, filing-level coverage and
original-source semantics remain unverified even when the other candidates pass.

## Confirmed table-side validator correction

The historical NKE source-set check exposed a false positive for two NIL facts:
the existing `FactSourceOccurrenceInput` contract allows raw_value NULL or empty
string for NIL, while the previous validator required empty string. The fix
normalizes only those two permitted representations when **both nil flags are
true**; all other evidence and mapped-value validation remain exact. It changes
validator code only, not source facts, shared records or another team's contract.
Targeted tests reject non-nil empty/null values, false nil flags, mapped zero,
wrong concept/unit/period and cross-filing/source/document-set IDs.

## Ownership and completion boundary

Jazzy owns the validator, table-side defects, tests, reproducible outputs and PR.
Bryce owns fact/period/unit extraction dispositions; Acquisition owns discovery
completeness and matching original bytes; Cleaning/Chunking owns unresolved
section boundaries. Michael must confirm processing/schema/deployment choices;
the confirmed discovery snapshot remains bounded by its as-of date. salu's
independent acceptance is not asserted. Missing sources or pending deployments
must never be reported as all historical tables accepted.
