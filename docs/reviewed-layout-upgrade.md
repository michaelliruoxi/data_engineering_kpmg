# Reviewed source-layout upgrade

The independent-review corrections use a separate V6 profile. Use this profile for
new remediation runs; V4 and V5 remain supported for identifying historical
artifacts. Reusing a processing-set name does not upgrade stored content.

| Artifact | Previous corrected profile | Reviewed profile |
| --- | --- | --- |
| Profile | `corrected-layout-v5` | `reviewed-layout-v6` |
| Reports and section identities | `report-v2` | `report-v3` |
| Tables, cells and review issue keys | `tables-v5` | `tables-v6` |
| Chunk algorithm | `paragraph-v1` | `paragraph-v2` |
| Default processing-set key | `automated-v5` | `automated-v6` |
| Facts | `facts-v1` | `facts-v1`, exact immutable payload agreement required |

V6 creates fresh report/table identities for the revised source boundaries,
registrant checks and numeric-cell classification. Paragraph V2 snaps overlap
starts to token boundaries. Historical Paragraph V1 behavior remains available.
All three supported document-set version tuples are retained. Old receipts
remain unchanged; the current parser refuses to reuse a stale receipt.

Apply additive migrations 011 through 015 to the explicitly selected receiving
database before ingestion. V5 and V6 check all five recorded checksums before any
filing work. Migration 013 extends the existing table/source consistency guards,
checks exact report/chunk membership, and exposes actual stored parser receipts
through `finance.processing_revision`. A NULL historical revision remains NULL;
it is never filled from today's parser policy.

## Reader behavior and pre-migration inventory

The chosen contract keeps default source analysis on the single served set.
This deliberately narrows the older V4 analysis views: when `finance.filing`
serves the legacy family, `sec.analysis_ready_facts` and
`sec.analysis_ready_statement_cells` return no source rows, even if a passed V4
set exists. Returning both historical V4 and published corrected rows would
mix versions and could duplicate financial values. Historical source reads
must select an explicit `processing_set_id` from `sec.source_fact_catalog` or
`sec.source_statement_cells`.

| Consumer | Selection after the upgrade |
| --- | --- |
| `finance.*` | Current served family and set |
| `sec.analysis_ready_facts`, `sec.analysis_ready_statement_cells`, `sec.source_chunk_catalog` | Current served source set only |
| `sec.chunk_citations` | Served versioned chunks, plus unbound legacy reports |
| `sec.section_catalog`, `sec.statement_catalog`, `sec.fact_catalog`, `sec.chunk_catalog` | Explicit preferred legacy set, even while a corrected source set is served |
| `sec.source_chunk_catalog_history`, `sec.chunk_citations_history` | All retained versions for explicit historical queries |

Migration 014 repairs `sec.chunk_catalog` by reading historical citations within
its exact preferred legacy report/chunk scope. It does not change any preference,
stored artifact, or default source-analysis filter. Migrations 011–013 are left
unchanged because their shared-database application status was not verified.
The reader tests exercise a populated 011-to-current upgrade and explicit
legacy preference through corrected publication and rollback.

Before applying migration 012 or the full upgrade batch, record this read-only
count on the receiving database (requires migration 010 or later). A nonzero
result identifies filings whose older source-analysis rows disappear while
their legacy family remains served; review those consumers before migration.

```sql
BEGIN TRANSACTION READ ONLY;
SELECT count(*) AS legacy_served_filings_with_passed_source_set,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM sec.source_processing_sets v4
           WHERE v4.filing_id = served.filing_id
             AND v4.validation_status = 'passed'
             AND v4.versions->>'tables' = 'tables-v4'
       )) AS legacy_served_filings_with_passed_v4_set
FROM finance.filing served
WHERE served.processing_family = 'legacy'
  AND EXISTS (
      SELECT 1 FROM sec.source_processing_sets source_set
      WHERE source_set.filing_id = served.filing_id
        AND source_set.validation_status = 'passed'
  );
ROLLBACK;
```

