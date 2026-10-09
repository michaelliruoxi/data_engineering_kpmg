# Task 2 processing workflow

The input verifier, source-ID mapper, transaction controller, and configurable
pure dry-run are available. The controller is implemented in
`sec_pipeline.processing.run_ingestion_transaction()` and dry-run in
`run_dry_run()`. Production facts, table, cleaner, and validation adapters still
need integration; this framework does not yet complete the supplied filing's
financial import.

## Available commands

Run these from the repository root with the locked environment:

```sh
uv run --locked python scripts/process_filing.py --verify-inputs
uv run --locked python scripts/process_filing.py --help
uv run --locked python -m unittest discover -s tests -p 'test_processing*.py' -v
```

Input verification checks the approved local sources without SEC requests.
It does not parse financial data or write database records. Transaction-controller
tests use a recording connection; dry-run tests use pure fixture adapters and
Ruby's actual chunker. CLI tests verify and parse unchanged bundled source files
with deliberately limited test parsers while database connections are forbidden.
These offline tests prove workflow and isolation behavior; they are not acceptance
of the team's financial extraction and cleaning. The opt-in PostgreSQL suite below
separately verifies real transaction behavior.

## Dry-run command and pure adapters

`--dry-run` requires an explicit adapter module and all four processing versions.
The module must expose `get_dry_run_plan()` returning `DryRunPlan(stages,
required_checks)`. No production adapter module is supplied yet. A missing module,
parser, result, or required validation check is an error; nothing is silently
skipped or reported as a successful filing run.

After the real adapters are agreed and integrated, use this command with their
actual module and version labels. The uppercase values below are placeholders:

```sh
uv run --locked python scripts/process_filing.py --dry-run \
  --manifest source_manifest.json --data-root . \
  --adapter-module YOUR_AGREED_ADAPTER_MODULE \
  --facts-version YOUR_FACTS_VERSION --tables-version YOUR_TABLES_VERSION \
  --reports-version YOUR_REPORTS_VERSION --chunks-version paragraph-v1
```

Each `DryRunStages` parser receives only a `DryRunContext`, containing verified
source paths, requested versions, reference data, temporary IDs, and previous
parser outputs. It must not connect to databases or perform storage. Adapters
translate the actual teammates' parser signatures into this runner-owned API.
Facts and tables return `ParseResult(items, rejected=(), unresolved_cells=0)`
with materialized records. The report adapter returns a `ParseResult` with one
`ParsedReport(full_text, sections)`. Its section offsets refer to the unchanged
final cleaned text. The default chunks adapter calls Ruby's real `chunk_report()`.
The pure validation adapter returns the same strict `ValidationResult` described
below and checks actual parser outputs for the exact requested versions.

The CLI reuses `load_manifest()` and `verify_sample()`, verifies before loading
adapter code, and the runner verifies before parsing and again after validation.
Verification uses local files only and never replaces or downloads a missing or
changed source. Help does not load adapters or read source files.

Optional `--reference-file path.json` supplies a JSON object to the validator.
Its optional `expected_counts` object names `facts`, `tables`, `reports`, and/or
`chunks` with non-negative integer counts. A supplied count must match extraction;
absent counts are reported as unknown (`null`), not invented. Other reference
fields are passed through for the agreed financial checks.

Successful stdout is a JSON summary of accession, source hashes, versions,
expected/extracted/stored/rejected counts, unresolved cells, report characters,
and the actual validation checks. All stored counts are zero. Parsed records,
report text, credentials, and temporary source/report UUIDs are not returned.
There is no database connection parameter and the dry-run never invokes the
metadata or storage helpers. Unexpected adapter diagnostics are reported by stage
without printing their exception text.

## Transaction controller contract

The caller first uses `load_manifest()` and `verify_sample()` from the existing
sample downloader. Pass the verified manifest, its path, the data root, and
a fresh writable connection with `autocommit=False` to the controller. Existing
transactions are rejected, so the controller cannot commit or roll back unrelated
caller work. The connection remains open for its caller to close.

Metadata is registered through the real `seed_manifest()` by default. Its nested
transaction remains inside the controller's outer transaction. Source IDs are
resolved by role through each manifest `local_path`: XML supplies facts; HTML
supplies tables and the report.

