# Financial Tables component verification — October 12, 2026

Baseline: main `1369716`, initial Task 4 commit `7ef9e4e`. The existing extraction
exports were preserved. The real database tests intentionally reparse the verified
sample because a database UUID must come from the receiving database.

## Actual results

| Check | Result | Evidence |
| --- | --- | --- |
| GitHub identity/repository rights | Passed | jazzy2233; repository pull/push allowed |
| Original offline command | Failed | Ran 163 tests; 159 passed, 4 import errors; 3 class-level skips |
| Offline command with explicit src | Passed | 208 passing cases; 13 skipped cases plus 3 skipped classes; unittest: Ran 221 tests, OK (skipped=16) |
| Actual PostgreSQL component suite | Passed | 7 tests, 0 failures, 0 skips, PostgreSQL 17.11 |
| Facts/table inventory | Passed | 1622 facts-v1, 3 tables-v1, zero unresolved cells |
| Table memberships | Passed | income_statement 76, balance_sheet 76, cash_flow 131; total 283 |
| Repeat import | Passed | Actual committed replay retains UUIDs, complete row snapshots, timestamps and links |
| Conflicts | Passed | Changed fact/table content under the same version rejected; failed replay preserves committed snapshot |
| Incorrect fact IDs | Passed | Missing/unknown IDs and actual other filing/source/version records rejected |
| Whole transaction rollback | Passed | Error after partial table loading and late SQL error restore the exact pre-run business-table snapshot |
| Raw inputs | Passed | Manifest and all four source hashes unchanged |
| Shared RDS read-only login | Passed | sec_jazzy, sec_filings, TLSv1.3, TLS_AES_256_GCM_SHA384; verify-full with AWS CA |
| Shared SELECT rights | Passed | All three financial relations readable |
| Shared write rights | Unavailable | INSERT/UPDATE/DELETE all false; privileges queried without writes |
| Shared production versions | Not present | facts-v1 and tables-v1 absent on October 12 |
| Full team workflow | Not verified | Cleaner not merged; independent validator and complete production adapter plan absent |
| Sally independent review | Not verified | Automated checks do not establish independent review |
| Michael reviewed shared load | Not verified | No shared writes performed |

Shared Microsoft accession `0001193125-26-191507` exists. Current source/version
counts: XML `manual-example-v1` has 24 facts, HTML `manual-example-v1` has 3 excerpt
tables, with 24 same-filing links (income 8, balance 10, cash flow 6). The catalog
also has 1 report, 6 chunks and 4 source documents. These are today's queries,
not values copied from an older report. Full tables have not been deployed.

## Python import evidence and running requirements

Interpreter: uv-managed CPython 3.12.14 in `.venv/bin/python`, uv 0.12.21.
The editable `sec_pipeline.pth` contains the correct repository `src` path.
The failing interpreter's `sys.path` includes site-packages but omits `src`;
`python -v` explicitly reports `Skipping hidden .pth file` for both the editable
file and `_virtualenv.pth`. macOS `ls -lO` confirms the hidden flag.

Clearing those two flags makes direct Python, uv `--no-sync`, and a subsequent
uv sync/import succeed temporarily. The hidden flags return later and the
original discovery command fails again. We have not established which process
reapplies those flags; this is not proof of a uv defect or a persistent fix.
Existing subprocess tests launch the same `sys.executable` and inherit the
parent environment (one removes only PG/POSTGRES keys). Explicit PYTHONPATH
therefore reaches those children too. No absolute machine path was added to code.

Supported repeatable command for this machine:

```sh
PYTHONPATH="$PWD/src" uv run --locked python -m unittest discover -s tests -v
```

Ordinary discovery deliberately does not perform database writes. Its 16 skip
entries include 13 opt-in test cases and 3 skipped classes; class-level skips
are not included in unittest's `testsRun` count.

## Real local component reproduction

Use a new dedicated, empty local PostgreSQL test database with a name ending in
`_test` or `_test_SUFFIX`. The script rejects remote TCP hosts, pins explicit
loopback addresses, ignores ambient PG/POSTGRES settings, verifies the actual
database/server target, and refuses existing business data. A Unix socket is
also supported; the recorded run used a separate cluster with TCP disabled,
host authentication rejected, and a private local socket. SSL is disabled only
for this local socket test; shared RDS always uses verify-full.

```sh
PYTHONPATH="$PWD/src" uv run --locked python scripts/run_tables_postgres_tests.py \
  --host /ABSOLUTE/LOCAL/TEST/SOCKET --port 55432 \
  --database jazzy_financial_tables_test --user YOUR_LOCAL_TEST_ROLE --migrate
```

`--migrate` applies official repository migrations only to an empty dedicated
schema. Omit it on later runs. Password-protected loopback setups should adapt
the explicit local connection configuration; this socket-based suite uses no
password or shared account. The runner opens no shared connection.

The suite verifies the manifest through the repository helper, seeds actual
metadata, resolves real HTML/XML UUIDs, calls `facts_bridge.write_facts`, then
`tables_bridge.write_tables` using the in-run scoped mapping. A caller-owned
outer transaction surrounds each load. It compares JSON inline/XML ID sets to
stored table memberships and checks every fact's filing/source/version. A
separate connection confirms that uncommitted rows are invisible. Replay is
tested after an actual commit; only test-owned metadata and output rows are
deleted afterward, and the exact empty business-table baseline is checked.
The migration ledger remains. No mock or demonstration financial parser is used.

## Independent review and full workflow handoff

- Cash flow `r059c003`: inspect the untagged March 31 quarterly ending cash cell
  and same-row nine-month instant fact. Verify concept, period, unit, dimensions,
  scale and value for `same_row_semantic_repeat`; the shared fact explains 132
  data cells but 131 unique memberships. This is not an amount-only lookup.
- Balance sheet: three numeric row headings retain ten fact references for
  allowances, accumulated depreciation and share counts. Review all ten, not
  only the 66 financial data cells.
- For all tables: compare physical grid/spans, totals, date columns, USD millions
  versus EPS, display parentheses versus XML sign, blanks/formatting, original
  XPath/HTML, inline IDs, XML provenance and source hashes.
- Run `financial-tables-queries.sql` after an approved load. Membership sets must
  match all JSON references, with 76/76/131 links and no filing/source/version mismatch.

Main currently has facts, tables and Ruby's chunker, but no production cleaner
or independent validator. Emma's open PR #8 offers `clean_report(html_path)` and
`CleanedReport`; it is not in this baseline and does not supply a full runner
adapter or independent validator. Required production `report` and `validate`
adapters, agreed report/chunk versions, reference checks and complete
`get_dry_run_plan`/`get_ingestion_plan` provider remain integration work.
No fake report/validator was supplied to claim full processing success.

Shared deployment must use Michael or an approved loader after review, with
the receiving database's own fact IDs. This component test runner deliberately
rejects the shared host and is not a shared deployment command. Use the final
Jace production plan once real cleaner and independent validation adapters exist.
