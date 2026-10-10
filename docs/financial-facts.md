# Financial facts

`sec_pipeline.fact_extraction` turns every fact in a filing's XBRL XML instance into a `FactInput` record and loads those records into `sec.financial_facts`. Extraction reads local files only: it makes no network requests, downloads no taxonomy, and needs no database.

| Function | Purpose |
| --- | --- |
| `extract_facts(xml_path, *, filing_id, source_document_id, extraction_version="facts-v1")` | Parse the XML and return a `FactExtraction` with `facts`, `failures`, and `inventory`. |
| `FactExtraction.require_complete()` | Raise `FactExtractionError` unless every occurrence was extracted. |
| `store_facts(conn, facts)` | Store through `store_fact()` in the caller's transaction and return a `StoredFacts` mapping. It never commits. |
| `check_inline_correspondence(html_path, facts)` | Compare the facts with the inline XBRL tags in the filing HTML. |

## Commands

Run from the repository root. The first command is a dry run and opens no database connection.

```powershell
uv run --locked python -m sec_pipeline.fact_extraction --check-html
uv run --locked python -m sec_pipeline.fact_extraction --output data/processed/facts-v1.jsonl
uv run --locked python -m sec_pipeline.fact_extraction --load --env-file .env
uv run --locked python -m unittest tests.test_fact_extraction -v
```

- The dry run prints the inventory, any failures, and, with `--check-html`, the HTML comparison. It exits with status 1 if any occurrence failed or any comparison disagreed.
- `--output` writes one JSON record per line to a new file and refuses to overwrite an existing one. `data/processed/` is ignored by Git. These records carry placeholder filing and source IDs.
- `--load` is for a local database. It verifies and registers the manifest with `seed_manifest()`, then stores all facts in one transaction, so a failure leaves nothing behind. The shared database accepts it only from a login with write permission; the integrated import belongs to the task 2 runner.
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
| `unsupported_extraction` | A numeric fact whose text is not a finite decimal (`N/A`, `1,000`, `INF`, empty), fraction or tuple content, a `dateTime` period, non-dimensional `segment` content |

A file that is not well-formed, declares a `DOCTYPE`, or is not an XBRL instance raises `FactExtractionError` before any fact is read.

## Handoff

**Task 2, runner.** Resolve the XML document UUID from `seed_manifest()`'s `document_ids` by the XML file's manifest `local_path`, then:

```python
extraction = extract_facts(xml_path, filing_id=filing_id, source_document_id=xml_document_id,
                           extraction_version="facts-v1").require_complete()
stored = store_facts(conn, extraction.facts)   # inside the runner's transaction
```

For a dry run, pass placeholder UUIDs and skip `store_facts()`. `extraction.inventory` supplies the source hash and the expected and extracted counts for the run report. Loading the same content again returns the same UUIDs; changed content under the same version raises `DataConflict`.

**Task 4, tables.** Every inline tag (`ix:nonFraction` or `ix:nonNumeric`) in the HTML carries an `id` equal to one fact's `occurrence_key`, so a table cell links to a fact through `stored.fact_ids[inline_id]`. `check_inline_correspondence()` verified this for the sample: 1,622 of 1,622 facts match one inline tag on `id`, concept, `contextRef`, `unitRef`, and nil flag, and no inline tag lacks a fact. For all 1,426 numeric values, the displayed number multiplied by ten to the power of `scale`, and negated when `sign="-"`, equals `numeric_value`. A cell showing `82,886` with `scale="6"` is the fact `82886000000`.

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

## Limits of `facts-v1`

- XBRL footnotes are not attached to facts. The sample has 3 footnotes linked to facts by 14 arcs; the inventory counts them under `not_extracted`.
- Text values are not compared with the HTML, because inline text is transformed and split across continuation tags.
- Periods must be plain dates. Fraction items and tuples are reported as failures instead of being stored.
- Only the supplied Microsoft filing has been processed. Another filing needs its own inventory and checks.
