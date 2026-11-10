# PR #22 integration — November 9, 2026

The integration combines draft [PR #22](https://github.com/michaelliruoxi/data_engineering_kpmg/pull/22)
with current main and the existing local capacity-limit work. Its isolated
checkout is `output/pr22-integration-20261109/`. The original working branch and
existing edits are preserved; that original checkout still predates PR #20.

Verified inputs:

- Current main: `f8d18e2825dbf34d556764c70e2255bce8bdfc8f`, including merged
  PRs #19, #20 and #21. All 250 tracked files in the isolated baseline match
  the upstream Git blob hashes.
- Open draft PR #22: `17451eb6ec429940716ec4273659b04e4e80e485`,
  `jazzy/historical-table-validation`. Its original PostgreSQL CI passed.
- The concurrent section investigation is recorded in
  [section-boundary-investigation-2026-11-09.md](section-boundary-investigation-2026-11-09.md).
  PR #20 restores the 13 missing keys in the seven reported filings. The
  investigation independently reports 233 archived section audits passed;
  that is not financial-table acceptance or a live database inventory.

## Reconciled changes

- Keep PR #22's original-source table reconstruction, distinct relationship /
  source / section results, explicit old-MSFT recheck, receipt disagreements,
  missing-filing denominator and actual table-chunk boundary.
- Include the already-merged PR #21 fact-format fixes. Its number-word and
  historical numeric-format support is now in the runtime baseline, rather
  than an unmerged reference copy. The NTAP irregular share-count display and
  the separate opening-cash context issue remain review items. Missing label
  archives and original-source receipts remain missing evidence.
- Filter supplemental manifests to the confirmed 2021-01-01 through
  2026-12-31 original-filing window. Record excluded accessions and their
  origins. Missing/invalid dates and conflicting CIKs stop before output.
  New in-window discoveries remain visible, with missing-database counts
  separate from the old 233-filing section snapshot.
- Require a new, empty scoped-validation output directory. Reusing a directory
  could previously overwrite evidence and include stale per-set snapshots.
- Preserve the local capacity changes with current main's shared byte-limit
  helpers. Both PowerShell launchers forward the same configurable ceiling;
  preflight reports success only below the ceiling. No storage was resized.

The two scope/output regressions were reproduced on the unmodified PR before
their fixes. Focused validation then passed: 254 parser/facts/table tests with
2 opt-in skips and 233 subtests; 64 capacity/preflight/progress/integration
tests. These suites overlap and must not be added as a unique test total.
The full local run produced 1,351 passes, 235 explicit skips and 937 passing
subtests. Six CLI subprocess checks initially failed because the borrowed
interpreter's relative `PYTHONPATH` no longer resolved after changing directory.
All seven tests in that module passed with the isolated source directory as an
absolute `PYTHONPATH`; no code or assertion was weakened. Across the full run
and that targeted rerun, all 1,357 runnable test cases passed. PostgreSQL and
optional archive skips are not passes. Hosted PostgreSQL results are recorded
on the PR. Whitespace checks, Python compilation and CLI help also passed.
PowerShell argument flow was inspected; Windows execution was not available.

## Handoff to the reprocessing work

1. Use the integrated PR revision after review; do not work from the old
   `c70ce55` checkout or reapply the section regex fixes. The seven screenshot
   filings miss 13 keys on that old code, not 14. The section investigation
   records 36 affected archived filings / 77 missing keys, but text formatting
   changes are broader, so those 36 are not the migration denominator.
2. Implement supported additive report/table versions across adapters,
   immutable storage, parser receipts, readers and a forward migration. A new
   set key or invented CLI version does not bypass `ParserRevisionConflict`.
   Retain the old supported tuples and preserve historical records.
3. Refresh SEC discovery at the actual run cutoff and query live database
   inventory. The November 1 report's 21 present / 212 absent out of 233 and
   20 HTML receipt disagreements are dated evidence, not current counts.
4. Reconcile original bytes and NTAP context evidence; retain unresolved cases
   as blocked/review. Validate original-DOM boundaries independently from
   range coverage and table semantics independently from section passes.
5. Use one scoped writer, transactional canaries, verified serving selection,
   per-filing checkpoints and rollback selections. Query actual stored and
   served output before claiming completion. Actual Ruby table-chunk producer
   samples and independent acceptance remain separate requirements.

The capacity launchers documented here retain the existing global queue and
date window. They are not the ten-company migration runner; do not use them as
a substitute for the separate scoped reprocessing queue.

Direct conversation access is unavailable in this session. This file records
the coordination state for the other project conversations; no message was
sent to them. No shared database write, migration, backfill or serving change
has been performed by this integration task.
