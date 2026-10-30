# Offline report section validation

The 2026-10-29 source snapshot passed all **233** original filings in the
requested 2021–2026 filing-date window: **59 10-Ks and 174 10-Qs**. The audit
checked **3,704 sections and 44,057 chunks**, with zero repeated pipe separators,
missing expected Part/Item headings, source-location errors, or chunk gaps.
The latest available filing in this snapshot was dated 2026-10-28.
The [per-filing results](validation/report-sections-2021-2026.json) record
accessions, original SEC URLs, hashes, section inventories, counts, and scope limits.

| Ticker | 10-K | 10-Q | Result |
| --- | ---: | ---: | --- |
| AAPL | 5 | 18 | Passed |
| CASY | 6 | 17 | Passed |
| MCK | 6 | 17 | Passed |
| MDT | 6 | 17 | Passed |
| MSFT | 6 | 18 | Passed |
| MU | 6 | 18 | Passed |
| NKE | 6 | 18 | Passed |
| NTAP | 6 | 17 | Passed |
| PH | 6 | 17 | Passed |
| TTWO | 6 | 17 | Passed |

Run the cleaner and section-bounded chunker against local original SEC HTML:

```sh
uv run python scripts/validate_report_sections.py \
  --manifest data/section-validation/corpus_manifest.json \
  --output data/section-validation/audit.json
```

The default scope requires both `10-K` and `10-Q` sources for AAPL, CASY, MCK,
MDT, MSFT, MU, NKE, NTAP, PH, and TTWO. Every selected manifest entry is checked.
Use `--ticker AAPL MSFT --form 10-Q` to explicitly restrict a diagnostic run.
This command reads local files and does not download, ingest, or access a database.

The manifest is an object with a `filings` array. Each record supplies `ticker`,
`form`, `accession_number`, `html_path` relative to the manifest, and preferably
the original bytes' `sha256`. `expected_section_ids` is the exact list of Part
and Item headings independently reviewed in that original filing. Record the
review method and original table of contents XPath under `expectation_evidence`.
Do not generate the expected identifiers with the parser being tested. Omitted
Items are valid only when the original source omits them; the checker does not
assume every possible SEC Item appears in every filing.

When explicit expectations are absent, the audit derives them independently
from original DOM table of contents rows: Item/Part labels, resolving internal
navigation targets, and page columns. It supports a separate Item-number column,
Part/Item labels sharing a row, and unlinked financial-statement group labels
whose child rows contain the page links. For sources without a TOC, it reads
explicit emphasized or underlined Part/Item blocks from the original DOM.
These body headings also supplement legitimate sections omitted from a TOC.
The report parser and its vocabulary never supply expected identities. Ambiguous,
partial, or unsupported outlines stay `unverified`; the JSON records original
row/block XPaths and target IDs for review.

For historical scope, include `filing_date_from`, `filing_date_to`, and
`companies[ticker]` with `discovery_status: "complete"` and `discovered_count`.
Every discovered accession must appear in `filings`, including failed downloads.
The audit rejects incomplete discovery, missing inventory rows, and dates outside
the declared window. Historical completeness applies to the discovery snapshot;
future filings after that snapshot cannot be verified in advance. Acquire the
requested 2021–2026 sources with the resumable companion command:

```sh
uv run python scripts/acquire_section_validation.py \
  --output data/section-validation --since 2021-01-01 --until 2026-12-31
```

That command reads the authorized SEC identity from `EDGAR_IDENTITY`; identity
values are not stored in the corpus manifest or audit output.
Resume reuses the saved discovery snapshot. Use a new output directory when
refreshing for newly filed reports or changing the date range/company scope.

```json
{
  "filings": [{
    "ticker": "AAPL",
    "form": "10-Q",
    "accession_number": "0000320193-26-000001",
    "html_path": "sources/filing.htm",
    "sha256": "original-file-sha256",
    "expected_section_ids": ["part-i", "part-i-item-1", "part-i-item-2"],
    "expectation_evidence": {"method": "Original TOC review", "source_xpath": "/html/body/table[1]"}
  }]
}
```

The JSON output contains source hashes, every section title, exact text offsets,
original DOM locators, and actionable failures. It checks repeated pipe separators,
unclassified sections, missing or extra expected headings, headings accidentally
starting in TOC links, locator resolution and heading text, continuous section
coverage, exact chunk slices, section boundaries, and full chunk coverage.
Recognized annual Items whose semantic taxonomy is `unknown` remain distinct
from unclassified text.

Exit status is `0` only for a passing, explicitly scoped corpus; `1` means a
check failed; `2` means coverage is incomplete or independent heading expectations
are missing. Missing original bytes never count as checked ticker/form coverage.
A successful local audit does not verify already stored database output or
establish every table's financial meaning. Historical discovery is checked against
the declared inventory and remains limited to its source discovery snapshot.

## Output and existing database records

The report renderer omits empty layout cells from readable rows and places a
single separator between nonempty cells. The separate structured table grid
continues to retain original spans and empty cells. Table extraction V2–V4
keeps row-group labels such as `ASSETS:` out of column headings and combines
alternate physical value anchors into one presentation column when the original
date header establishes that they are the same column. V1 table output stays
unchanged.

Source-backed heading handling supports issuer-specific annual titles, Part III
introductory proxy notices, and a quarterly Exhibit index without a printed Item
number when its original TOC explicitly links Item 6 to that nearby index.
Older Item 1C executive-officer sections retain their governance meaning;
cybersecurity titles receive the cybersecurity type. Original titles and DOM
locators remain available for every detected section.

These fixes change report and table payloads. Parser policy revisions invalidate
stale cached dry-run success and reject older same-set processing receipts before
stage writes. They do **not** change immutable storage identities or upgrade
existing database rows. Reprocessing existing datasets requires explicit new
storage versions or a reviewed migration; changing a set key alone is not an
upgrade. No database migration or data reload is performed by this PR.
