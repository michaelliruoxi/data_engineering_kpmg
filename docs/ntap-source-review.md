# NetApp source-tagging review

The two NetApp warnings concern different original 10-Qs. Both are genuine
source-tagging inconsistencies. The parser must preserve the reported XBRL
values and dates; neither warning is permission to silently replace a fact or
mark a processing set passed. The investigation below was reproduced on
November 9, 2026 from original HTML and the SEC's extracted XML.

The [machine-readable evidence](validation/ntap-source-review-2026-11-09.json)
records source URLs, hashes, original occurrence IDs, locations and diagnostics.
No database connection, migration, correction or serving-selection change was
performed by this review.

## Shares: an untagged final digit

- Filing: **0000950170-24-023115**, filed February 29, 2024.
- [Original HTML](https://www.sec.gov/Archives/edgar/data/1002047/000095017024023115/ntap-20240126.htm)
  and [extracted XML](https://www.sec.gov/Archives/edgar/data/1002047/000095017024023115/ntap-20240126_htm.xml).
- Visible shares outstanding as of February 23: **206,376,661**.
- Inline occurrence `F_ecfeb3b1-4b41-4de2-80ba-09a49da5b9c2` encloses only
  `206,376,66`; the final `1` is in the next sibling span, immediately before
  the word `shares`. Its XML value is consequently **20,637,666**.

This is a numeric-tag boundary error, not a missing digit that the extractor
should guess. Registry 4/5 `num-dot-decimal` allows grouping separators anywhere
in the integer part. The previous strict groups-of-three check therefore rejected
a valid transformation while failing to explain the actual source defect.
The [registry definition](https://www.xbrl.org/Specification/inlineXBRL-transformationRegistry/REC-2022-02-16/inlineXBRL-transformationRegistry-REC-2022-02-16.html)
is the basis for the corrected transformation grammar; older registry rules remain
separate.

The correspondence diagnostic now records the tagged value, complete adjacent
visible number and both original locations. It deliberately handles only a
bounded untagged-digit pattern, with checks against footnotes, hidden content,
separate numeric facts and whitespace-separated text. It is not a general visual
layout interpreter. Fact coverage retains review status with this specific
explanation. Ingestion validation rejects the confirmed boundary problem before
successful sealing/commit; it does not substitute the visible number into raw facts.

## Opening cash: the wrong instant date

- Filing: **0001193125-26-380207**, filed September 2, 2026.
- [Original HTML](https://www.sec.gov/Archives/edgar/data/1002047/000119312526380207/ntap-20260731.htm)
  and [extracted XML](https://www.sec.gov/Archives/edgar/data/1002047/000119312526380207/ntap-20260731_htm.xml).
- Cash-flow duration: **April 25 through July 31, 2026**.
- Opening cash of **$2,075 million** in occurrence
  `F_53290b14-2c99-4abd-8568-269fa89e91d2` uses instant **April 25**.
- The same filing's cash reconciliation reports the same concept, amount,
  entity and currency at **April 24**, occurrence
  `F_b24e750f-c266-462a-a044-a7914d4ef326`.

The existing `opening_balance_date_not_prior_day` rule is correct. A date-only
XBRL instant denotes the end of that day, whereas a duration start date denotes
the beginning of the day. See the [XBRL date explanation](https://www.xbrl.org/dates-in-xbrl/).
The opening balance for the April 25 duration is therefore supported at the
April 24 instant. The comparative year's opening occurrence follows that rule.

`corroborate_opening_cash_issue` supplies a separate diagnostic from the original
visible occurrences, contexts and units. It requires matching scope and amount,
checks the core cash-flow duration, and abstains on unsupported, ambiguous or
contradictory evidence. It leaves the original April 25 fact, its issue,
`review_required` status and sealed V4 payload unchanged.

## Reproduce and hand off

Given the original HTML/XML pair, run:

```sh
uv run python scripts/review_filing_source_issues.py \
  --html /path/to/ntap-20260731.htm \
  --xml /path/to/ntap-20260731_htm.xml \
  --output outputs/ntap-cash-review.json
```

Run the same command with `ntap-20240126` for the shares case. Exit 2 is the
expected result for both originals: the source issues remain real even when
their causes are established. Exit 1 means validation failed. Exit 0 only means
these bounded checks found no issue; it is not universal financial acceptance.
The command neither changes sources nor accesses the database.

The facts validation policy revision invalidates older cached successes and
processing receipts that did not check numeric-tag boundaries. It does not
migrate immutable stored records. Dots must use supported versioned reprocessing
for existing data and retain these source discrepancies in its reconciliation.
Exposing corrected values requires a separate explicit curated-data contract
that keeps original facts and provenance. There is currently no API that can
safely clear either source warning by setting a status to `passed`.