The shared-database count has not been run: its connection configuration was
unavailable. The behavior above is the implementation decision for owner
confirmation before migration; no shared migration or serving change has run.

## Ingestion and acceptance

Use a dedicated scoped universe and queue, reconciled original source archives,
and the established database capacity limit. For an existing offline queue:

```sh
uv run --locked python scripts/backfill_sec.py --dry-run --offline \
  --universe data/section-upgrade/universe.json \
  --state data/section-upgrade/queue.json --since 2021-01-01 --until 2026-11-09 \
  --data-root . --tables-version tables-v6 --limit 1

uv run --locked python scripts/backfill_sec.py --ingest --offline --validated-only \
  --universe data/section-upgrade/universe.json \
  --state data/section-upgrade/queue.json --since 2021-01-01 --until 2026-11-09 \
  --data-root . --tables-version tables-v6 --limit 1 \
  --env-file .env.aws --database sec_filings --max-database-gib 14
```

These dates and capacity reproduce the earlier investigation examples; use the
actual recorded scope and configured ceiling. This command does not create or
assert complete SEC discovery. Legacy stale receipts are retained for explicit
migration, while pending work can continue. Re-acquiring a manifest with the
same stable source membership does not overwrite its first acquisition receipt.

Follow the validation, publication and rollback steps in
[the corrected-section runbook](corrected-section-upgrade.md) using the returned
V6 processing-set UUID. V6 remains unpublished until explicitly selected. Inspect
`finance.processing_revision` alongside `finance.filing` to see the actual served
set and recorded parser revisions. Publication does not refresh external
retrieval indexes; rebuild and verify those consumers separately.

Do not interpret a successful local parser run as a completed receiving-database
migration or verified historical extraction. Preserve missing-source and semantic
review outcomes, and run transactional canaries before expanding the scope.

## Rollout sequence

Run each step on the ingest machine, in order. Steps 1, 3 and 5 are read-only.
The examples use the scoped queue from the commands above; substitute the
recorded scope, dates and capacity ceiling. On Windows use
`.\.venv\Scripts\python.exe` in place of `uv run --locked python`.

**1. Inventory the receiving database before migrating.** This runs the count
above and records it together with the migration ledger:

```sh
uv run --locked python scripts/check_upgrade_ready.py \
  --env-file .env.aws --database sec_filings \
  --output data/section-upgrade/evidence/before-migration.json
```

The evidence file lists every packaged migration as applied, pending or changed,
predicts what `sec-db migrate` would apply, counts stored processing sets by the
parser revisions they recorded (`stale_for_this_checkout` sets cannot be
replayed under their identity and need the V6 profile), counts the served
selections, and runs the reader-impact count. Exit 0 means nothing is pending
or the pending batch has no recorded concern. Exit 2 means the `assessment`
names a decision or a blocker: a changed or out-of-order ledger blocks
`sec-db migrate` itself; a nonzero reader-impact count, other active sessions or
a missing `sec_reader` role need an operator decision. The file is evidence, not
approval, and contains the database host name; keep it outside Git.

