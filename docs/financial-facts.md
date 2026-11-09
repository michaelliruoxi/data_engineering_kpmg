# Financial facts

`sec_pipeline.fact_extraction` turns every fact in a filing's XBRL XML instance into a `FactInput` record and loads those records into `sec.financial_facts`. Extraction reads local files only: it makes no network requests, downloads no taxonomy, and needs no database.

| Function | Purpose |
| --- | --- |
| `extract_facts(xml_path, *, filing_id, source_document_id, extraction_version="facts-v1")` | Parse the XML and return a `FactExtraction` with `facts`, `failures`, and `inventory`. |
| `FactExtraction.require_complete()` | Raise `FactExtractionError` unless every occurrence was extracted. |
| `store_facts(conn, facts)` | Store through `store_fact()` in the caller's transaction and return a `StoredFacts` mapping. It never commits. |
| `check_inline_correspondence(html_path, facts)` | Compare the facts with the inline XBRL tags in the filing HTML. |
| `lookup_fact_ids(conn, *, filing_id, source_document_id, extraction_version="facts-v1", occurrence_keys=None)` | Read the UUIDs of facts already stored in that database, by occurrence key (= inline HTML id). Read-only. |
| `python -m sec_pipeline.fact_coverage` | Coverage summary across many archived filings, such as the ten-company shortlist. See [Ten-company coverage](#ten-company-coverage-20212026). |

## Commands

Run from the repository root. The first command is a dry run and opens no database connection.

```powershell
uv run --locked python -m sec_pipeline.fact_extraction --check-html
uv run --locked python -m sec_pipeline.fact_extraction --output data/processed/facts-v1.jsonl
uv run --locked python -m sec_pipeline.fact_extraction --load --env-file .env
uv run --locked python -m sec_pipeline.fact_extraction --load --check-html --env-file .env
uv run --locked python -m sec_pipeline.fact_extraction --env-file .env --export-ids data/processed/facts-v1-ids.csv
uv run --locked python -m unittest tests.test_fact_extraction -v
```

- `--export-ids` reads the database selected by `--env-file` and writes one CSV row per fact: `occurrence_key`, `fact_id`, concept, label, value, nil flag, `unitRef`, `contextRef`, period, dimension count, and the filing, source, and version scope. It only reads, so a `sec_reader` login is enough, and it refuses to overwrite an existing file. Combined with `--load`, it runs after the load.

- The dry run prints the inventory, any failures, and, with `--check-html`, the HTML comparison. It exits with status 1 if any occurrence failed, any fact disagreed with its inline tag, or any inline tag has no fact.
- `--output` writes one JSON record per line to a new file and refuses to overwrite an existing one. `data/processed/` is ignored by Git. These records carry placeholder filing and source IDs.
- `--load` is for a local database. It verifies and registers the manifest with `seed_manifest()`, then stores all facts in one transaction, so a failure leaves nothing behind. The shared database accepts it only from a login with write permission; the integrated import belongs to the task 2 runner.
- With `--load --check-html`, extraction failures, HTML/XML mismatches, unmatched inline tags, and confirmed numeric-tag boundary problems stop the command before it opens a database connection or writes output files. The diagnostic JSON is still printed and the exit status is 1. A passing check proceeds to the normal transactional load.
- The live database test in `tests/test_fact_extraction.py` is skipped unless `SEC_DB_TEST=1` is set. Prepare the dedicated test database as described in [the database guide](database.md#handoff-and-acceptance).

## Inventory of the supplied sample

For the unchanged Microsoft 10-Q XML (`msft-20260331_htm.xml`, SHA-256 `d9c4e6b9…47cef1`), `extract_facts()` reports:

| Item | Count |
| --- | ---: |
| Fact occurrences found | 1,622 |
| Extracted / failed | 1,622 / 0 |
| Contexts / units | 409 / 6 |
| With `unitRef` / without | 1,428 / 194 |
| Numeric values / nil / text | 1,426 / 2 / 194 |
| Numeric values equal to zero | 87 |
| Duration / instant / forever periods | 827 / 795 / 0 |
| Facts with dimensions | 895 (1,342 explicit and 4 typed dimension entries) |
| Distinct concepts | 372 |
| Facts with a label / without | 1,610 / 12 |

Both nil facts are `us-gaap:CommitmentsAndContingencies`, at March 31, 2026 and June 30, 2025. Units in use are USD (1,307 facts), pure (55), shares (35), USD per share (29), EUR (1), and `msft:Segment` (1).

## Field mapping

| `FactInput` field | Source in the XML |
| --- | --- |
| `occurrence_key` | The fact element's `id`. See [occurrence keys](#occurrence-keys). |
| `concept_namespace`, `concept_name` | The element's namespace URI and local name, such as `http://fasb.org/us-gaap/2025` and `Assets`. Prefixes are resolved, never stored as the identity. |
| `raw_value` | The element's text exactly as written, including whitespace. For text blocks this is the complete unescaped HTML string. `NULL` for a nil fact; an empty element gives `''`. |
| `numeric_value` | `Decimal(raw_value)` when the fact has a `unitRef` and is not nil. Otherwise `NULL`. The value never passes through a float. |
| `is_nil` | `xsi:nil="true"` or `"1"`. A nil fact keeps its unit and has no numeric or raw value. |
| `reported_decimals`, `reported_precision` | The `decimals` and `precision` attributes, unchanged. |
| `unit` | `{"numerator": [...], "denominator": [...]}` with every measure expanded to `{namespace}local`. A `<divide>` fills the denominator. `NULL` for facts without `unitRef`. |
| `period_kind` and dates | From the context: `instant` sets `instant_date`; `startDate` and `endDate` set `period_start` and `period_end`; `forever` sets none. |
| `dimensions` | From the context's `segment` and `scenario`. Explicit: `{"kind", "dimension", "member"}`. Typed: `{"kind", "dimension", "value"}`, where `value` is the member's XML with the namespace declarations it uses. Sorted by dimension name. |
| `label` | The concept's label from the filer's extension schema, when available. See [labels](#labels). |
| `source_reference` | Where the value came from. See below. |

A fact is numeric only when it has a `unitRef`. XBRL requires that attribute on numeric items and forbids it on others, so `DebtInstrumentIssuanceYear` (`2009`) and `EntityCentralIndexKey` (`0000789019`) stay text.

`source_reference` holds:

| Key | Meaning |
| --- | --- |
| `element_id` | The fact element's `id`; absent if the element has none. |
| `xpath` | Positional path in the XML, such as `/*/*[485]`. |
| `document_order` | One-based position among the fact occurrences. |
| `qname` | The element name as written, such as `us-gaap:Assets`. |
| `context_ref`, `unit_ref` | The `contextRef` and, when present, `unitRef` attribute values. |
| `entity` | The context's identifier `scheme` and `identifier`. |
| `dimension_containers` | `segment` or `scenario` for each entry in `dimensions`, in the same order. |
| `value_source` | `element_text`, or `xsi:nil` for a nil fact. |
| `lang` | The element's `xml:lang`, when present. |
| `label_source` | The schema file and label role the `label` came from, when a label was found. |

## Occurrence keys

The occurrence key is the fact element's `id` attribute, verbatim, for example `F_3d73e726-8508-4605-87a7-ae4fbcdbc622`. All 1,622 sample facts have a unique `id`.

- A fact with no `id` gets `xpath:` followed by its positional path, for example `xpath:/*/*[13]`. An XML `id` cannot contain a colon, so the two forms never collide.
- An `id` used by more than one fact is reported as a failure for each of them.
- Facts that repeat a concept, context, and value stay separate records. Quarterly revenue of 82,886,000,000 USD appears four times in the sample, under four keys.

Fact UUIDs are assigned by the database. `StoredFacts.fact_ids` maps occurrence key to UUID and is valid only for the `filing_id`, `source_document_id`, and `extraction_version` it carries.

## Labels

The standard taxonomies are not downloaded. Labels come from the label linkbase embedded in the extension schema that the instance names in `link:schemaRef`, and only when that reference is a plain file name next to the XML (`msft-20260331.xsd` in the sample). A URL or a missing file leaves every label `NULL`.

- The standard label is preferred, then the terse, total, and verbose labels. The role used is recorded in `source_reference.label_source`. In the sample, 1,590 facts use the standard label and the 20 `NetIncomeLoss` facts use the total label, "Net income".
- Labels for standard-taxonomy concepts are matched through the taxonomy's `prefix_LocalName` element-id convention, because their schemas are not read.
- The 12 unlabeled sample facts are `ecd` concepts for which the filer's schema supplies no label.

Labels are part of the stored content. Keep the schema file beside the XML, as the manifest already does, so that a repeat import produces identical records.

## Failures

Every occurrence ends in `facts` or in `failures`; none is skipped silently. A failure records the positional path, concept, `id`, a category, and a reason.

| Category | Examples |
| --- | --- |
| `malformed_source` | `contextRef` or `unitRef` with no matching definition, a repeated fact `id`, an invalid `decimals` or `xsi:nil` value, a nil fact with content, a period that starts after it ends |
| `unsupported_extraction` | A numeric fact whose text is not a finite decimal (`N/A`, `1,000`, `INF`, empty), fraction content, a tuple (a top-level element without `contextRef`) and every item inside it, a `dateTime` period, non-dimensional `segment` content |

A file that is not well-formed, declares a `DOCTYPE`, or is not an XBRL instance raises `FactExtractionError` before any fact is read.

## Handoff

**Task 2, runner.** Resolve the XML document UUID from `seed_manifest()`'s `document_ids` by the XML file's manifest `local_path`, then:

```python
extraction = extract_facts(xml_path, filing_id=filing_id, source_document_id=xml_document_id,
                           extraction_version="facts-v1").require_complete()
stored = store_facts(conn, extraction.facts)   # inside the runner's transaction
```

For a dry run, pass placeholder UUIDs and skip `store_facts()`. `extraction.inventory` supplies the source hash and the expected and extracted counts for the run report. Loading the same content again returns the same UUIDs; changed content under the same version raises `DataConflict`.

In the runner's adapter API (`jace/ingestion-workflow`), the facts stage maps directly:

```python
from sec_pipeline.fact_extraction import XML_ROLE, extract_facts, store_facts

def _xml_path(context):
    [record] = [r for r in context.manifest["files"] if r["role"] == XML_ROLE]
    return context.data_root / record["local_path"]

def _extract(context):
    return extract_facts(_xml_path(context), filing_id=context.source_ids.filing_id,
                         source_document_id=context.source_ids.xml_document_id,
                         extraction_version=context.versions["facts"])

def parse_facts(context):              # PureStage: DryRunContext -> ParseResult
    extraction = _extract(context)
    return ParseResult(extraction.facts, rejected=extraction.failures)

def write_facts(conn, context):        # Stage: (conn, PipelineContext) -> WriteResult
    extraction = _extract(context).require_complete()
    stored = store_facts(conn, extraction.facts)
    return WriteResult(ParseResult(extraction.facts),
                       [stored.fact_ids[f.occurrence_key] for f in extraction.facts])
```

`stored_ids` is in the same order as `items`, as the runner requires. Field names are those on `jace/ingestion-workflow` as of October 12, 2026.

**Task 4, tables.** Every inline tag (`ix:nonFraction` or `ix:nonNumeric`) in the HTML carries an `id` equal to one fact's `occurrence_key`, so a table cell links to a fact through `stored.fact_ids[inline_id]`. `check_inline_correspondence()` verified this for the sample: 1,622 of 1,622 facts match one inline tag on `id`, concept, `contextRef`, `unitRef`, and nil flag, and no inline tag lacks a fact. For all 1,426 numeric values, the displayed number multiplied by ten to the power of `scale`, and negated when `sign="-"`, equals `numeric_value`. A cell showing `82,886` with `scale="6"` is the fact `82886000000`.

How the table side gets fact UUIDs:

- **Keep the inline id in the cell, not the UUID.** UUIDs are created by whichever database stores the facts, so a UUID from one database (yours, mine, or the shared one) means nothing in another. The inline id is the stable key; resolve it to a UUID only at load time, in the database the links go into.
- **Inside the runner's transaction**, use the facts stage output: `stored.fact_ids[inline_id]`, or the runner's equivalent built from `items` and `stored_ids`.
- **When the facts are already in that database** (your local test database after `--load`, or the shared database after Michael's import), call `lookup_fact_ids(conn, filing_id=..., source_document_id=xml_document_id, occurrence_keys=cell_inline_ids)`. The result is scoped to the filing, XML source, and version, so a link cannot reach another filing's or version's fact. A key missing from `fact_ids` means no stored fact: mark the cell unresolved.
- **To inspect the mapping by hand**, run `--export-ids` against that same database and open the CSV.
- **Never link on amount.** Quarterly revenue of 82,886,000,000 USD is four separate facts with four UUIDs (`F_3d73e726…`, `F_5639c226…`, `F_898c1aec…`, `F_9df6c506…`); only the inline id says which one a cell shows.

**Task 7, validation.** Read results for this version through the provenance view:

```sql
SELECT occurrence_key, concept_name, label, numeric_value, raw_value, is_nil, unit,
       period_kind, period_start, period_end, instant_date, dimensions,
       source_reference, source_url, sha256
FROM sec.fact_provenance
WHERE accession_number = '0001193125-26-191507'
  AND extraction_version = 'facts-v1'
ORDER BY (source_reference->>'document_order')::int;
```

## Ten-company coverage (2021–2026)

The November 1 survey covered **233 filings** (59 original 10-Ks and 174 original 10-Qs) from AAPL, CASY, MCK, MDT, MSFT, MU, NKE, NTAP, PH and TTWO. It used the October 29 section-validation inventory. The filing-date window in `config/shortlist.json` extends through December 31, 2026, so a later discovery can include more filings. [Section validation](report-section-validation.md)

### Run it

On a machine with the acquisition archive ([shortlist acquisition](shortlist-acquisition.md)):

```bash
uv run --locked python -m sec_pipeline.fact_coverage \
  --config config/shortlist.json --archive-root outputs/shortlist-20261028 \
  --discovery outputs/shortlist-20261028/discovery.json --output outputs/fact-coverage-20261101
```

- It verifies each archive's hashes, then runs the same `extract_facts()` and `check_inline_correspondence()` as the import adapters. No database, no network.
- Output: `coverage.md` (summary for the team), `coverage.csv` (one row per filing), `coverage.json` (everything). The output directory must be new or empty.
- Per filing: occurrences extracted/failed, period kinds and the report period, units used, numeric/nil/text counts, dimensions, labels, DEI identity, inline correspondence, and eight primary-statement anchors (revenue, net income, diluted EPS, operating cash flow, assets, liabilities and equity, equity, cash) at the report period end.
- A filing receives `review` when its DEI form or period disagrees with filing metadata or the DocumentType context, an anchor lacks a fact ending on the declared report date, or a number has irregular digit grouping or a detected tag-boundary problem. The boundary diagnostic records the complete adjacent visible number separately from the unchanged tagged/XML value. An extraction or correspondence failure yields `failed`. Source verification errors yield `archive_error`. Discovery entries without archives yield `missing_archive`.
- Exit status is 0 when at least one filing was assessed and all passed, 2 when the report needs attention, and 1 for a setup error. Supply a complete discovery for the same configuration to detect missing filings. Add `--ticker MSFT` to select one company from that scope.

On a database migrated through 010, a reader login can check the loaded facts by filing, XML source document and extraction version. The query counts stored facts and anchors at the DEI report date. Each processing-set receipt applies to the source and version on that row. Filings with no facts appear with zero counts. [Reader query](fact-coverage-shortlist.sql)

### Survey of the original SEC files, November 1, 2026

The survey read each filing's index, primary HTML, extracted XML instance and extension schema from the SEC. It used a browser-based JavaScript port of the validation rules at commit `724623f`, before the subsequent registry and coverage corrections. The bundled Microsoft filing matched on XML digest and sample counts (1,622 occurrences, 409 contexts, 6 units, 1,426 numeric, 2 nil, 194 text, 827 duration, 795 instant, 895 dimensional and 1,426 numeric values equal).

The survey classified **232 filings as passed and one NetApp filing for review**. Its artifacts record XML digests and label-source presence. The Python archive command also records HTML digests, manifest report dates and counts of labelled facts. Run the command above for the archive's current results. [Survey summary](validation/fact-coverage-2021-2026.md) · [Survey CSV](validation/fact-coverage-2021-2026.csv) · [Survey JSON](validation/fact-coverage-2021-2026.json)

| Check | Result |
| --- | --- |
| Filings reached | 233 of 233 (transient SEC HTTP 503 responses were retried) |
| Fact occurrences | **290,632**, all extractable; 0 failures, 0 tuples |
| Periods | 169,884 duration, 120,748 instant, 0 forever; no `dateTime` or reversed periods |
| Values | 259,598 numeric (20,604 equal to zero), 445 nil, 30,589 text; 156,644 dimensional (796 typed) |
| Units | every `unitRef` resolves to a unit with a numerator measure |
| Fact ids | every fact has an `id`; every fact matches exactly one inline tag on concept, context, unit and nil; no inline tag lacks a fact |
| DEI | `dei:DocumentPeriodEndDate` equals the end of the `DocumentType` context in all 233 |
| Statement anchors | all eight anchors present at the report period end in all 233 |
| Labels | embedded in the `.xsd` only for MSFT (from January 2024) and NTAP (from February 2024): 23 filings; the other 210 keep them in a separate `_lab.xml` |
| Footnotes | 504 XBRL footnotes, counted but not attached (as in the sample) |

The survey compared four numeric rule sets. The v1/v3/v3-text adapters require every numeric value to be compared.

| Rule | Equal | Not compared | Filings with any not compared |
| --- | ---: | ---: | ---: |
| `facts-v1` before this change | 232,767 | 26,831 | **41** (would fail import) |
| `inline_value_transforms` (tables-v4 path) before this change | 256,930 | 2,668 | 32 |
| `facts-v1` at survey revision `724623f` | 259,598 | 0 | 0 |
| `inline_value_transforms` at survey revision `724623f` | 259,597 | 1 | 1 (NTAP "206,376,66", below) |

No value compared unequal in the survey. The 2,668 values the earlier tables-v4 rules could not compare included 2,661 em dashes, six NetApp number-word values and one irregularly grouped share count. Registry 2/3 formats appeared through mid-2021 for eight companies and through mid-2023 for MSFT and NTAP.

### What changed for older filings

- **Older numeric formats.** Apple's 10-Q 0000320193-21-000010 contains 448 `ixt:numdotdecimal` values and 46 `ixt:zerodash` values. Both validation paths recognize these names in the Registry 2/3 namespaces. An integer can be ungrouped or use commas, spaces or nonbreaking spaces between three-digit groups. A decimal point requires digits on both sides. Registry 2/3 `zerodash` accepts one of the listed dash characters, including the em dash. Registry 1 `numdash` accepts only an ASCII hyphen-minus.
- **"N hundred thousand".** NetApp's 10-K 0001564590-21-033645 and five later filings tag office space as "three hundred thousand" (`ixt-sec:numwordsen`); the SEC value is 300000. Number words now allow a 1–999 coefficient before one magnitude. Phrases with two magnitudes or trailing words remain unsupported.
- **Numeric-tag boundaries are reviewed.** NetApp's 10-Q 0000950170-24-023115 visibly reports **206,376,661** shares. Its inline tag encloses only `206,376,66`; the final `1` is outside that tag in the next span, so the extracted XML contains **20,637,666**. Registry 4/5 permits that grouping syntax, and both transformation paths now agree on the unchanged raw value. `numeric_tag_boundary_issues` separately identifies the missing tagged digit with original locators. Coverage remains `review`, and source-validated ingestion rejects this discrepancy. See the [original-source investigation and reproduction commands](ntap-source-review.md).
- Stored fact content is unchanged; these are validation changes only.

### Known gaps for this scope

- **Labels.** facts-v1 reads labels only from the extension schema in the four-file archive. Eight of the ten companies never embed labels there: every AAPL, CASY, MCK, MDT, MU, NKE, PH and TTWO filing, plus MSFT and NTAP before 2024, keeps them in a separate `_lab.xml` (210 filings), so their `label` is `NULL`. Concept names, values, periods and units are unaffected. Archiving `_lab.xml` would change stored content and needs a new facts version.
- **Share-count tagging defect** above (NTAP 10-Q filed 2024-02-29). The separate NTAP opening-cash date discrepancy in the September 2, 2026 filing is documented in the [same investigation](ntap-source-review.md); its original review status is preserved.

## Limits of `facts-v1`

- XBRL footnotes are not attached to facts. The sample has 3 footnotes linked to facts by 14 arcs; the inventory counts them under `not_extracted`. The ten-company scope has 504.
- Text values are not compared with the HTML, because inline text is transformed and split across continuation tags.
- Periods must be plain dates. Fraction items and tuples are reported as failures instead of being stored.
- Labels come only from the extension schema; see [Known gaps](#known-gaps-for-this-scope).
- Python coverage has been checked on the bundled Microsoft filing. The November 1 survey provides separate evidence for the 233 filings. Acceptance of a filing depends on its own verified archive and review findings.
