# Ten-company acquisition and Exhibit 21

This workflow supports Jace's acquisition task for **AAPL, CASY, MCK, MDT,
MSFT, MU, NKE, NTAP, PH, and TTWO**. The existing S&P 500 backfill validates
a roughly 500-issuer universe and combines acquisition with processing and
database loading. This smaller entry point reuses the existing SEC client and
source verifier, adds a ten-company inventory, and archives directly linked
Exhibit 21 documents alongside annual filings.

The script writes local files only. It does not connect to PostgreSQL, choose
processing versions, extract subsidiary relationships, or load reports/chunks.

## Scope and existing inventory

`config/shortlist.json` uses the user-confirmed inclusive **filing-date**
window, **2021-01-01 through 2026-12-31**, for original 10-K and 10-Q filings
of every listed company. The status is `user_confirmed`. Changing the
window or companies requires a new output directory; do not mix checkpoints.
Amendments and predecessor-issuer traversal are not supported by this wrapper.

Michael can run [export-shortlist-inventory.sql](export-shortlist-inventory.sql)
on an existing reader connection and save its single JSON cell as
`outputs/shared_inventory.json`. The query uses a read-only transaction and a
same inventory window, 2021-01-01 through 2026-12-31. It exports filing/source
records and legacy processing-set references; it does not assess V4 processing.
It has been checked against the repository schema but has not been run on the
shared database. Michael still owns processing and database acceptance.

Also obtain the existing source archive roots when available. A database record
does not establish that its local file bytes are accessible. The public sample
in this repository is not a complete inventory of the team's downloads.

## Run from the repository

Use Python 3.12 and the locked project environment (`uv sync --locked`). Run all
commands below from this repository's root. Output under `outputs/` is ignored
by Git; keep archives and exported inventory out of source commits.

### 1. Inspect available local originals (offline)

```bash
uv run --locked python scripts/shortlist_acquisition.py inventory \
  --config config/shortlist.json --output outputs/shortlist-20261028 \
  --archive-root .
```

Each `--archive-root` must be a data root containing either a root-level
`source_manifest.json` or `data/raw/sec/<accession>/source_manifest.json`, with
source paths relative to that root. Repeat the flag for multiple archives.
Only manifests and originals passing the existing source verifier count.
Identical copies are deduplicated by accession; conflicting copies are reported.

### 2. Discover and download

Set `EDGAR_IDENTITY` to your own real name and contact email in your terminal.
The SEC client reads it only at runtime. Do not use a placeholder, commit it,
or put it in a screenshot. With the configured scope confirmed:

```bash
uv run --locked python scripts/shortlist_acquisition.py discover \
  --config config/shortlist.json --output outputs/shortlist-20261028

uv run --locked python scripts/shortlist_acquisition.py download \
  --config config/shortlist.json --output outputs/shortlist-20261028 \
  --archive-root . --limit 10
```

`discover` checks ticker/CIK mappings against the SEC and reads recent and
relevant historical submissions pages. It saves raw JSON evidence and one
checkpoint per company. A resumed discovery skips completed companies: use a
new output directory to refresh them later.

`download` first verifies and copies reusable originals from the supplied roots,
then attempts at most `--limit` uncompleted filings (default 10). Repeat to
continue. It archives the filing index, primary HTML, extracted XBRL instance,
and extension schema using the existing downloader. Annual reports additionally
receive any directly linked `EX-21` / `EX-21.x` documents, with separate hashes
and an `exhibit21_manifest.json`. Existing original bytes are never replaced.
Other referenced exhibits are not downloaded. Joint-registrant identity
disagreements during wrapper resumption stop for review.

The existing SEC transport keeps requests sequential and at least one second
apart, verifies TLS, rejects redirects, and stops on access blocks. There is no
automatic retry loop around a blocked run. Resolve the cause before resuming.

On macOS, the equivalent guided launcher is:

```bash
bash scripts/run_shortlist_acquisition.command
```

The launcher derives the repository path, prompts for identity if necessary,
discovers the shortlist, downloads up to 10 filings, and regenerates inventory.
Repeat it for the next batch. Read `download_status.json` after each run.

### 3. Compare against the shared export

After obtaining Michael's JSON export:

```bash
uv run --locked python scripts/shortlist_acquisition.py inventory \
  --config config/shortlist.json --output outputs/shortlist-20261028 \
  --archive-root . --archive-root outputs/shortlist-20261028 \
  --database-export outputs/shared_inventory.json
```

The export must cover the entire configured window. Expected SEC counts and
shared-database counts stay `null` until their evidence is available. Local
absence does not mean absence from the shared database. `not_in_shared_database`
compares filing accessions only; source-record completeness and processing
quality require Michael's review of the export and the actual archives.

## Interpret results and hand off

| Output | Meaning |
| --- | --- |
| `discovery.json` | SEC filing list and per-company discovery checkpoint |
| `sec_api_evidence/` | Raw SEC JSON and retrieval/hash receipts |
| `download_status.json` | Per-filing archive progress and Exhibit 21 review flags |
| `data/raw/sec/<accession>/` | Original bytes, source manifest, and annual Exhibit 21 manifest |
| `inventory.json` | Verified supplied archives and available SEC/database comparisons |

`not_listed_in_index_review_required` means no direct Exhibit 21 row was found.
Review the annual report's exhibit references manually: it may incorporate an
earlier exhibit. This status is not evidence of unavailability. Subsidiary
extraction and GraphRAG relationships are not implemented here. Inventory's
`exhibit21_history_status` deliberately remains `not_verified`; use individual
exhibit receipts and the manual review record for historical acceptance.

Exit codes: **0** means the requested stage completed without detected local
errors; **2** means partial progress or review is required; **3** means SEC
access was blocked; **1** means setup or runtime failure. Inventory may return
0 with unknown team/SEC counts; its status stays `partial_evidence_only`.
Download completion is not shared-database completion or final task acceptance.

The acquisition handoff is complete when the scope is agreed, all ten companies'
SEC lists are checked against existing sources, each gap has verified originals
or a documented unresolved exception, and each annual Exhibit 21 is archived or
has an explicit manual-review outcome. Give Michael the manifests, original
archives, comparison JSON, and unresolved items for integration. Do not upload
private exports or the full archive to GitHub.

## Offline verification

```bash
uv run --locked pytest -q tests/test_shortlist_acquisition.py \
  tests/test_acquisition.py tests/test_filing_sources.py
```

The new tests cover scope boundaries, unknown counts, Exhibit 21 selection and
URL limits, immutable files, checkpoint reuse, and export validation. Real SEC
download and shared-database integration still need validation on a connected
team machine; offline test results do not establish historical completeness.
