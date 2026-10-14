"""Load a filing's facts, report, sections and chunks when its v3 tables fail.

The fallback keeps the tables-v3 profile's fact, report and chunk versions and
every check that applies to them. Statement tables are recorded as absent in a
separate processing set rather than guessed, and the reason the full import
failed is stored with the set, so a repaired table parser can add the complete
filing-import-v3 set later without conflicting with this one.
"""

from dataclasses import replace

from lxml import html

from . import filing_adapter as base
from . import filing_adapter_v2 as segmented
from . import filing_adapter_v3 as full
from .database import FactInput, ReportInput
from .fact_extraction import check_inline_correspondence
from .processing import ParseResult, ParsedReport, ProcessingError, ValidationResult, WriteResult
from .report_cleaning import clean_report


SET_KEY = "filing-import-v3-text"
VERSIONS = full.VERSIONS
COVERAGE = {"facts": "complete", "tables": "absent", "reports": "unknown", "chunks": "complete"}
TABLE_CHECKS = frozenset({
    "no_unresolved_cells", "table_fact_references", "table_source_cells", "statement_segment_coverage",
    "source_only_markers_verified", "hidden_nil_bindings_verified", "financial_cell_inventory_complete",
})
REQUIRED_CHECKS = tuple(check for check in full.REQUIRED_CHECKS if check not in TABLE_CHECKS)


def parse_tables(context):
    full._require_versions(context)
    return ParseResult(())


def write_tables(conn, context):
    return WriteResult(parse_tables(context), ())


def validate(context):
    """Repeat the full import's fact, report, section and chunk checks; select no tables."""
    full._require_versions(context)
    outputs = {name: base._parsed(context, name) for name in ("facts", "tables", "report", "chunks")}
    if any(not isinstance(result, ParseResult) for result in outputs.values()):
        raise ProcessingError("Text-only validation requires materialized parser results.")
    if outputs["tables"].items:
        raise ProcessingError("A text-only import must not select statement tables.")
    reports = outputs["report"].items
    if len(reports) != 1 or not isinstance(reports[0], (ParsedReport, ReportInput)):
        raise ProcessingError("Text-only validation requires one cleaned report.")
    facts = outputs["facts"].items
    if any(not isinstance(fact, FactInput) for fact in facts):
        raise ProcessingError("Text-only validation requires source fact records.")
    by_id = {fact.occurrence_key: fact for fact in facts}
    scope_valid = bool(facts) and len(by_id) == len(facts) and all(
        fact.filing_id == context.source_ids.filing_id
        and fact.source_document_id == context.source_ids.xml_document_id
        and fact.extraction_version == context.versions["facts"]
        and fact.source_reference.get("element_id") == fact.occurrence_key for fact in facts)
    path = base._source_path(context, "main_inline_xbrl_filing")
    correspondence = check_inline_correspondence(path, facts)
    document = html.document_fromstring(
        path.read_bytes(), parser=html.HTMLParser(encoding="utf-8", no_network=True)).getroottree()
    sections, locators, chunks = base._report_checks(context, reports[0], outputs["chunks"].items, document, path)
    checks = {
        "supported_versions": True,
        "no_rejected_records": not any(result.rejected for result in outputs.values()),
        "fact_source_scope": bool(scope_valid),
        "fact_inline_correspondence": (correspondence["matched"] == len(facts) == correspondence["inline_elements"]
                                       and not correspondence["mismatches"] and not correspondence["inline_ids_without_fact"]),
        "numeric_values_compared": correspondence["numeric_values_not_compared"] == 0,
        "report_text_matches_cleaner": reports[0].full_text == clean_report(path, form=context.manifest["form"]).full_text,
        "section_bounds_and_coverage": bool(sections), "section_source_locators": locators,
        "section_labels_not_promoted": all(section.get("validation_status") in {"detected", "unverified"}
                                           for section in reports[0].sections),
        "chunk_slices_and_coverage": chunks,
        "index_registrants_verified": bool(segmented.configuration_metadata(context.manifest, context.data_root)["index_registrants"]),
    }
    return ValidationResult(dict(VERSIONS), checks)


def validate_ingestion(conn, context):
    return validate(context)


def configuration_metadata(manifest, data_root):
    """The receipt identity shared by every text-only set of the same sources."""
    return segmented.configuration_metadata(manifest, data_root)


def get_dry_run_plan():
    plan = full.get_dry_run_plan()
    return replace(plan, stages=replace(plan.stages, tables=parse_tables, validate=validate),
                   required_checks=REQUIRED_CHECKS)


def get_ingestion_plan(tables_unavailable):
    """Record why the full import failed alongside the sealed text-only selection."""
    reason = dict(tables_unavailable)

    def metadata(manifest, data_root):
        return {**configuration_metadata(manifest, data_root), "tables_unavailable": reason}

    plan = full.get_ingestion_plan()
    return replace(plan, stages=replace(plan.stages, tables=write_tables, validate=validate_ingestion),
                   required_checks=REQUIRED_CHECKS, configuration_metadata=metadata)
