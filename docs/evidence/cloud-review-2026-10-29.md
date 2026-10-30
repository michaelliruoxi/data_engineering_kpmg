# Cloud review evidence — 2026-10-29

## Baseline and preservation

Observed GitHub `main` at `c70ce55402d20d6a343803efb2b6626a5d80cbfd`
(PR #18 merged 2026-10-28). Implementation uses a new cloud clone and separate
worktree. The Mac checkout was not accessed or modified. Its reported 38-commit
lag and uncommitted validation work are historical claims, not current findings.
Comparing those changes requires a patch/status export from that checkout.

## Step 1 — historical validation evidence (incomplete; not final approval)

Read PR #18 and `docs/historical-table-validation.md`. The tracked archive has
only Microsoft sample `0001193125-26-191507`. Its three manifest-listed HTML,
XML and XSD files and separately listed index HTML match all four SHA-256 receipts. This sample was
explicitly excluded from the requested historical re-audit.

No historical audit snapshots, private discovery manifests, queue, authorized
shared database configuration/CA, or configured `EDGAR_IDENTITY` were available
in this isolated cloud checkout. No SEC download or shared database connection
was attempted. The previously reported 34 processing candidates / 20 filings /
40 missing originals therefore remain **dated findings**, not fresh counts.

| Evidence layer | Current result |
| --- | --- |
| Stored relationships on historical receiving DB | Not rerun: authorized read-only connection and CA or saved snapshots missing |
| Original-source historical semantics | Not rerun: corresponding snapshots/receipts and original HTML/XML missing |
| Committed sample receipt hashes | 4/4 manifest-listed files (including index_document) match |
| Validator unit/regression tests | 135 passed (`uv run --locked python -m pytest tests/test_historical_table_validation.py tests/test_table_relations.py tests/test_table_relations_cli.py -q`) |
| Direct pytest executable | Collection failed resolving `tests`; use the documented `python -m pytest` invocation |

### Scope and processing decisions still required

The ten named companies are AAPL, CASY, MCK, MDT, MSFT, MU, NKE, NTAP, PH and
TTWO. `config/shortlist.json` has a pending-owner-confirmation filing-date window
2021-10-13 through 2026-10-13, original 10-K/10-Q only. It is a proposed filter,
not a final accession list. `docs/export-shortlist-inventory.sql` has a different
legacy inventory window; neither overrides the scope owner. Final accessions,
the meaning of “17”, and one approved processing family/set per accession need
owner confirmation from the actual inventory. No IDs or versions are invented.

NTAP opening-cash correction remains blocked on its accession, selected
processing snapshot and original period/context evidence. Existing
`opening_balance_date_not_prior_day` review behavior is preserved; synthetic
regression coverage does not establish the actual NTAP period.

### Independent acceptance work and next inputs

Supply the private `shared-inventory.json` plus accession snapshots, discovery /
queue manifest, and matching source archive through an authorized cloud input.
Replay using the existing runner (see historical validation guide), keeping
stored checks and original-source checks separate. Alternatively configure an
authorized read-only DB connection with the existing CA. Downloads require an
already authorized SEC contact identity, and must use the existing `SecClient`.
The owner must approve scope/processing choices; independent reviewer acceptance
is still required. Candidate checks never constitute final approval.

## Step 2 — queue/receipt status

Added `scripts/report_backfill_status.py` and pure reconciliation module/tests.
Live receiving counts remain **unverified**: no queue or authorized cloud DB
configuration is available. Offline mode explicitly returns null DB counts,
never treating queue imports as DB imports. The published finance browsing
selection is exposed with candidate family/IDs/versions; it is not owner approval.
The original Mac patch cannot be compared; PR #18's validation code is already
on the verified main baseline. No other uncommitted Mac change is assumed merged.

## Step 3 — CI coverage and explicit skip boundaries

CI now creates `sec_reader NOLOGIN` **before** migration 010 grants, enables
bulk-storage and issuer-history database checks, prints skip reasons (`-ra`),
and runs the real committed Microsoft table→cell→fact/source and section
persistence tests in their own empty disposable database. A fixture preparation
script generates exports from hash-verified committed sources; it does not
contact SEC or copy receiving database IDs.

Three small hash-pinned **synthetic** regression models cover NKE-discovered NIL
representation, NTAP-style opening-cash period review, and bank split/continuation
layout. Five new fixture tests passed; these models are not real issuer source
acceptance or a substitute for the unavailable multi-company original archive.

Local disposable PostgreSQL is 16.2; hosted CI is configured for PostgreSQL 17.
Finance reader permissions + bulk + new fixtures: 37 passed. The first full run
used an application name equal to the test DB and correctly failed the isolation
guard. After fixing it, 1,239 passed / 146 skipped / 2 setup errors (bulk test DB
name was not set); the 37-test focused rerun includes those bulk checks with the
correct name and passes. Final aggregate results are recorded below when run.

Remaining archive skips are explicit: private Apple, Clorox, Citi, Fiserv,
Coherent, XOM, Packaging Corporation and other historical split/combined/
continuation/marker sources are absent. The normal aggregate intentionally
skips dedicated runner suites; CI executes ingestion, Microsoft replay and
relationship suites separately. No unavailable archive test is relabeled passed.

Real Microsoft PostgreSQL replay: **7 passed**, 1,622 facts, three physical
statements, 283 unique table/fact memberships; stable committed replay and exact
baseline restoration. Separate real relationship/proposal suite: **22 passed**;
2,285 anchor cells, 284 references, three persisted section mappings with replay
and caller rollback verified. `actual_chunk=not_supplied`, shared database not
validated/deployed. These local IDs are not shared RDS identities.

## Step 4 — capacity configuration

The chosen `MaxDatabaseGiB` now flows through launcher, read-only preflight and
ingestion via common byte conversion/boundary checks; default 14 GiB preserved.
48 capacity/progress/handoff tests passed, including custom 7/25 GiB limits one
byte below, exactly at, and one byte above threshold. Launcher forwarding is
statically checked; PowerShell is unavailable in this cloud environment, so no
Windows process-launch execution is claimed. Documented a read-only, fresh RDS
FreeStorageSpace check and operator reserve outside this DB's footprint. No AWS
login, shared RDS query, infrastructure resize or resource change was performed.