`PipelineStages` is Jace's runner-owned adapter contract, not a claim about
teammates' parser signatures. An adapter calls a pure parser with source inputs,
then uses existing storage helpers with the supplied connection. It returns
a non-`None` result; later adapters read prior results through `context.outputs`.
Adapters must propagate failures and must not open separate write connections.

| Order | Adapter | Available prior outputs |
| --- | --- | --- |
| 1 | `facts` | Registered filing and source IDs |
| 2 | `tables` | Facts needed for table links |
| 3 | `report` | Facts and tables |
| 4 | `chunks` | Stored report ID and cleaned report output |
| 5 | `validate` | All four outputs, still uncommitted |

Supply explicit non-empty version labels under `facts`, `tables`, `reports`, and
`chunks`. Ruby's current chunker defaults to `paragraph-v1`; the other version
labels need to match the actual adapters. The controller does not invent defaults.

The validation adapter must check the actual outputs and return a
`ValidationResult(versions=..., checks=...)`. Its versions must equal the exact
requested labels. `required_checks` is an explicit non-empty list from the
agreed validator checklist. Every required check must be present, and all returned
check values must be exactly `True`; `False`, `None`, a missing check, an empty
result, a bare success flag, or a version mismatch prevents commit. Check names
and parser-specific result shapes still need agreement with the module owners.

Only successful validation permits the outer transaction to commit. Metadata
and outputs remain in that same transaction, so a later propagated failure
causes rollback. After metadata, every write stage, and validation, the controller
also requires an active, healthy transaction. An adapter that catches a SQL error
without recovering the transaction cannot produce a successful import.
`seed_metadata` is an optional injection point used by offline
tests; production integrations should retain the real default metadata helper.

## Local PostgreSQL transaction tests

Run the explicit live-test command from the repository root:

```sh
uv run --locked python scripts/test_ingestion_local.py \
  --env-file .env.local --database sec_filings_test
```

The dedicated local database must already exist, have the project migrations,
and contain no business rows. The command reads the explicit environment file,
ignores ambient `PG*`/`POSTGRES_*` settings throughout the run, pins the connection
to a loopback address, and rejects remote hosts, application database names, or
names without the `_test` suffix. It does not create, migrate, truncate, or drop
databases. An unavailable, unmigrated, or nonempty database is an error; the
explicit command cannot report success through skipped tests.

The ten tests use the actual verified manifest, default `seed_manifest()`,
storage helpers, PostgreSQL constraints, and Ruby's real chunker. They write two
clearly marked fixture facts, one linked fixture table, and fixture report/chunks;
these are not financial records extracted from the filing. The suite checks:

- Validation reads the exact requested versions and actual stored rows while a
  separate connection still sees only previously committed data.
- Failures after each write stage, false/unverified/missing checks, wrong versions,
  and a late PostgreSQL chunk constraint failure roll back metadata and outputs.
- A second successful run preserves every UUID, row count, row value, timestamp,
  and table/fact link; a failed replay preserves the previous committed import.
- A conflicting fact rolls back a new preceding row and preserves existing data;
  adapters cannot manually commit or roll back the controller's outer transaction.
- A swallowed SQL error in a write stage or validator cannot report success after
  PostgreSQL has aborted the transaction. An error correctly recovered inside a
  nested savepoint leaves the outer transaction healthy and can still succeed.

Successful cases really commit so an independent connection can prove visibility
and replay. Cleanup uses only this test's recorded metadata UUIDs and output
versions, in foreign-key order, and checks that the database returns to its initial
empty state. A session advisory lock prevents concurrent copies of this suite.
Original source and manifest hashes are checked before and after; HTTP requests
are forbidden. Ordinary test discovery skips this live suite unless configured by
the explicit command. The six local-target guard tests remain part of the offline
processing suite.

## Remaining integration

Wire the actual facts, table, cleaner, and validation adapters into the pure
dry-run and write controller. Add the real-import CLI with explicit destination,
versions, and references, and its stored/rejected/unresolved count summary.
The live fixture suite verifies PostgreSQL transaction and replay behavior; the
actual production adapters still need those checks and full acceptance on the
supplied filing. Jace's shared login is read-only: write tests use a local writable
database, followed by Michael's reviewed import.
