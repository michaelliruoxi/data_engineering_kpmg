# Financial fact coverage, ten-company shortlist 2021–2026

**How this was produced.** Original SEC files (filing index JSON, extracted XBRL instance, extension schema, primary inline HTML) read on 2026-11-01 in the Claude desktop browser from www.sec.gov, and checked with a JavaScript port of facts-v1 extract_facts() and check_inline_correspondence() at commit 724623f. On the bundled Microsoft sample the port reproduces the Python results exactly. Inventory: docs/validation/report-sections-2021-2026.json. Rerun `python -m sec_pipeline.fact_coverage` on the acquisition archive for the authoritative result. Per-filing detail: [fact-coverage-2021-2026.csv](fact-coverage-2021-2026.csv) (one row per filing) and [fact-coverage-2021-2026.json](fact-coverage-2021-2026.json) (everything, including the XML SHA-256 of each filing).

Scope: 10 companies, 10-K, 10-Q, filing dates 2021-01-01 to 2026-12-31. Generated 2026-11-01T21:50:13+00:00 by `fact-coverage-v1 (browser survey)` (facts-v1 extraction and inline correspondence). Status: **review_required**.

- Filings: **233** — passed 232, review 1, failed 0, archive errors 0, missing archives 0.
- Facts extracted: **290,632**; occurrences not extracted: 0.
- Numeric values equal to the inline HTML: 259,598; not compared: 0.
- Periods: duration 169,884, instant 120,748.
- Units: USD 226,153, shares 11,837, pure 11,765, USD/shares 7,061, patent 415, EUR 387, segment 229, employee 207.
- Filings with no labels (label linkbase not archived): 210.

| Ticker | Filings | Passed | Review | Failed | Missing | Usable 10-K | Usable 10-Q | Facts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| AAPL | 23 | 23 | 0 | 0 | 0 | 5 | 18 | 20,139 |
| CASY | 23 | 23 | 0 | 0 | 0 | 6 | 17 | 11,488 |
| MCK | 23 | 23 | 0 | 0 | 0 | 6 | 17 | 36,634 |
| MDT | 23 | 23 | 0 | 0 | 0 | 6 | 17 | 44,266 |
| MSFT | 24 | 24 | 0 | 0 | 0 | 6 | 18 | 37,361 |
| MU | 24 | 24 | 0 | 0 | 0 | 6 | 18 | 29,525 |
| NKE | 24 | 24 | 0 | 0 | 0 | 6 | 18 | 31,313 |
| NTAP | 23 | 22 | 1 | 0 | 0 | 6 | 17 | 27,132 |
| PH | 23 | 23 | 0 | 0 | 0 | 6 | 17 | 25,502 |
| TTWO | 23 | 23 | 0 | 0 | 0 | 6 | 17 | 27,272 |

## Filings needing attention

| Ticker | Form | Filed | Accession | Status | Reason |
| --- | --- | --- | --- | --- | --- |
| NTAP | 10-Q | 2024-02-29 | 0000950170-24-023115 | review | 1 number(s) printed with irregular digit grouping, for example EntityCommonStockSharesOutstanding shown as '206,376,66' (XML 20637666); the XML value may be off by a power of ten |

## Limits

- Facts-v1 rules: an occurrence counts as extracted only when its value, period, unit and dimensions are represented exactly; failures are listed, never dropped.
- Core-concept anchors are a sanity check for the report period, not accounting validation; a company may legitimately use another concept.
- Labels come only from the archived extension schema. Older filings keep labels in a separate label linkbase that the four-file archive does not include.
- Without --discovery, missing filings cannot be detected; totals cover supplied archives only.

## Numeric inline correspondence by rule

The v1/v3/v3-text import adapters require zero values "not compared".

| Rule | Equal | Unequal | Not compared | Filings with any not compared |
| --- | ---: | ---: | ---: | ---: |
| `facts-v1` on main before PR #21 | 232,767 | 0 | 26,831 | 41 |
| `inline_value_transforms` (v4) before PR #21 | 256,930 | 0 | 2,668 | 32 |
| `facts-v1` in PR #21 | 259,598 | 0 | 0 | 0 |
| `inline_value_transforms` in PR #21 | 259,597 | 0 | 1 | 1 |

The one value the v4 module still does not compare is the NetApp share count printed "206,376,66".

## Per company

| Ticker | Filings | Registry 2/3 formats used until | Would fail import before PR #21 | Labels embedded in `.xsd` |
| --- | ---: | --- | ---: | --- |
| AAPL | 23 | 2021-07-28 | 3 | never (separate `_lab.xml`) |
| CASY | 23 | 2021-06-25 | 2 | never (separate `_lab.xml`) |
| MCK | 23 | 2021-08-05 | 3 | never (separate `_lab.xml`) |
| MDT | 23 | 2021-06-25 | 2 | never (separate `_lab.xml`) |
| MSFT | 24 | 2023-04-25 | 10 | 12 filings, from 2024-01-30 |
| MU | 24 | 2021-07-01 | 3 | never (separate `_lab.xml`) |
| NKE | 24 | 2021-07-20 | 3 | never (separate `_lab.xml`) |
| NTAP | 23 | 2023-06-14 | 10 | 11 filings, from 2024-02-29 |
| PH | 23 | 2021-05-06 | 2 | never (separate `_lab.xml`) |
| TTWO | 23 | 2021-08-03 | 3 | never (separate `_lab.xml`) |

Inline number formats, by number of filings using each: (none) 232, sec:2015-08-31 numwordsen 212, xbrl:2020-02-12 num-dot-decimal 165, xbrl:2020-02-12 fixed-zero 165, xbrl:2015-02-26 numdotdecimal 41, xbrl:2015-02-26 zerodash 31, xbrl:2022-02-16 num-dot-decimal 27, xbrl:2022-02-16 fixed-zero 15.
