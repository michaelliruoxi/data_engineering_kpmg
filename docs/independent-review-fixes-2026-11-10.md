# Independent review implementation — November 10, 2026

This change integrates the reviewed PR #22 tree `99846c7494d584b26aa8fe64704622bd155ec2ae`
with the PR #24 tree `027e11f9e3c6087cc7edd933f9b6d7a20e0f8e54` and addresses
the independent main review and financial-extraction addendum. The original root
checkout and both prior review snapshots are preserved.

## Findings and implementation

| Finding | Implementation |
| --- | --- |
| 1: Trailing financial statements absorbed by Item 16 | Fixed: heading-independent selected-table/financial-section invariant in parsing and stored acceptance, followed by broader appendix recognition. `test_primary_statement_section_gate.py` and all five unchanged handoff cases. |
| 2: Section audit checked only key inventory | Fixed: Shared acceptance gate checks original navigation positions, order, TOC starts and unsupported combined Item headings. Split-block labels, class-styled headings and unsupported Item modifiers cannot silently pass under another section. Ambiguity fails closed. Evidence: `report_validation.py`; `test_review_section_boundaries.py`. |
| 3: Stale receipts block the global queue | Fixed: Preserve historical ingestion status; mark stale receipts separately, continue pending work and retain incomplete/review status. Evidence: `backfill.py`; `test_ops_review_regressions.py`. |
| 4: Chained rollback re-publishes withdrawn output | Fixed: Migration 012 adds publication/rollback event identity and explicit reversion lineage. Evidence: `publication.py` and migration 012; `test_publication_review.py`. |
| 5: Unpublished sets leak through default readers | Fixed with an explicit reader decision: default source analysis remains served-only; additive migration 014 restores preferred legacy `chunk_catalog`. Populated-upgrade and coexistence tests in `test_publication_review.py`; owner confirmation required before shared migration. |
| 6: Regenerated manifest rejects identical originals | Fixed: `processing_identity.py`, `backfill_v4.py`, `source_set_storage.py`, and `filing_ingestion_v4.py` compare complete stable identity and preserve first configuration/digests, processing ID and ingested queue status. Both manifest variants reproduced in hosted PostgreSQL before the fix; unchanged end-to-end assertions in `test_manifest_replay_postgres.py`. |
| 7: Ten-company universe rejected | Fixed: Explicit scoped-universe format retains separate strict S&P 500 validation. Evidence: `universe.py`; `test_ops_review_regressions.py`. |
| 8: Missing migration fails every queue item | Fixed: Global migration checksum preflight before per-filing attempts. Evidence: `backfill.py` and V5/V6 preflight; `test_ops_review_regressions.py`. |
| 9: Supplemental documents cannot pass acceptance | Fixed: Permit unclassified non-primary documents only; retain exact source, chunk and primary-boundary checks. Evidence: `corrected_acceptance.py`; `test_corrected_acceptance.py`. |
| 10: Version labels hide parser changes | Fixed: Distinct parser revisions plus additive reviewed V6 profile; receipt revisions exposed in `finance.processing_revision`. Evidence: `parser_policy.py` and migration 015; `test_parser_policy.py` and `test_publication_revision_upgrade.py`. |
| 11: Inventory matched ticker rather than issuer | Fixed: Normalize and match CIK; NULL or lower-case display tickers do not imply missing data. Evidence: `shortlist_table_validation.py`; `test_ops_review_regressions.py` covers CIK inventory matching. |
| 12: Capacity launcher changes missing from PR #24 | Fixed: Integrated PR #22 capacity forwarding and strict PowerShell argument binding. Evidence: `scripts/check_backfill_ready.py`, `run_backfill.ps1`, `start_backfill.ps1`; capacity/preflight regression tests, Windows execution unverified. |
| 13: Chunks start inside numbers/words | Fixed: `paragraph-v2` snaps overlap to whitespace, retains exact slices and complete coverage, and rejects indivisible tokens exceeding the size limit. `paragraph-v1` remains reproducible. Evidence: `chunking.py`; paragraph-v2 regressions and 234-source chunk audit. |
| 14: Table mapped across a rejected boundary | Fixed: Return ambiguous when rejected intervening boundaries invalidate the claimed section. Evidence: `table_sections.py`; `test_review_section_boundaries.py`. |
| 15: Evidence failure misreports committed publication | Fixed: `review_section_upgrade.py` reserves/flushes evidence before commit, records commit acknowledgement failures as `outcome_unknown`, removes empty failed reservations, and preserves `committed_evidence_incomplete` after a known commit. Nine CLI evidence tests pass. |
| 16: Printed number extends outside inline tag | Fixed: `fact_extraction.py` reads immediate text before parent traversal restrictions, including links/list/heading/Inline XBRL wrappers. Six unchanged regressions pass; hidden/superscript/sibling-link/block exclusions remain, including explicit `ix:hidden` regressions. Corpus scan retains exactly two hits. |
| 17: Historical month/day transforms unsupported | Fixed: Support the two reported 2015-registry formats with day/month validation. Evidence: `fact_extraction.py` and `inline_value_transforms.py`; inline transform regressions. |
| 18: Statement selection mixes registrants | Fixed: Resolve combined-filer identity first; require undimensioned primary evidence and one reporting entity, while preserving legitimate dimensional detail rows. Evidence: `tables.py` and `statement_headings.py`; statement selection/segment regressions. |
| 19: Numeric untagged cells classified as formatting | Fixed: Numeric data cells remain unresolved, including currency, footnote and Unicode-minus forms; note-reference headers remain explicit exceptions. Evidence: `tables.py`; `test_table_cells.py` and reconstruction regressions. |

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
only within the matching processing-set tuple. Apply migrations 011 through 015 to a disposable
test database first. Migrations 011–013 remain unchanged; 014 repairs the legacy
chunk reader and 015 updates parser-revision seal/publication guards. A new set name alone does not upgrade immutable artifacts.

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

