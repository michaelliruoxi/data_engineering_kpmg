# Claude review implementation — November 10, 2026

This change integrates the reviewed PR #22 tree `99846c7494d584b26aa8fe64704622bd155ec2ae`
with the PR #24 tree `027e11f9e3c6087cc7edd933f9b6d7a20e0f8e54` and addresses
Claude's main review and financial-extraction addendum. The original root
checkout and both prior review snapshots are preserved.

## Findings and implementation

| Finding | Implementation |
| --- | --- |
| 1: Trailing financial statements absorbed by Item 16 | Explicit financial-statements appendix, signature and executive-officer boundaries; real Take-Two source regression. |
| 2: Section audit checked only key inventory | Shared acceptance gate checks original navigation positions, order, TOC starts and unsupported combined Item headings. Split-block labels, class-styled headings and unsupported Item modifiers cannot silently pass under another section. Ambiguity fails closed. |
| 3: Stale receipts block the global queue | Preserve historical ingestion status; mark stale receipts separately, continue pending work and retain incomplete/review status. |
| 4: Chained rollback re-publishes withdrawn output | Migration 012 adds publication/rollback event identity and explicit reversion lineage. |
| 5: Unpublished sets leak through default readers | Default analysis/chunk/citation views follow serving selection; explicit history views retain audit access. |
| 6: Regenerated manifest rejects identical originals | Compare stable document identity, membership and source hashes; preserve the first manifest digest and record the new processing receipt separately. |
| 7: Ten-company universe rejected | Explicit scoped-universe format retains separate strict S&P 500 validation. |
| 8: Missing migration fails every queue item | Global migration checksum preflight before per-filing attempts. |
| 9: Supplemental documents cannot pass acceptance | Permit unclassified non-primary documents only; retain exact source, chunk and primary-boundary checks. |
| 10: Version labels hide parser changes | Distinct parser revisions plus additive reviewed V6 profile; receipt revisions exposed in `finance.processing_revision`. |
| 11: Inventory matched ticker rather than issuer | Normalize and match CIK; NULL or lower-case display tickers do not imply missing data. |
| 12: Capacity launcher changes missing from PR #24 | Integrated PR #22 capacity forwarding and strict PowerShell argument binding. |
| 13: Chunks start inside numbers/words | `paragraph-v2` snaps overlap to whitespace, retains exact slices and complete coverage, and rejects indivisible tokens exceeding the size limit. `paragraph-v1` remains reproducible. |
| 14: Table mapped across a rejected boundary | Return ambiguous when rejected intervening boundaries invalidate the claimed section. |
| 15: Evidence failure misreports committed publication | Reserve a new output file before database access and flush evidence before commit. A subsequent evidence failure reports `committed_evidence_incomplete` (exit 3). |
| 16: Printed number extends outside inline tag | Check contiguous numeric prefixes and suffixes independently of irregular digit grouping; require source review instead of accepting the tag. Separate facts, footnotes and block boundaries remain distinct. |
| 17: Historical month/day transforms unsupported | Support the two reported 2015-registry formats with day/month validation. |
| 18: Statement selection mixes registrants | Resolve combined-filer identity first; require undimensioned primary evidence and one reporting entity, while preserving legitimate dimensional detail rows. |
| 19: Numeric untagged cells classified as formatting | Numeric data cells remain unresolved, including currency, footnote and Unicode-minus forms; note-reference headers remain explicit exceptions. |

Additional fixes cover conflicting supplemental dates, atomic acquisition and
interrupted-download recovery, explicit offline supplemental-source errors,
status reporting, non-Git provenance, and avoiding whole-queue writes on
unchanged skips. The facts CLI now distinguishes unmatched/uncompared values
from matched values. Historical table validation reconstructs V4 through V6
through the correct transformation sequence.

The legacy `tables-v1` readable-row presentation remains frozen to preserve
immutable payloads. Consumers should use V2 or later (the reviewed profile uses
V6); those versions already preserve adjacent columns correctly. No historical
fact values are rewritten to disguise a source defect.

## Version and operational contract

Use [the reviewed upgrade runbook](reviewed-layout-upgrade.md). The supported
new tuple is `facts-v1 / tables-v6 / report-v3 / paragraph-v2`; migration 013
retains the older supported tuples and allows paragraph-v2 report membership
only within the matching processing-set tuple. Apply migrations 012 and 013 to a disposable
test database first. A new set name alone does not upgrade immutable artifacts.

Older records remain available. Stale receipts require versioned reprocessing
and must not be counted as verified completion. Pre-012 publication events are
marked `legacy`: their old schema cannot distinguish restoration from publication,
so automatic rollback refuses to infer missing lineage. Publication evidence
paths must be new files.

Implementation and tests do not constitute a live migration, backfill,
publication, or retrieval-index refresh. The shared database has not been changed.
PowerShell execution, absent original XML archives and external retrieval
consumers require separate acceptance evidence. Same-parser reconstruction is
a consistency check, not independent proof of every financial interpretation.

## Validation

Focused regressions cover the reported triggers, including original Take-Two
and Microsoft source, queue progress, source receipts and publication failures.
The November 10 retry produced these results (suites overlap):

- Full local run: 1,501 passed, 251 explicit skips, 981 passing subtests, and
  one failure in a heading test that supplied an empty source-fact inventory.
  The fixture now supplies valid undimensioned entity evidence; its original
  no-heading-recovery assertion remains unchanged. The entire affected heading,
  statement-selection and statement-segment suite then passed: 59 tests, 23
  optional skips and 75 passing subtests.
- Final numeric-boundary/correspondence checks: 119 passed, 4 optional skips
  and 140 passing subtests. Corrected/reviewed original-source parsing and
  adapter checks: 37 passed, 14 optional skips.
- Final section/audit regressions: 40 passed. Operational regressions:
  57 passed, including queue progress, acquisition recovery and publication
  evidence failures.
- A fresh archived-source audit with paragraph-v2 checked 234 filings:
  233 passed; one JPM filing remains unverified because an independent original
  heading inventory is unavailable. It produced 3,858 sections and 44,604 chunks
  with zero audit errors or oversized-token failures. All six Take-Two annual
  filings passed. Source hashes and results match the committed evidence JSON.
- Python compilation, CLI help and whitespace checks passed.

These are report/chunk and local code results, not complete financial-value or
live-database acceptance. Local database startup was unavailable because the
sandbox prohibits PostgreSQL shared-memory allocation; opt-in database skips
are not passes. The hosted workflow enables the new V4/V5/V6 coexistence tests
and committed Microsoft ingestion/replay/rollback tests for both V5 and V6,
but that workflow has not run on this implementation.

## Integration and hosted validation

The fixes are committed in the isolated checkout
`output/claude-review-fixes-20261110/` on
`codex/claude-review-fixes-20261110`. The original checkout is unchanged.
This integration preserves PR #24 head
`1efd5042df7e3248ac4bf0707c99edd2e189a8cc` and PR #22 head
`c68684943682108f407c8f2a5d64e06593c2f5db` as upstream parents. Local snapshot
commits have different history; compare complete Git trees when reconciling
local and published revisions.

The separate draft PR records its exact published head, Git tree and hosted
PostgreSQL results. Inspect those results before merge review; historical CI
runs from PR #22 or #24 do not validate these changes. No shared database
migration, backfill, serving change or deployment has run.
