# Historical table relationship validation

This tool audits Jazzy's table-side relationships without shared database writes.
It reuses `table_relations`, `table_cells`, v4 occurrence contracts, original-DOM
section mapping, and the existing SEC acquisition client. It is not a second
table identity system, a backfill runner, or an independent sign-off.

## Candidate scope, not an invented final shortlist

The requested companies are AAPL, CASY, MCK, MDT, MSFT, MU, NKE, NTAP, PH and TTWO.
The runner queries **all currently stored original 10-K/10-Q filings for these
CIKs, without an inferred year filter**. DB holdings are only candidates.
Supply repeatable `--manifest` inputs to union known un-ingested filings from
Jace's `discovery.json`, source manifests, filing arrays, inventories or backfill
queues. Unknown missing historical accessions remain unknown, not zero.

Jace's `origin/jace/shortlist-acquisition-20261028` config currently labels its
rolling filing-date window `pending_Emma_confirmation`. That is useful evidence,
not approval to turn the window or the phrase "17 10Q/10K" into a final scope.
The old Microsoft sample `0001193125-26-191507` is listed but not re-audited.

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
the scope owner must confirm final accessions and the meaning of "17". salu's
independent acceptance is not asserted. Missing sources or pending deployments
must never be reported as all historical tables accepted.