## Initial integration validation

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
- Initial hosted PostgreSQL run 42766984050: 1,635 passed, 148 explicit skips,
  1,022 passing subtests, and one outdated finance-view inventory assertion.
  The expected inventory now includes `processing_revision`, retaining the
  exact schema inventory check and extending reader SELECT/no-write checks
  to that new view. This quick contract check runs before the full suite.

These are report/chunk and local code results, not complete financial-value or
live-database acceptance. Local database startup was unavailable because the
sandbox prohibits PostgreSQL shared-memory allocation; opt-in database skips
are not passes. The hosted workflow enables the new V4/V5/V6 coexistence tests
and committed Microsoft ingestion/replay/rollback tests for both V5 and V6.
The separate ingestion, relationship and citation stages were skipped after
the initial failed inventory assertion; their final results are recorded in
the PR along with the final revision's full-suite result.

## Integration and hosted validation

The fixes are committed in the isolated review checkout, on the branch of the
separate draft pull request. The original checkout's pre-existing tracked edits are preserved.
This integration preserves PR #24 head
`1efd5042df7e3248ac4bf0707c99edd2e189a8cc` and PR #22 head
`c68684943682108f407c8f2a5d64e06593c2f5db` as upstream parents. Local snapshot
commits have different history; compare complete Git trees when reconciling
local and published revisions.

The separate draft PR records its exact published head, Git tree and hosted
PostgreSQL results. Inspect those results before merge review; historical CI
runs from PR #22 or #24 do not validate these changes. No shared database
migration, backfill, serving change or deployment has run.


## Independent review follow-up

