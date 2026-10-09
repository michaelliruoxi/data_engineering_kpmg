# Task 2 ingestion runner handoff

Jace's branch `jace/ingestion-workflow` supplies verification, pure dry-run
orchestration, and an explicit transactional import command for the supplied
Microsoft filing `0001193125-26-191507`. The runner is ready for module integration
and review. Task 2's real-filing acceptance remains pending until the production
facts, tables, cleaner, and validator are connected.

## Implemented behavior

- Reuse the approved local manifest and four unchanged source files. Missing or
  changed sources stop processing without SEC requests or replacement downloads.
- Resolve `seed_manifest()` IDs through manifest `local_path`: facts use the XML
  source; tables and the report use the HTML source.
- Keep dry-run parsers independent of connections and credentials. Their temporary
  metadata/report IDs and parsed records are not used by the write run.
- Run metadata, facts, table links, report, chunks, and exact-version validation
  inside one caller-owned transaction. Failed/unverified checks, aborted SQL
  transactions, storage conflicts, and late failures prevent commit.
- Require explicit adapter/version choices, environment file, and database name
  for writes. Report hashes, versions, expected/extracted/stored/rejected counts,
  unresolved cells, and validation; verify actual selected stored IDs before commit.
- Use Ruby's real chunker through `chunk_dry_run_report()` and the write bridge
  `store_ingestion_chunks()`.

The contracts and command options are documented in the
[processing workflow guide](processing-workflow.md). Adapter interfaces here are
runner-owned bridges; each teammate's actual parser signature still needs to be
agreed and adapted.

## Validation evidence

Both recorded runs used code at commit `bf94976`:

| Environment | Check | Result |
| --- | --- | --- |
| Development checkout | `test_processing*.py` offline suite | 101 tests passed in 10.255 seconds |
| Jace's Mac, October 8, 2026 | Explicit local PostgreSQL fixture suite | 12 tests passed in 11.521 seconds; no skipped tests |
| Dedicated local database | `127.0.0.1:5432 / sec_filings_test` | Fixture cleanup restored the empty database |
| Original sources | Manifest and raw files checked before/after live suite | Unchanged |

The PostgreSQL suite commits clearly marked fixture facts, linked tables, report,
and real chunks to establish visibility, replay, actual stored counts, and
whole-import rollback. The original source manifest, default metadata seeding,
storage helpers, constraints, and chunker are real. The financial/report records
are fixtures; these results do not validate the team's real financial extraction,
cleaning, or financial reference checks. The offline suite does not use live SQL.

Reproduction commands, from the repository root:

```sh
uv run --locked python scripts/process_filing.py --verify-inputs
uv run --locked python -m unittest discover -s tests -p 'test_processing*.py' -v
uv run --locked python scripts/test_ingestion_local.py \
  --env-file .env.local --database sec_filings_test
```

The live test database must already exist, be migrated, be local, and contain no
business records. The test command does not create, migrate, or clear a database
containing existing application data. Use the existing database testing guide for
setup; keep credentials outside version control.

## Inputs needed from module owners

| Owner | Handoff needed for integration |
| --- | --- |
| Bryce — facts | Published branch/module and pure parser signature; materialized fact records with stable occurrence keys, exact values/context semantics, XML provenance, version label, and rejection diagnostics |
| Jazzy — tables | Published branch/module and pure parser signature; the three statement records, stable cell/table locators, fact occurrence references, link evidence, unresolved-cell diagnostics, and version label |
| Emma — cleaner | Published branch/module and pure parser signature; final cleaned text and ordered Unicode-offset sections with original-source locators, inclusion/removal evidence, and version label |
| Ruby — chunks | Existing `chunk_report()`/`store_chunks()` are integrated; use the finalized report and its stored ID. Current chunker label is `paragraph-v1` |
| Sally — validation | Published validator/reference fixtures, agreed required check names, exact-version validation functions for pure and stored outputs, and real-source acceptance evidence |
| Michael — integration | Review bridge contracts, coordinate merged module versions, and confirm the route for the reviewed shared import |

Teammates should supply their actual versions and public function signatures.
The runner does not prescribe new financial parser APIs or substitute fixture
parsers for missing modules. Jace writes the bridges and continues owning the
command and transaction orchestration.

The adapter module needs both `get_dry_run_plan()` and `get_ingestion_plan()`.
The pure plan returns `ParseResult` outputs and one `ParsedReport`; the write plan
returns `WriteResult` outputs containing storage input records and their UUIDs.
Validators receive reference data and must return explicit `ValidationResult`
checks for exactly the requested versions. Every required check must pass before
an import can commit. See the workflow guide for the complete shapes and examples.

## Remaining acceptance work

1. Adapt the production module signatures, labels, and reference checks into both
   plans. Keep fact occurrence IDs available for table links and finalized report
   text available for chunking.
2. Run pure dry-run on the real supplied filing. Check all 1,622 XML fact
   occurrences, the three primary statements, retained report content, sections,
   chunk coverage/citations, and financial references with the module owners.
3. Run the integrated command against the explicitly selected writable local
   database. Independently validate stored outputs, repeat the same import, and
   deliberately fail late to prove preservation of the previous database state.
   Fixture test success does not replace these production-adapter checks.
4. Complete PR review and Michael's reviewed shared import, then verify read-only
   access and source tracing for the selected versions. Jace's current shared
   `sec_jace` account remains read-only.

The real command shapes are in the workflow guide. Their uppercase adapter,
version, and reference values are placeholders until the owners' production
modules are published and agreed. No complete financial import or shared import
is claimed by this handoff.