**2. Back up, rehearse and migrate** as described in
[the deployment guide](aws-deployment.md#applying-future-schema-updates). Run
step 1 again afterwards with a new output path; `pending` must be empty.

**3. Preflight the queue with the profile it will use.** The preflight checks the
migrations and tables of the selected profile, so pass the same profile and
scope as the import:

```sh
uv run --locked python scripts/check_backfill_ready.py --tables-version tables-v6 \
  --universe data/section-upgrade/universe.json \
  --state data/section-upgrade/queue.json --since 2021-01-01 --until 2026-11-09 \
  --max-database-gib 14
```

Without options it checks the October 13 `tables-v3` queue against migrations
001–009, as before. That default result says nothing about a V6 run: a database
at 001–009 passes it and cannot start `tables-v6`. `scripts/run_backfill.ps1`
and `scripts/start_backfill.ps1` remain the `tables-v3` launchers for the
October 13 queue; start a V6 run with the `backfill_sec.py` commands above.
`--env-file` and `--database` select another target, such as a restored
rehearsal copy. Only a loopback database may omit `verify-full` TLS.

**4. Dry-run, then ingest a one-filing canary** with the two `backfill_sec.py`
commands above. Replay it with `--replay` and confirm the same IDs and counts.

**5. Audit what was ingested.** One read-only command audits every reviewed set
the queue ingested into this database and records what each filing serves now:

```sh
uv run --locked python scripts/publish_reviewed_queue.py plan \
  --state data/section-upgrade/queue.json --data-root . \
  --env-file .env.aws --database sec_filings \
  --output-dir data/section-upgrade/evidence/plan-001 --limit 1
```

It runs `review_section_upgrade.py validate` for each filing, writes one
evidence file per filing and `plan.json`, and prints the plan's SHA-256. Queue
entries that are review-required, stale, from another profile or ingested into
another database are counted under `skipped_queue_entries` and never planned.
A set that is served now and no longer passes is listed under
`served_sets_failing_audit`; the command does not withdraw it.

**6. Publish the reviewed plan.** Review `plan.json`, then pass its SHA-256:

```sh
uv run --locked python scripts/publish_reviewed_queue.py publish \
  --plan data/section-upgrade/evidence/plan-001/plan.json --plan-sha256 PLAN_SHA256 \
  --data-root . --env-file .env.aws --database sec_filings \
  --output-dir data/section-upgrade/evidence/publish-001
```

Each filing is published through `review_section_upgrade.py publish` with the
selection recorded in the plan as its expected selection, so a filing whose
selection changed since the plan is reported as `selection_conflict` and left
alone. A changed plan file, or a plan made for another database, publishes
nothing. An unacknowledged commit stops the batch with exit 3 and
`outcome_unknown`; inspect `finance.filing` and the publication history before
continuing. Publishing the same plan again leaves already-published filings
unchanged. External retrieval indexes are not refreshed.

**7. Withdraw a batch** with the summary that publication wrote. Each filing is
restored to its exact earlier selection, or left alone if that baseline changed:

```sh
uv run --locked python scripts/publish_reviewed_queue.py rollback \
  --published data/section-upgrade/evidence/publish-001/publish-summary.json \
  --env-file .env.aws --database sec_filings \
  --output-dir data/section-upgrade/evidence/rollback-001
```

Use a new `--output-dir` for every run; earlier evidence is never replaced.
Repeat steps 4 to 6 with a larger `--limit` only after the canary's plan,
publication and rollback evidence has been reviewed.

Verification limits: the batch command is tested against the real review
command with only the database layer replaced. The inventory's SQL statements
are executed only by the hosted workflow's PostgreSQL tests in
`tests/test_upgrade_readiness.py`; check that run for the revision in use. None
of these commands has run against the shared database.


## Parser receipts and unchanged storage labels

This follow-up changes report section output and source acceptance in **V4,
V5 and V6**, because they share the cleaner and numeric correspondence checks.
It does not rewrite XML fact values or change the table-cell transformation or
chunking algorithms. Changed section boundaries can change chunk boundaries and
section associations even with the same chunk algorithm.

| Profile | Report revision now required | Facts/source-check revision |
| --- | --- | --- |
| V4 (`report-v1`) | `legacy-visible-layout-boundaries-v4` | `numeric-tag-boundaries-v3` |
| V5 (`report-v2`) | `corrected-visible-layout-boundaries-v4` | `numeric-tag-boundaries-v3` |
| V6 (`report-v3`) | `reviewed-original-source-boundaries-v4` | `numeric-tag-boundaries-v3` |

The table revision remains `registrant-numeric-cell-validation-v2`; paragraph-v2
remains `token-boundary-overlap-v2`. Every selected primary statement must map to
a section typed `financial_statements`, independently of the heading wording.
Parsing enforces that invariant for all three profiles; stored corrected-set
acceptance checks the actual relational sections again.

Existing receipts and artifact rows remain immutable. In particular, every
already-ingested production `tables-v3` queue receipt from the prior parser is
stale after this change: the shared `report-v1` and numeric-check revisions
changed. Resumption retains historical `ingested` status and result, marks
`stale_receipt`, excludes it from current verified completion, and continues to
pending work. A regenerated manifest with byte-identical sources and unchanged
membership, versions, profile and parser revisions is different: it reuses the
same processing-set ID and preserves the first manifest digests. Rebuilding the
core `source_manifest.json` has the same provenance-only treatment.

A new set key does not create new report/table identities. Existing legacy
artifacts can be remediated with the fresh V6 storage tuple. Already-stored V6
artifacts that need different content require further explicit storage versions
or a separately reviewed data migration; this change does not invent such an
upgrade or overwrite their content. Migration 015 requires current revisions
for newly sealed corrected sets and new publication. Existing serving pointers
and proven historical rollback remain intact; stale receipts cannot pass fresh
stored acceptance or be newly published.

V5 is retained for historical compatibility, not selected for new remediation.
Its `original-heading-v1` configuration label is a historical compatibility
label: the current parser also applies the stronger source-boundary and selected
statement checks. Use the stored parser revisions for the implementation
identity and V6 for new remediation. No V5 profile or historical receipt is
silently relabelled or deleted.

## Source acceptance measured on the complete shortlist

On November 11, 2026 the complete document sets of all **233** shortlist
filings (10 issuers, 59 annual and 174 quarterly reports filed from January 1,
2021 through October 29, 2026) were acquired and parsed with the reviewed
profile. Before the changes below, 209 were accepted and 24 rejected. Eighteen
of the rejections were not source defects; they came from four rules that were
stricter than the source evidence requires:

| Rejected because | Filings | Now accepted when | Recorded evidence |
| --- | ---: | --- | --- |
| A year-typed fact is printed as `2024 ` and extracted as `2024` | 8 (MSFT 6, NTAP 2) | A plain, untransformed `ix:nonNumeric` value differs from a whitespace-free XML value only by surrounding space, tab or line breaks. XML Schema collapses that whitespace for every item type except strings. | `inline_whitespace` with policy `xml-schema-collapse-single-token-v1` and the original text; `value_comparison` stays `exact` on the collapsed value |
| A text block contains the filer's own link to an anchor that no member document defines | 1 (MCK) | The identical literal `#fragment` link is in both sources and the anchor is absent from every member document | `target_status: absent_from_every_member` on that link; a link whose anchor exists has unchanged evidence |
| The closing cash rows read "Net (decrease) increase" or "Cash, cash equivalents and restricted cash" | 6 (CASY) | The labels identify the table; the dated opening, change and closing facts must still reconcile exactly with the three activity totals | unchanged `closure_evidence` |
| A caption such as "Three Months Ended June 30," is marked up over the first year column only | 3 (MCK) | The header holds exactly one such caption and otherwise only bare years, units or row labels, and the tagged facts in the bare-year column carry exactly the resulting period | `header_sources` cites the caption cell |

Text containing whitespace, an anchor that exists only in another member
document, duplicated or empty fragments, changed link text, balances that do not
reconcile, a second caption, and a tagged period that differs from the printed
one are all still rejected; each has a regression test.

Every one of the 209 filings accepted before the change parses to a
byte-identical payload after it (facts, source occurrences, reports, sections,
chunks, tables, issues and checks were fingerprinted with both versions). The
rules only turn a former rejection into an acceptance, so no stored receipt
changes content and the parser revisions are unchanged.

After the change 225 filings pass, 2 pass source verification with a
review-required cell, and 6 are rejected. All eight have a defect in the
filer's own tagging; none is repaired or guessed:

| Filing | Outcome | Source defect |
| --- | --- | --- |
| NTAP `0000950170-23-045061`, `0001193125-26-380207` | Review required | Opening cash balance is dated the first day of the period instead of the day before |
| CASY `0000726958-21-000078` (10-K) | Rejected | Goodwill in the April 30, 2021 column is tagged as of April 30, 2020 |
| MDT `0001613103-24-000011` | Rejected | A dash in the January 27, 2023 quarter column is tagged with the January 26, 2024 quarter |
| NTAP `0000950170-22-017904` | Rejected | Opening cash balance is dated April 29, 2021; the period begins May 1, 2021 |
| NTAP `0001193125-25-297237` | Rejected | The prior-year six months start on April 26, 2024, one day early (183 days) |
| NTAP `0000950170-24-023115` | Rejected | Printed `206,376,661` is tagged `206,376,66` |
| MSFT `0001564590-22-015675` | Rejected | Printed `16.7` is tagged `16` |

The first four rejected filings validate under `tables-v3`, which loads their
facts, report and chunks through its text-only fallback and records why the
statement tables are unavailable. The last two fail the numeric-tag check that
every profile shares, so no profile loads them. A review-required set is stored
and cannot be published.

These counts come from dry runs on archived sources and from a local PostgreSQL
rehearsal. They are not a statement about the shared database.

## Layouts accepted after the November 11 audit

The reviewed profile was then run on 73 filings from 26 issuers outside the
shortlist (banks, insurers, REITs, utilities, energy, retail, pharmaceuticals,
technology, telecom, transport; 10-K and 10-Q, filed 2021 and 2025-2026). Half
of the issuers were used to find rejected layouts; the other half were parsed
only once, after the changes were final. Two more combined-filer annual reports
(Duke Energy and NextEra, 2021) could be archived only after the DEI namespace
change below; both stop at statement selection and are not in the table. One
Wells Fargo 10-K still cannot be archived.

| Set | Filings parsed | Accepted before | Accepted after |
| --- | ---: | ---: | ---: |
| Issuers used to find layouts (13) | 37 | 9 | 25 |
| Issuers held out (13) | 36 | 12 | 20 |
| Further issuers, parsed once after every change in this document (12) | 24 | 12 | 14 |

None of the rejections removed below was a source defect. Each rule turns a
former rejection into an acceptance and leaves the stored payload of every
filing accepted before unchanged: all 227 accepted shortlist filings, the 233
passing archived primaries and the 33 previously accepted outside filings were
fingerprinted with both versions (facts, occurrences, reports, sections,
chunks, tables, issues, checks and table-section mapping evidence). The parser
revisions are therefore unchanged.

| Rejected because | Now accepted when | Still rejected |
| --- | --- | --- |
| A contents row gives "N/A" as the page of an Item | The row has no link and ends in N/A, Not applicable or None; the Item is then not expected in the body (`declared_not_applicable`) | Any other unlinked Item row in a linked contents table |
| An unlinked contents table with page numbers was read as body headings | A compact table with three or more Item rows and three or more page cells is navigation; expectations come from emphasized body headings | A report whose body headings do not cover the core Items |
| The contents list "Part I" and the body prints no Part heading | The independent body outline also finds no such Part heading | A Part heading the body outline finds and the sections lack; any missing Item |
| A contents link points to a page-top anchor above a running header | At most 160 visible characters, and no more than 400 elements, separate the anchor from its heading | A target separated from the heading by other text |
| "PART I" is followed by an introductory note before Item 1 | Every contents entry precedes that Part line and no Part or Item line lies between it and its first Item | A Part line inside the contents |
| The Item 8 heading shares its block with a linked index of statements | The links are inside a table nested in the heading block | A section located in a linked contents row |
| Item titles: "Intentionally Omitted", "[Removed and Reserved]", "Selected Consolidated Financial Data", "Consolidated Financial Statements...", "Disclosure" (singular), "Market Price of and Dividends on...", "Description of Business", "Controls & Procedures", "Interim ... Financial Statements", "PART I:" | The title follows its own Item number | "Issuer Purchases of Equity Securities" as a Part II Item 2 title, and every other wording |
| A period a few days longer or shorter than whole months (quarters closing on the Friday or Sunday nearest the quarter end) | The printed end date is the tagged one, the length is within seven days of the printed months, and the period starts the day after a date for which the filing reports an undimensioned balance | A context that starts a day early (the NetApp defect); any other length |
| A fiscal year named for the calendar year in which it starts ("2025" for the year ended January 31, 2026) | `dei:DocumentFiscalYearFocus` and `dei:DocumentPeriodEndDate` declare that naming, and the tagged year is a 52/53-week or month-end fiscal year | The same columns without that declaration |
| A duration caption marked up over the first value column only, printed in two cells or without its date | The header holds exactly one duration and at most one month-day cell, and the tagged facts carry exactly the resulting period | A second caption; a column tagged with another period |
| Opening and closing cash labelled by date ("at January 1", "– September 30") or by fiscal year | A total cash concept, and the named day is exactly the first or last day of the column period; the fact's own date must still equal the result | A day inside the period; an opening balance dated the first day of the period |
| A date stacked above its year in one cell ("September 30" over "2026") | The month, day and year parse as one date and the tagged facts carry it | Any other run of digits |
| Transformation Registry 3 date names (`datemonthdayyear`, `datemonthyearen`, `datedaymonthen`, `datedaymonthyearen`) | The transformed value equals the XML value | Unknown names and impossible dates |
| Combined filings under the dated 2019 and 2020 DEI namespaces could not be archived | The namespace is `dei/YYYY` or `dei/YYYY-MM-DD` | Any other namespace |

Two changes reject what was accepted before, and neither affects a local
filing. A contents row whose title cell, not its Item label, links elsewhere is
no longer a section start; the legacy profiles labelled 98% of the archived
JPMorgan 10-Q as financial statements from such a row, and now report that
text as an unclassified prefix. For that reason the JPMorgan row of
`validation/reviewed-section-audit-2026-11-10.json` no longer reproduces (12
sections instead of 13, still failed); the other 233 rows replay exactly with
`scripts/audit_review_regressions.py`. A primary document that is not UTF-8 is
rejected (`ReportEncodingError`) instead of being stored with U+FFFD
replacement characters.

The remaining outside rejections were left as they are: issuer-specific
statement titles, statements split between a 10-K and its annual-report
exhibit, combined-filer statement selection, bank cross-reference layouts and
untagged numeric cells. These counts come from dry runs on archived sources and
say nothing about the shared database.

## Audited profile (V7)

Some defects are in the stored content of filings the reviewed profile already
accepts. Stored reports and tables are immutable, and migration 015 pins the
reviewed parser revisions, so they are corrected under a separate opt-in
profile. V4, V5 and V6 keep producing exactly what they produced before.

| Artifact | Reviewed profile | Audited profile |
| --- | --- | --- |
| Profile | `reviewed-layout-v6` | `audited-layout-v7` |
| Reports and section identities | `report-v3` | `report-v4` |
| Tables, cells and review issue keys | `tables-v6` | `tables-v7` |
| Chunk algorithm | `paragraph-v2` | `paragraph-v2` |
| Default processing-set key | `automated-v6` | `automated-v7` |
| Parser revisions | reports `reviewed-original-source-boundaries-v4`, tables `registrant-numeric-cell-validation-v2` | reports `audited-section-metadata-v1`, tables `statement-continuation-inventory-v1` |
| Migrations | 011-015 | 011-016 |

Select it with `--tables-version tables-v7` wherever `tables-v6` is accepted
(`backfill_sec.py`, `check_backfill_ready.py`, `publish_reviewed_queue.py`).
Migration 016 re-creates the triggers and the citation view of 012-015 with the
new version added to their lists and seeds five section types; it changes no
existing row. Facts, source occurrences, statement cells, section boundaries and
section keys are the same as under V6. What differs:

| Stored under V6 | Under V7 | Shortlist (227 accepted filings) |
| --- | --- | --- |
| A heading that runs into its first sentence is stored whole as the title ("Item 1. Business. Parker-Hannifin Corporation is a leading...") | The title and `original_title` end at the heading; the sentence stays in the body at the same offsets | 38 sections in 4 filings; no title over 200 characters remains |
| `incorporation_status: unresolved` on any section that mentions the proxy statement, such as the cover page or the exhibit list | Only Part III sections, which defer their own content | 98 flags removed in 68 filings; the Part III flags remain |
| `signatures` typed `other_information`; Items 1B, 6 and 9C typed `unknown` and `unverified` | `signatures`, `unresolved_staff_comments`, `reserved` or `selected_financial_data`, `foreign_inspection_disclosure`, all `detected` | every filing |
| A raised note reference joined to the preceding text: "revenues1", "adjustments(1)", "Level 1(2)" | One space before a reference that is `<sup>`, superscript-aligned or raised by relative positioning, when it follows a digit, `%`, `)` or a word of three or more letters; ordinals (17th) and unit exponents (m2) stay joined | 2,335 spaces in 137 filings; no other character of any report changes |
| A tagged table printed as part of a selected statement is not extracted and not mentioned | `structure.omitted_continuations` on the statement's last segment lists each such table: its XPath, title, reason and tagged-fact count (policy `statement-continuation-inventory-v1`). Nothing is extracted from it | 19 tables with 182 tagged facts in 17 Casey's filings |
| An untransformed text value that differs from the XML only by XML Schema whitespace normalization is rejected unless it is a single token | Accepted when the XML value is the inline text with line breaks and tabs replaced by spaces, or with runs collapsed and the ends trimmed; `inline_whitespace` records the policy `xml-schema-whitespace-normalization-v1`, the facet and the original text | none locally |

The same comparison on the outside filings: every filing V6 accepts is accepted
by V7 with identical facts and cells (25 of the first set, 20 of the held-out
set and 14 of the third set, which was downloaded after the profile was
finished); V7 also accepts one Prologis 10-Q that V6 rejects for a postal code
printed after a line break. In all 61 primary documents of the second and third
sets the report text differs from V6 only by the inserted spaces, every one in
front of a note reference ("December 31, 2023 3" instead of "20233"). The inventory found one more omitted table, a
Pfizer supplemental cash-flow table with 12 tagged facts.

A V7 set is unpublished until it is selected, like a V6 set. The inventory makes
an incomplete statement visible; it does not complete it, and table coverage
stays `partial`.

## Operational status and evidence outcomes

`fully_imported` is deliberately an ingestion-completion label. This PR relaxes
that label to permit declared `tables=partial` and `reports=unknown` when all
four artifact classes are stored, the receipt passes, and no unaccounted cell
or source-quality issue remains. It does not relax source acceptance,
publication, semantic coverage or financial-value correctness. The status
consumer contract is also documented in [automated backfill](automated-backfill.md).

If publication or rollback loses its commit acknowledgement, the evidence has
`status=outcome_unknown`, `transaction_status=unknown`, and
`attempted_served_selection`; it does not claim the candidate is served. Inspect
the receiving database selection and publication history in a fresh read-only
transaction before retrying. If recording that outcome also fails, the command
emits a sanitized unknown-outcome message. A failed run removes its newly
reserved empty file. A known successful commit followed by evidence-write
failure still returns exit 3 with `committed_evidence_incomplete`.

## Manual withdrawal when the previous baseline changed

Automatic rollback intentionally refuses to guess a replacement baseline.
For a deliberate withdrawal, an operator must first review and approve the
current selection and the *new expected fallback*, and save a fresh inventory.
The following is an operator procedure, not an action performed by this PR:

1. Begin a transaction and call `sec.lock_processing_filing(filing_id)`.
2. Recheck `finance.filing` and the matching
   `sec.preferred_source_processing_sets` row. Stop if the current source-set ID
   differs from the reviewed ID.
3. Leave `sec.rollback_publication_event` empty and delete exactly that filing's
   matching source-preference row. Require exactly one deleted row. Do not
   disable triggers, change a legacy preference, or edit publication history.
4. Query `finance.filing` again. Commit only if its family and processing-set ID
   exactly match the newly approved fallback, including an explicitly approved
   empty selection; otherwise roll back the transaction.
5. Record the before/after selection and the appended history event. This is a
   new withdrawal (the existing trigger records a publish-kind event with a NULL
   new preference), not a claim to restore the old baseline. Refreshing external
   indexes is a separate action.

This manual procedure has not been executed on the shared database. Automatic
rollback's existing refusal and exact-history restoration tests remain in place.
