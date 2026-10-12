# Financial tables handoff — tables-v1

This module extracts the three complete primary statements in the supplied Microsoft 10-Q (`0001193125-26-191507`). It preserves the original physical grid and produces a readable projection. It does not extract all note tables or replace Bryce's financial-facts module.

## Run offline

From the repository root:

```bash
uv sync --locked
uv run --locked python -m sec_pipeline.tables_cli --output outputs/financial-tables
uv run --locked python -m unittest discover -s tests -v
```

The CLI verifies manifest sizes and hashes before parsing. It needs no credentials or SEC requests. JSON and TSV exports are evidence for review; their UUIDs 1/2/3 are explicitly placeholders and must never be imported. The integrated runner calls `extract_tables()` with actual metadata UUIDs.

## Inputs and outputs

`extract_tables(html_path, xml_path, *, filing_id, html_document_id, xml_document_id, facts_version='facts-v1', tables_version='tables-v1')` returns three `FinancialTableInput` records. The HTML and XML document UUIDs must differ. Use the metadata seed's IDs keyed by manifest `local_path`.

| Table key | Financial value cells | Numeric label cells | Fact references | Unique XML IDs / expected membership links |
| --- | ---: | ---: | ---: | ---: |
| income_statement | 76 | 0 | 76 | 76 |
| balance_sheet | 66 | 3 | 76 | 76 |
| cash_flow | 132 | 0 | 132 | 131 |
| Total | 274 | 3 | 284 | 283 |

The balance sheet's three numeric labels contain ten inline facts (allowances, accumulated depreciation, and shares). The cash-flow end balance in the first 2026 quarterly column is untagged. Cell `r059c003` resolves to the unique same-row instant fact for March 31, 2026, with identical concept/unit/dimensions and checked scale/value. This is an explicitly recorded `same_row_semantic_repeat` relation, not an amount-only search. Reviewers should inspect this case. It shares a fact with the nine-month column, so 132 data cells produce 131 unique table memberships.

## Structure contract

`structure` is serializable JSON. `rows` contains every original anchor cell, including blanks. `grid[r][c]` identifies its anchor, so row/column spans can be reconstructed. A missing grid slot is null. Cell locators are zero-based `rNNNcNNN` physical grid coordinates, stable for unchanged source/version. Source updates require a new version.

Each cell retains display text, source HTML, original XPath and optional element ID, spans, row heading, column headings and interpreted period, classification, unresolved reason, and fact references. Display signs and currency symbols may occupy adjacent physical cells and remain in the grid. `presentation_rows` groups those formatting cells with their value for easier reading; `readable_text` is the complete physical-grid TSV. `source_html` also preserves inline styles, borders, superscripts and original spacing; `is_total` is only a convenience heuristic, not an authoritative accounting classification.

Each fact reference retains original XML ID, expanded concept, context and unit IDs, expanded units/dimensions, period, raw XML value, nil flag, inline scale/sign/format, link method and source scope. Money shown in millions remains separate from exact XML dollar values. EPS uses scale 0. A displayed outflow can be negative while its XBRL payment concept is positive. Do not rewrite the fact to match displayed parentheses.

`source_xpath`, hashes, heading context, scale qualifiers and following text preserve table provenance. The first following sibling is retained, but comprehensive narrative footnote extraction belongs to report cleaning. Physical source HTML is authoritative for presentation details. Primary statement selection uses surrounding heading text plus financial row labels, never table indices. The current heading/date/number-format policy targets the supplied sample; other issuers require explicit fixtures and a new supported policy.

Classifications are `resolved`, `resolved_label`, `unresolved`, `header`, `label`, `blank`, and `formatting`. Non-fact categories preserve source content and are not silently treated as numerical facts. Unresolved financial cells block loading.

## Facts / runner integration

Bryce's merged `facts-v1` implementation uses the XML/inline ID as `occurrence_key` for every sample fact. Its database UUIDs must be resolved in the database receiving the tables, never copied from a different database. Keep `FactInput.raw_value`, namespace/concept, unit, period, dimensions, nil and exact `Decimal` value faithful to XML.

`sec_pipeline.tables_bridge.parse_tables(context)` is the pure table adapter for Jace's `DryRunStages`. `write_tables(conn, context)` is the corresponding `PipelineStages` adapter. It obtains the preceding facts stage's scoped UUID map through `facts_bridge.fact_ids_for_tables(context)`, calls `load_tables()`, and returns `WriteResult` IDs in parser-item order. Pass these callables as `tables=` to the plan builders in `facts_bridge`; the cleaner and independent validator must still be supplied. These table adapters alone are not a complete import plan.

For already-stored facts, Bryce also supplies `lookup_fact_ids(conn, filing_id=..., source_document_id=xml_document_id, extraction_version='facts-v1', occurrence_keys=...)`. Its returned `StoredFacts.fact_ids` can populate `FactScope`. Both paths retain original inline IDs in table JSON and resolve database IDs only for membership loading.

The new `test_tables_bridge.py` suite uses the actual 1,622 parsed sample facts to check all 283 expected memberships against their full fact records. Database storage is mocked in these tests; their success does not establish PostgreSQL import/replay/rollback or shared deployment. Missing stage output, wrong scope, missing IDs, mismatched stored records, unsupported versions, and escaping/duplicate manifest sources fail before table writes.

```python
from sec_pipeline.tables import extract_tables, FactScope, load_tables

# conn has autocommit=False; the RUNNER owns the outer transaction.
with conn.transaction():
    # Metadata and facts are loaded here first by the owning modules.
    tables = extract_tables(html_path, xml_path,
        filing_id=filing_id, html_document_id=html_document_id,
        xml_document_id=xml_document_id,
        facts_version='facts-v1', tables_version='tables-v1')
    scope = FactScope(filing_id, xml_document_id, 'facts-v1', xml_id_to_fact_id)
    table_ids = load_tables(conn, tables, scope)
    # Continue other stages and required validation; do not catch failures and commit.
```

`load_tables()` checks declared scope and actual stored facts before writes, including provenance identifying the XML occurrence. It compares exact values, concept, units, periods, dimensions and source/version. It uses existing `store_financial_table()` and `link_table_fact()` without schema changes or independent commits. Its transaction context is a stage savepoint inside the runner's active outer transaction. Identical table content/version uses the storage layer's replay behavior; changed content requires a new version. The runner must propagate all errors so a whole-import failure rolls back every stage.

The facts mapping is an integration handoff, not a reason to create substitute production facts. The shared reader accounts cannot import tables. Michael or an approved loader runs shared writes after review.

## Validation status

Offline tests cover complete sample inventory, nine independently transcribed reference values, exact scale/periods, cash-flow sign presentation, numeric labels, semantic repeat, merged cells/blanks/dashes, original locators, deterministic output, selection with extra layout tables, ambiguous selection, missing IDs, wrong concept/context/unit, repeated amounts, and scope rejection before writes. Existing regression tests are also run.

Live PostgreSQL tests are opt-in and are not established by offline success. The merged facts implementation now provides the scoped map contract, and sample compatibility is checked offline. Final acceptance still needs an actual database import/replay/rollback test, Sally's independent reference review (including the untagged cash balance), and Michael's reviewed shared import. This handoff does not claim those team-wide steps are complete.
