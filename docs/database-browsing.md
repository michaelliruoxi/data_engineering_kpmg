# Finding and using filing content

For standardized financial columns and the simpler company/filing/statement map,
start with the [financial browsing schema](database-finance.md). This guide
describes the underlying `sec` evidence and explicit processing selections.

Use `filing_overview` to choose a filing and inspect its selected processing set, if any. Use `section_catalog` for prose, `statement_cells` for financial statement presentation, and `fact_catalog` for exact source occurrences. Install migrations 001–005; the preferred catalogs need an explicitly chosen set and do not choose the latest version automatically. The overview also shows filings without a preferred set.

## Where each part of a report lives

All extracted narrative text lives in `reports.full_text`. `report_sections` stores its original heading, form-specific part/item, canonical type, source locator, and character offsets. Section text is a slice of that exact report version, not a second independent copy. `chunks` holds smaller exact slices for retrieval and citations. Unrecognized headings can remain in an unknown block or within the preceding section. An empty search result proves neither absence nor completeness.

The form locations below follow the SEC's [Form 10-K](https://www.sec.gov/files/form10-k.pdf) and [Form 10-Q](https://www.sec.gov/files/form10-q.pdf). The canonical codes are this project's vocabulary.

| Content | 10-K location | 10-Q location | Canonical section type |
| --- | --- | --- | --- |
| Cover and introductory content | Before Part I | Before Part I | `front_matter` or `unknown` |
| Business | I / 1 | No dedicated item | `business` |
| Risk factors | I / 1A | II / 1A | `risk_factors` |
| Cybersecurity | I / 1C | No dedicated item | `cybersecurity` |
| Properties | I / 2 | No dedicated item | `properties` |
| Legal proceedings | I / 3 | II / 1 | `legal_proceedings` |
| Mine safety | I / 4 | II / 4 | `mine_safety` |
| Equity market / sales | II / 5 | II / 2 | `market_equity` / `equity_sales` |
| MD&A | II / 7 | I / 2 | `mda` |
| Market risk | II / 7A | I / 3 | `market_risk` |
| Statements and notes | II / 8 | I / 1 | `financial_statements` |
| Auditor disagreements | II / 9 | No dedicated item | `accounting_changes` |
| Controls | II / 9A | I / 4 | `controls` |
| Other information | II / 9B | II / 5 | `other_information` |
| Directors and governance | III / 10 | No dedicated item | `directors_governance` |
| Compensation | III / 11 | No dedicated item | `executive_compensation` |
| Ownership | III / 12 | No dedicated item | `ownership` |
| Related parties | III / 13 | No dedicated item | `related_party_transactions` |
| Audit fees | III / 14 | No dedicated item | `audit_fees` |
| Exhibits/schedules | IV / 15 | II / 6 | `exhibits` |
| Optional summary | IV / 16 | No dedicated item | `summary` |
| Defaults | No dedicated item | II / 3 | `defaults` |
| Remaining items, signatures, unfamiliar headings | Includes I / 1B, II / 6 and 9C | As present | Retained text; currently `unknown` or within the preceding section |

Pass `form=manifest['form']` to `clean_report()` for deterministic 10-Q/10-K interpretation; amendments use the base form's vocabulary. The detector recognizes supported item headings and avoids table-of-contents matches. It does not guarantee boundaries for every issuer's layout. `unverified` means no classification check; `detected` means a supported heading was found; `validated` is reserved for an explicit validation supplied by a caller. Neither `detected` nor a passed processing set asserts human review.