The starting published revision was `020b6d66c4353e89257c43f5d4ac8658d114507b`,
with tree `fbc12e387a32665df94ee5045c6e633a197f0e97`, matching local snapshot
`a665a99150240e3f3366768836f247c187c31d10`. Hosted run
[42927075809](https://github.com/michaelliruoxi/data_engineering_kpmg/actions/runs/42927075809)
passed every stage before these fixes.

All 12 supplied regression assertions were added unchanged and failed locally
before implementation. A regression-only ordinary fast-forward commit,
`6d8223ebcafecaebfd8c59c6a65607fa5877aa86`, then reproduced both manifest-replay
failures in hosted PostgreSQL. Run
[42945791801](https://github.com/michaelliruoxi/data_engineering_kpmg/actions/runs/42945791801)
had exactly **14 expected failures**, 1,636 passes, 148 skips and 1,022 passing
subtests: the 12 supplied cases plus unchanged document-set/core-manifest
reacquisition demoting an already-ingested queue entry to failed. M2 was fixed
only after that result. Regression assertions were not loosened.

The additional low items are handled as follows:

| Item | Resolution and evidence |
| --- | --- |
| Singular signature | Fixed in cleaner and independent audit with `SIGNATURES?`; annual handoff test and a quarterly stale-boundary audit regression. |
| Rollback when baseline changed | Documented an explicit operator withdrawal procedure in the upgrade runbook; automatic rollback still refuses an unreviewed fallback. Not executed on the shared database. |
| Commit-error/empty-file evidence | Fixed and tested in nine CLI cases; unknown outcomes no longer claim confirmed serving. |
| `fully_imported` meaning | Documented the deliberate label relaxation at the consuming backfill-status contract and in the runbook; semantic/source/publication gates remain strict. |
| Same artifact labels, changed parsing | Bumped report revisions to v4 and numeric-check revision to v3, explained all V4/V5/V6 effects and stale production `tables-v3` receipts. Added stale stored-acceptance and populated publication/rollback checks. |
| V5 policy label | Documented as a historical compatibility label; V5 remains available for history, and new remediation uses V6. No historical tuple is silently retired or relabelled. |

All 19 original findings have implemented resolutions. Findings 2–4, 7–14 and
17–19 retain the prior fixes and their existing regression assertions; findings
1, 6, 15 and 16 received the follow-up work above. Finding 5 has the deliberate
consumer behavior documented in the runbook; that decision and the actual
receiving-database affected-filing count remain prerequisites to applying the
migrations, not completed shared operations.

Corpus counts are recorded below. Final full-suite local results, the exact
published head, tree and hosted-run ID/results are recorded in
[PR #25](https://github.com/michaelliruoxi/data_engineering_kpmg/pull/25), since
a commit cannot contain its own eventual commit hash.

### Remaining verification limits

Shared migration history and the pre-migration affected-filing count were not
available; 011–013 were therefore preserved and only additive migrations added.
PowerShell execution, financial-value correctness for unavailable original XML
archives, issuer layouts outside this corpus, external index refresh and actual
table-chunk producer acceptance remain unverified. Source-only table selection
is unavailable for 23 archived filings and is reported explicitly, never counted
as a passing selected-table gate. The shared database, queue, serving selection
and indexes were not changed. Root checkout edits are preserved; its local
integration note now links to this neutral filename. Existing branch/history
names were retained as instructed; all new authored material uses neutral names.


### Final archived-source validation

Against the starting tree, all 234 unchanged source hashes were verified:
**233 report/chunk audits passed; one failed**. There are **3,962 sections and
44,673 paragraph-v2 chunks**. Exactly **104 filings change section output**, all
10-Q singular-signature splits; cleaned text changes in **zero** filings. The
five initially overmatched annual reference lists were corrected and are not
counted as changes. Broader appendix labels now require downstream original
statement-table evidence; the heading-independent gate remains strict.

The selected-primary gate ran on 211 filings: **210 passed and one failed**,
JPM `0001628280-26-054343`, whose heading audit was already unverified before
this change. Thus one previously unverified audit now explicitly fails, and
**zero previously passing filings newly fail**. The other 23 filings have
pre-existing unavailable statement selection and are listed separately in the
evidence; a report/chunk pass does not assert table-gate acceptance for them.

All six Take-Two annuals pass both the report/chunk audit and the selected-table
gate: `0001628280-21-010627`, `0001628280-22-014580`,
`0001628280-23-019851`, `0001628280-24-024623`,
`0001628280-25-026694`, and `0001628280-26-037434`.

The full numeric-boundary scan checked **267,390 tags** and retained exactly
**two hits**: MSFT `0001564590-22-015675` (16 to 16.7) and
NTAP `0000950170-24-023115` (206,376,66 to 206,376,661). Their original XML
values remain unchanged and require source review. These numeric outcomes and
the already unsupported JPM layout remain acceptance blockers, not grounds to
weaken the gates or change a serving preference.

[Machine-readable evidence](validation/reviewed-section-audit-2026-11-10.json)
contains every accession, source hash, section-output hash, baseline comparison,
statement mapping and numeric-boundary observation. Reproduce the current audit
with `scripts/audit_review_regressions.py`, that JSON as `--inventory`, the
archived project root as `--source-root`, and a fresh `--output` path.

Changed section-output accessions:

| Issuer | Count | Accessions |
| --- | ---: | --- |
| AAPL | 18 | `0000320193-21-000010`, `0000320193-21-000056`, `0000320193-21-000065`, `0000320193-22-000007`, `0000320193-22-000059`, `0000320193-22-000070`, `0000320193-23-000006`, `0000320193-23-000064`, `0000320193-23-000077`, `0000320193-24-000006`, `0000320193-24-000069`, `0000320193-24-000081`, `0000320193-25-000008`, `0000320193-25-000057`, `0000320193-25-000073`, `0000320193-26-000006`, `0000320193-26-000013`, `0000320193-26-000020` |
| CASY | 17 | `0000726958-21-000026`, `0000726958-21-000123`, `0000726958-21-000136`, `0000726958-22-000021`, `0000726958-22-000115`, `0000726958-22-000126`, `0000726958-23-000021`, `0000726958-23-000089`, `0000726958-23-000097`, `0000726958-24-000010`, `0000726958-24-000074`, `0000726958-24-000091`, `0000726958-25-000011`, `0000726958-25-000101`, `0000726958-25-000114`, `0000726958-26-000010`, `0000726958-26-000085` |
| JPM | 1 | `0001628280-26-054343` |
| MDT | 16 | `0001613103-21-000042`, `0001613103-21-000062`, `0001613103-22-000011`, `0001613103-22-000046`, `0001613103-22-000063`, `0001613103-23-000008`, `0001613103-23-000128`, `0001613103-23-000149`, `0001613103-24-000011`, `0001613103-24-000147`, `0001613103-24-000165`, `0001613103-25-000022`, `0001613103-25-000161`, `0001628280-25-053988`, `0001628280-26-011107`, `0001628280-26-060473` |
| MSFT | 18 | `0001564590-21-002316`, `0001564590-21-020891`, `0001564590-21-051992`, `0001564590-22-002324`, `0001564590-22-015675`, `0001564590-22-035087`, `0001564590-23-000733`, `0000950170-23-014423`, `0000950170-23-054855`, `0000950170-24-008814`, `0000950170-24-048288`, `0000950170-24-118967`, `0000950170-25-010491`, `0000950170-25-061046`, `0001193125-25-256321`, `0001193125-26-027207`, `0001193125-26-191507`, `0001193125-26-436133` |
| NTAP | 17 | `0001564590-21-009843`, `0001564590-21-046976`, `0000950170-21-005014`, `0000950170-22-002669`, `0000950170-22-017904`, `0000950170-22-025960`, `0000950170-23-005545`, `0000950170-23-045061`, `0000950170-23-067033`, `0000950170-24-023115`, `0000950170-24-101408`, `0000950170-24-130551`, `0000950170-25-029066`, `0001193125-25-189700`, `0001193125-25-297237`, `0001193125-26-076622`, `0001193125-26-380207` |
| PH | 17 | `0000076334-21-000029`, `0000076334-21-000111`, `0000076334-21-000225`, `0000076334-22-000007`, `0000076334-22-000018`, `0000076334-22-000049`, `0000076334-23-000009`, `0000076334-23-000028`, `0000076334-23-000064`, `0000076334-24-000008`, `0000076334-24-000020`, `0000076334-24-000067`, `0000076334-25-000011`, `0000076334-25-000021`, `0000076334-25-000071`, `0000076334-26-000008`, `0000076334-26-000073` |
