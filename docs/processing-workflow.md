# Task 2 processing workflow

The input verifier and source-ID mapper are available. The transaction controller
is implemented in `sec_pipeline.processing.run_ingestion_transaction()`.
The full processing command and dry-run are still under development: this
controller alone does not extract or import the supplied filing.

## Available commands

Run these from the repository root with the locked environment:

```sh
uv run --locked python scripts/process_filing.py --verify-inputs
uv run --locked python -m unittest discover -s tests -p 'test_processing*.py' -v
```

Input verification checks the approved local sources without SEC requests.
It does not parse financial data or write database records. The workflow tests
use a recording connection and fixture adapters, without credentials or a
database. They test ordering, error propagation, and validation gates; they
are not PostgreSQL rollback or real-filing acceptance tests.

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
causes rollback. `seed_metadata` is an optional injection point used by offline
tests; production integrations should retain the real default metadata helper.

## Remaining integration

Wire the actual facts, table, cleaner, chunking, and validation adapters; provide
the processing CLI with explicit destination, versions, and reference options;
add a dry-run that verifies and parses without opening a database connection;
and report source hashes and extracted/stored/rejected/unresolved counts.
Complete real PostgreSQL late-failure rollback and stable replay tests before
claiming acceptance on the supplied filing. Jace's shared login is read-only:
write tests use a local writable database, followed by Michael's reviewed import.