Governance disclosures may be incorporated by reference from a proxy statement. The cleaner preserves reference text and marks detected proxy references in `source_locator`; it does not fetch or resolve the proxy. Until the referenced document is independently acquired and processed, the database contains the reference, not the referenced disclosure. Similarly, an exhibits section is a list/reference unless the actual attachment has been acquired. [SEC Form 10-K, General Instruction G](https://www.sec.gov/files/form10-k.pdf)

## How the financial representations fit together

`financial_facts` stores XBRL **occurrences**, preserving numeric `Decimal` values, nils, raw text, units, periods, dimensions, source references, and extraction versions. Two occurrences can describe the same measure. Do not sum all rows or equate a three-month duration with a nine-month duration.

`financial_tables.structure` retains the complete parser layout/evidence. New schema-v1 table loads also write one `table_cells` row per physical anchor cell, including labels, headers, blanks, spans and displayed text. `cell_fact_links` records accepted references to verified facts, including reference order and resolution evidence. One cell can cite multiple facts. `statement_cells` still returns one row per cell, with an ordered `linked_facts` JSON array; numeric values in that array are strings to preserve exact decimal precision. Use `fact_catalog.numeric_value` for SQL arithmetic with deliberate concept/period/unit/dimension selection.

The existing `financial_table_facts` membership table remains supported. For tables marked `relational_cells_version=1`, deferred database checks keep cells, accepted links, retained JSON, and table memberships consistent at transaction end. Legacy tables retain `NULL` here and may have no relational cells. The table loader continues rejecting unresolved financial cells before a successful load; unresolved parser evidence is not promoted to an accepted fact link.

## Explicit processing sets

A processing set selects independent fact and table source/version pairs, one report UUID, and optionally a chunking version. Facts commonly come from XML while prose and tables come from HTML. Version names need not match. Composite foreign keys, table-scope checks, and section containment prevent mismatched filing/report references.

Coverage has exactly four keys: `facts`, `tables`, `reports`, `chunks`. Each is `absent`, `unknown`, `partial`, or `complete`. `absent` means that stage is not selected; a selected scope must have rows. Completeness is caller-declared, not inferred from row counts. Chunk completeness describes the selected report, which may itself be partial.

Validation is separate: `unvalidated`, `passed`, or `failed`. A passed set must contain named boolean-true checks in `validation.checks`; only passed sets may be preferred. A validated excerpt may legitimately have partial coverage. The database validates references, counts, and consistency; it cannot independently verify a caller's completeness claim or the meaning of its checks.

Creating **any** set makes its selected artifact scopes immutable, including unvalidated or partial sets. Complete the selected scope before recording it. Identical replays retain IDs; corrections require new extraction/chunking versions and a new set key. Preferred selection is explicit and can be switched back to an earlier passed set. There is no automatic promotion or deletion of old evidence.

Artifact writes and selection require PostgreSQL's default **READ COMMITTED** isolation (READ UNCOMMITTED has the same behavior in PostgreSQL). Per-filing locks serialize publication with writes so a waiting writer cannot append to a newly sealed scope. Read-only queries may use other isolation levels. Helpers require caller-managed transactions and never commit; `run_ingestion()` commits the validated batch through its existing transaction controller.

For existing stored output, create a JSON specification using actual UUIDs and version strings from that filing. Example for a report excerpt only:

```json
{
  "filing_id": "REPLACE_WITH_FILING_UUID",
  "set_key": "reviewed-report-excerpt-v1",
  "report_id": "REPLACE_WITH_REPORT_UUID",
  "coverage": {"facts": "absent", "tables": "absent", "reports": "partial", "chunks": "absent"},
  "validation_status": "passed",
  "validation": {"checks": {"excerpt_matches_source": true}},
  "configuration": {"scope": "Reviewed excerpt only"}
}
```

Record only checks actually performed. Save the completed specification as `processing-set.json`, then run against the intended local configuration:

```sh
uv run --locked sec-db --env-file .env.local processing-set --spec processing-set.json --prefer
```

To include facts/tables, add `fact_source_document_id`, `fact_extraction_version`, `table_source_document_id`, `table_extraction_version`, and their coverage. Add `chunking_version` for the chosen report's chunks. Selected tables' fact references must match the selected fact source/version.

The existing adapter-based ingestion CLI also accepts `--processing-set-key NAME --coverage-file coverage.json --prefer-processing-set` with `--ingest`. The coverage file contains all four keys, each `unknown`, `partial`, or `complete`; this full-pipeline path selects all four nonempty stages. It records the adapter's successful checks and source hashes, and creates/selects the set in the same transaction as output ingestion. Omit `--prefer-processing-set` to record a set without changing browsing. Use `sec-db prefer-processing-set --filing-id UUID --set-id UUID` to select an existing passed set.

To select sets for many filings at once, run `sec-db prefer-validated --set-key KEY [--set-key KEY ...]`. For every filing without a preferred set, it selects the newest passed set of the first listed key that the filing has, one filing per transaction; it never replaces an existing choice, and `--dry-run` only counts. The backfill's `filing-import-v3-text` sets select facts, the report, and chunks with `tables` coverage `absent` (the reason is in `configuration->'tables_unavailable'`); list them after `filing-import-v3` so complete imports are preferred. Switching a filing that already has a set, for example to a later complete import, remains an explicit `prefer-processing-set` call.

## Migration and compatibility

Apply `sec-db migrate` to the intended database after taking the normal deployment backup. Migration 001 is unchanged; 002 adds sections, 003 adds selections/catalogs, 004 adds cells, and 005 adds source identity/profile fields. The migrator applies the pending batch transactionally and checks historical file hashes.

- Existing reports/chunks keep their IDs and text. Valid legacy offset descriptors become unknown, unverified sections; malformed/overlapping descriptors are left in original JSON rather than guessed. Existing chunks link only when a known key and exact containment agree.
- No preferred set is created automatically. The original `filing_catalog`, `fact_provenance`, and `chunk_citations` views continue showing all versions. A NULL preferred set does not mean a filing has no stored outputs.
- Legacy tables remain readable in `statement_catalog` when selected. Relational cells are populated by the schema-v1 table loader on load/replay before sealing; migrations do not invent cells from arbitrary legacy JSON.
- Document identity becomes `(filing_id, source_url)`. Re-downloading identical bytes at another local path retains the first receipt/path and UUID. Conflicting bytes/roles at the same URL fail. Duplicate historical filing/URL pairs stop migration 005 for explicit reconciliation. The old local-path uniqueness constraint remains as a compatibility safeguard.
- `companies.cik` is identity; `register_company()` resolves existing companies without overwriting their profile. `update_company_profile()` explicitly changes name/ticker. `filings.company_name_as_filed` captures new manifest names; historical rows remain NULL until supplied, rather than copying today's name into history.
- `source_documents.title`, `document_type`, and `storage_uri` are optional display/storage fields. Updating a locator does not change source provenance; selected source URL/hash/size/role stay protected.

Historical deployment snapshot: the shared RDS database was upgraded through migration 005 on **October 13, 2026**, and `sec_reader` has SELECT access to all 25 tables/views, including the new catalogs. The existing manual example was preserved; its two legacy sections are `unknown`/`unverified`, and no processing set or relational statement cells were created. At that snapshot, preferred catalogs were empty until a passed set was explicitly selected. These historical counts do not describe later imports or the finance schema. See the [verified deployment record](aws-deployment.md#schema-upgrade-october-13-2026). Other database instances still require their own backup, migration, and reader-permission checks.

## Design boundary

The schema favors clear relational paths for sections, cells and chosen versions, while retaining JSON for variable parser evidence. It does not create a separate table for every SEC item or collapse raw facts into one company metric. A curated comparable-metrics layer, resolved cross-document references, nested note subsections, full-text/vector indexes, and processing failure history can be added when their consumers and quality rules are defined. Current topic lookup uses indexed section types; no embedding model or extra database service is required.
