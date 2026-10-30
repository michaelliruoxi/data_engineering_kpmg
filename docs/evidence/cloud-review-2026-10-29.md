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
XML and XSD files match their SHA-256 receipts; the index HTML has no recorded
hash in that manifest and is not counted as hash-verified. This sample was
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
| Committed sample receipt hashes | 3/3 manifest-listed files match |
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
