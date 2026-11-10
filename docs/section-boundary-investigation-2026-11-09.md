**SEC section-boundary investigation — November 9, 2026**

The seven filings in the screenshot reproduce exactly with the current local
checkout's parser, but they contain **13 missing section keys, not 14**.
The underlying parser fixes already exist in merged
[PR #20](https://github.com/michaelliruoxi/data_engineering_kpmg/pull/20).
The remaining implementation work is to bring the working/deployed code onto
that revision and provide a safe versioned upgrade of older stored outputs.

This investigation regenerated outputs from original local HTML. It did not
query or modify the live database, change the parser, switch branches, or
reprocess stored records. Therefore, the counts below describe parser output,
not a confirmed inventory of defective live database rows.

**Code and evidence**

- Local branch: `codex/rds-capacity-limit`, commit
  `c70ce55402d20d6a343803efb2b6626a5d80cbfd` (before PR #20).
- PR #20 merge: `dfe430155bb73ca200a5bbf6ce4d72c5cf82489a`,
  October 29, 2026, 8:38 p.m. New York time.
- Current GitHub main's `report_cleaning.py` has the same Git blob as the
  PR #20 merge: `f2877e47528a2e7631117137bf318d85faeb2294`.
- The fixed cleaner and independent validator were loaded in isolation, using
  the unchanged local chunker and source-locator modules. Source SHA-256 hashes
  were checked against the saved original-source audit.
- Corpus: ten companies, 233 filings discovered through October 29, 2026.
  The newest filing in this snapshot is October 28. Filings arriving after
  this snapshot were not checked.

**The seven filings**

These are the newest 10-K and 10-Q per company in the archived snapshot,
not necessarily the newest filings available on November 9.

| Filing | Accession | Missing keys | Count |
| --- | --- | --- | ---: |
| MCK 10-K | 0000927653-26-000069 | Part II Item 6 | 1 |
| MDT 10-K | 0001628280-26-044354 | Part II; Part II Items 5 and 6; Part III | 4 |
| MSFT 10-K | 0001193125-26-323660 | Part II Item 6 | 1 |
| MU 10-Q | 0000723125-26-000015 | Part II Item 6 | 1 |
| MU 10-K | 0000723125-26-000023 | Part II Item 6; Part III; Part IV; Part IV Item 15 | 4 |
| NTAP 10-K | 0001193125-26-259683 | Part II Item 6 | 1 |
| PH 10-K | 0000076334-26-000105 | Part II Item 6 | 1 |
| **Total** | **7 filings** | | **13** |

**Root causes and fixes**

| Cause | Evidence and impact | Fix already in PR #20 |
| --- | --- | --- |
| A page footer is mistaken for a table-of-contents page column. | The old detector rejects a heading when its next nonempty line is numeric. Six annual Item 6 headings are immediately followed by page numbers: MCK 32, MDT 30, MSFT 33, MU 54, NTAP 36, PH 19. MU Item 15 is followed by 98. This accounts for seven missed Item boundaries. | Use original DOM navigation and contents-table evidence to exclude TOC headings. A numeric footer alone no longer rejects a source-backed body heading. |
| Annual Item 5 vocabulary is too narrow. | The old rule requires “MARKET FOR REGISTRANT”; MDT prints “Market for Medtronic’s Common Equity”. Its market and repurchase disclosure is consequently assigned to Part I Item 4, Mine Safety. | Accept bounded issuer-specific “Common Equity/Stock” title variants, retaining form and Item context. |
| Part headings require an empty interval before their first recognized Item. | MDT and MU have genuine proxy-reference introductory prose between Part III and Item 10. The old detector rejects both Part III boundaries and leaves their introductions in Item 9C. | Retain source-backed body Part headings with introductions; keep incorporated proxy content explicitly unresolved. |
| A body section has an alternate, unnumbered title. | MU 10-Q prints “Index to Exhibits”, while its original TOC explicitly links Item 6 / Exhibits to that body anchor. The old detector leaves the exhibit index inside Item 5, Other Information. | Resolve the original TOC link to the nearby unique body anchor; preserve “Index to Exhibits” as the original title and record the link evidence. Do not infer Item 6 from an unlinked title alone. |
| A missed Item causes a Part boundary to disappear too. | MDT Part II is skipped after Items 5 and 6 fail, because substantive market text lies before the next recognized Item. MU Part IV is skipped after Item 15 fails. The latter leaves annual exhibits under Item 14, Audit Fees. | Restore the Item boundaries and use source-backed Part handling. These are two dependent failures, not separate missing source documents. |

The relevant legacy rules are in
[`report_cleaning.py`](https://github.com/michaelliruoxi/data_engineering_kpmg/blob/c70ce55402d20d6a343803efb2b6626a5d80cbfd/src/sec_pipeline/report_cleaning.py):
Item 5 vocabulary at line 40, heading/page rejection at lines 125–129, and
Part selection at lines 158–170. The source-aware replacements are in the
[merged cleaner](https://github.com/michaelliruoxi/data_engineering_kpmg/blob/dfe430155bb73ca200a5bbf6ce4d72c5cf82489a/src/sec_pipeline/report_cleaning.py).

All 13 missing boundaries were located in the old cleaned text through their
original HTML nodes. The exhibits title in MU 10-Q is genuinely unnumbered;
the source relationship, rather than a literal body “Item 6” label, establishes
its identity. These findings support a boundary/classification defect for the
reported cases. They do not establish that every aspect of source extraction is
lossless.

The old and fixed outputs for all 20 snapshot-latest filings have identical
character sequences after removing whitespace and pipe separators. However,
their full text differs in **all 20 filings**, because PR #20 also removes
empty-cell/trailing table separators. Old offsets cannot be copied into the
new reports, even for filings whose section-key inventory is unchanged.

**Fresh verification**

| Scope | Old parser | PR #20 cleaner and independent validator |
| --- | --- | --- |
| 20 snapshot-latest filings | 7 filings missing 13 keys; no extra keys | 20 passed; all 13 keys restored |
| 233 archived filings | 36 filings missing 77 keys; no extra keys | 233 passed; 3,704 sections; 44,057 chunks; zero audit issues |

The full corpus comprises 59 annual and 174 quarterly filings. Fresh section
inventories and section metadata match the prior reviewed audit for all 233.
Checks include independently derived original-DOM heading expectations, source
locator resolution, heading offsets, section coverage, exact chunk text slices,
chunk containment and coverage, and repeated separators.

Historical missing-key counts are:

| Company | Filings checked | Filings with missing keys | Missing keys |
| --- | ---: | ---: | ---: |
| AAPL | 23 | 0 | 0 |
| CASY | 23 | 0 | 0 |
| MCK | 23 | 7 | 10 |
| MDT | 23 | 6 | 24 |
| MSFT | 24 | 6 | 6 |
| MU | 24 | 6 | 13 |
| NKE | 24 | 0 | 0 |
| NTAP | 23 | 5 | 5 |
| PH | 23 | 6 | 19 |
| TTWO | 23 | 0 | 0 |
| **Total** | **233** | **36** | **77** |

Missing-key counts do not measure every possible boundary-location difference
or every formatting change. Migration scope must be determined from actual
stored payloads and parser versions, not limited automatically to these 36.

**Proposed implementation**

1. **Adopt the existing parser fix.** Integrate PR #20/current main into the
   working branch while preserving the existing RDS/backfill edits, then verify
   the commit used by any scheduled worker. Avoid duplicating the regex fixes.
   Reproduce the seven filings first using the accessions above.

2. **Add an explicit storage-version upgrade.** Introduce a supported profile
   with a new report extraction version, such as `report-v2`, and regenerate
   sections and chunks under the new report IDs. The unchanged chunk algorithm
   can retain `paragraph-v1` because chunks are scoped to their report; use a
   new chunking version if its settings or policy also change. Update the
   adapter version checks, V4 parser's hard-coded report version, storage
   validation, receipt policy, and backfill profile together.

   This requires a forward database migration: the current V4 schema's
   `source_processing_sets.versions` constraint accepts only the
   `report-v1` tuple. Preserve support for the old tuple and explicitly add
   the new supported tuple; do not edit an already-applied migration or weaken
   source/version consistency checks. Relevant code is in
   `filing_adapter*.py`, `filing_parser_v4.py`, `source_set_storage.py`,
   and `migrations/008_source_document_sets.sql`.

   A new processing-set key alone is insufficient: report identity is
   `(source_document_id, extraction_version)`, and storage compares the
   entire immutable payload. PR #20's receipt guard deliberately rejects an
   older same-set revision before stage writes; it does not migrate data.

3. **Inventory and rebuild dependent outputs.** Read actual stored extraction
   versions, source hashes, and processing receipts to identify the affected
   population. Generate new reports, sections, chunks, table-to-section
   mappings, and chunk/citation links from the verified originals. Refresh any
   downstream retrieval index that consumes those chunks. Reuse facts and
   tables only where their payloads and validation contracts remain unchanged.
   PR #20 also changes table extraction V2–V4: adopting those changed table
   payloads requires new table storage versions or a separately reviewed
   migration, not a silent rerun under existing identities.

4. **Gate acceptance on semantic boundaries.** Run the independent original
   heading audit during the upgrade and record its result in the acceptance
   evidence. Existing range/coverage checks can pass when a missing section
   has simply been absorbed into its neighbor. Missing or unexpected source
   headings should block promotion; ambiguous source expectations should
   remain unverified. Preserve the explicit distinction between structural
   coverage, detected labels, and reviewed semantic correctness.

5. **Validate before changing the preferred dataset.** Require zero missing
   or unexpected keys for the reviewed scope, unique original locators, exact
   slices, full coverage, correct table/chunk links, and repeatable ingestion.
   Add database tests for legacy/new version coexistence, immutable replay,
   failure rollback, and selection of the new processing set. Retain the old
   processing selection for rollback. Only after these checks should consumers
   be switched to the new accepted outputs.

PR #20 already includes focused regression cases for page footers, issuer
titles, Part III introductions, linked exhibit indexes, and rejection of
unlinked or ambiguous aliases. The new work should concentrate on the versioned
upgrade and stored-output acceptance rather than rewriting those fixes.

**Local evidence and reproduction**

Generated evidence is in `data/section-investigation-20261109/`:

- `latest20-comparison.json`: accessions, hashes, all 13 source locators,
  old section assignments, source excerpts, offsets, and comparison results.
- `historical233-audit.json`: fresh independent audit of all 233 originals.
- `historical-legacy-comparison.json`: per-filing old/new key differences
  for the entire archived corpus.

From a checkout containing PR #20, the full audit can be reproduced with:

```sh
PYTHONPATH=src .venv/bin/python scripts/validate_report_sections.py \
  --manifest data/section-validation-20261029/corpus_manifest.json \
  --output data/section-investigation-20261109/reproduced-audit.json
```

The current local branch predates that script. The isolated audit in this
investigation did not modify its production modules.
