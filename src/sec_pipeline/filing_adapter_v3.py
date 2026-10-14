"""Source-aware table representation with explicit literal and hidden-nil counts."""

from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from . import filing_adapter as base
from . import filing_adapter_v2 as segmented
from .database import FactInput, ReportInput
from .fact_extraction import check_inline_correspondence
from .facts_bridge import fact_ids_for_tables, parse_facts
from .processing import ParseResult, ParsedReport, PipelineContext, ProcessingError, SourceIds, ValidationResult, WriteResult
from .report_cleaning import clean_report
from .source_cells_v3 import (
    POLICY, SourceBindings, SourceCellError, fact_reference, source_cell_counts,
    transform_tables, validate_representations,
)
from .source_locators import inline_source_xpath, source_xpath
from .table_cells import relational_cells
from .tables import FactScope, layout_grid, load_tables, validate_statement_segments
from .tables_bridge import parse_tables as parse_v2_tables


VERSIONS = {**segmented.VERSIONS, "tables": "tables-v3"}
REQUIRED_CHECKS = (*segmented.REQUIRED_CHECKS, "source_only_markers_verified",
                   "hidden_nil_bindings_verified", "financial_cell_inventory_complete")


def _require_versions(context):
    if dict(context.versions) != VERSIONS:
        raise ProcessingError("The source-aware filing plan requires tables-v3 and unchanged fact/report/chunk versions.")


def _v2_context(context):
    _require_versions(context)
    return replace(context, versions=dict(segmented.VERSIONS))


def parse_tables(context):
    parsed = parse_v2_tables(_v2_context(context))
    source = base._source_path(context, "main_inline_xbrl_filing").read_bytes()
    tables = transform_tables(parsed.items, source, base._parsed(context, "facts").items)
    return ParseResult(tables, rejected=parsed.rejected,
                       unresolved_cells=source_cell_counts(tables)["unresolved_cells"])


def write_tables(conn, context):
    parsed = parse_tables(context)
    scope = FactScope(context.source_ids.filing_id, context.source_ids.xml_document_id,
                      context.versions["facts"], fact_ids_for_tables(context))
    stored = load_tables(conn, parsed.items, scope, bulk=True)
    return WriteResult(parsed, [stored[table.table_key] for table in parsed.items])


def parse_report(context):
    return base.parse_report(_v2_context(context))


def write_report(conn, context):
    return base.write_report(conn, _v2_context(context))


def configuration_metadata(manifest, data_root):
    outputs = {}
    context = PipelineContext(manifest=manifest, data_root=Path(data_root),
        source_ids=SourceIds(UUID(int=1), UUID(int=2), UUID(int=3)), versions=VERSIONS, outputs=outputs)
    outputs["facts"] = parse_facts(context)
    tables = parse_tables(context).items
    return {**segmented.configuration_metadata(manifest, data_root),
            "source_value_policy": POLICY, "source_value_scope": "archived_dom",
            "source_cell_counts": source_cell_counts(tables)}


def _table_checks(context, tables, facts, bindings):
    document = bindings.tree
    scoped = {"filing_id": str(context.source_ids.filing_id),
              "xml_document_id": str(context.source_ids.xml_document_id), "facts_version": context.versions["facts"]}
    hashes = {record["role"]: record["sha256"] for record in context.manifest["files"]}
    references_valid = cells_valid = bool(tables)
    keys = [table.table_key for table in tables]
    cells_valid &= len(set(keys)) == len(keys)
    fields = ("cell_id", "row", "column", "rowspan", "colspan", "display_text", "source_xpath")
    for table in tables:
        cells = relational_cells(UUID(int=0), table)
        structure = table.structure
        references_valid &= (table.filing_id == context.source_ids.filing_id
                             and table.source_document_id == context.source_ids.html_document_id
                             and table.extraction_version == "tables-v3" and structure.get("fact_scope") == scoped)
        anchors = document.xpath(structure["source_xpath"])
        if len(anchors) != 1 or anchors[0].tag.lower() != "table":
            cells_valid = False
        else:
            original, grid = layout_grid(anchors[0], document)
            actual = [{name: cell.get(name) for name in fields} for row in structure["rows"] for cell in row]
            expected = [{name: cell.get(name) for name in fields} for row in original for cell in row]
            cells_valid &= actual == expected and structure.get("grid") == grid
        inventory = dict(Counter(cell["classification"] for row in structure["rows"] for cell in row))
        cells_valid &= (structure.get("inventory") == inventory
                        and structure.get("html_sha256") == hashes["main_inline_xbrl_filing"] == bindings.sha256
                        and structure.get("xml_sha256") == hashes["extracted_xbrl_instance"])
        for cell, refs in cells:
            for ref in refs:
                fact = facts.get(ref["xml_id"])
                references_valid &= fact is not None and ref.get("scope") == scoped
                if fact is not None:
                    references_valid &= all(ref.get(name) == value for name, value in fact_reference(fact).items())
                source = bindings.ids.get(ref["xml_id"], [])
                references_valid &= len(source) == 1
                if len(source) != 1:
                    continue
                source = source[0]
                references_valid &= inline_source_xpath(document, source) == ref["inline_xpath"]
                if ref["method"] == "hidden_inline_id":
                    # Independently checked from the fresh binding inventory by
                    # validate_representations, including the visible locator.
                    references_valid &= fact is not None and fact.is_nil is True
                elif ref["method"] == "inline_id":
                    references_valid &= cell.source_locator["xpath"] in {
                        source_xpath(document, node) for node in source.iterancestors()}
                elif ref["method"] == "same_row_semantic_repeat":
                    target = document.xpath(cell.source_locator["xpath"])
                    source_row = next((node for node in source.iterancestors() if node.tag == "tr"), None)
                    target_row = next((node for node in target[0].iterancestors() if node.tag == "tr"), None) if target else None
                    references_valid &= (source_row is not None and source_row is target_row
                                         and ref.get("repeated_from_inline_xpath") == ref["inline_xpath"])
                else:
                    references_valid = False
    return bool(references_valid), bool(cells_valid)


def validate(context):
    _require_versions(context)
    outputs = {name: base._parsed(context, name) for name in ("facts", "tables", "report", "chunks")}
    if any(not isinstance(result, ParseResult) for result in outputs.values()):
        raise ProcessingError("V3 validation requires materialized parser results.")
    reports = outputs["report"].items
    if len(reports) != 1 or not isinstance(reports[0], (ParsedReport, ReportInput)):
        raise ProcessingError("V3 validation requires one cleaned report.")
    facts = outputs["facts"].items
    if any(not isinstance(fact, FactInput) for fact in facts):
        raise ProcessingError("V3 validation requires source fact records.")
    by_id = {fact.occurrence_key: fact for fact in facts}
    scope_valid = bool(facts) and len(by_id) == len(facts) and all(
        fact.filing_id == context.source_ids.filing_id
        and fact.source_document_id == context.source_ids.xml_document_id
        and fact.extraction_version == context.versions["facts"]
        and fact.source_reference.get("element_id") == fact.occurrence_key for fact in facts)
    path = base._source_path(context, "main_inline_xbrl_filing")
    source = path.read_bytes()
    bindings = SourceBindings(source, facts)
    tables = outputs["tables"].items
    correspondence = check_inline_correspondence(path, facts)
    references, cells = _table_checks(context, tables, by_id, bindings)
    representations_valid = True
    try:
        counts = validate_representations(tables, source, facts)
    except SourceCellError:
        representations_valid = False
        counts = source_cell_counts(tables)
    # Rebuild classification, periods, and references from the archived sources.
    # Retained metadata must not be able to hide a cell from marker validation
    # or turn a tagged amount into formatting while preserving its source text.
    expected_tables = parse_tables(context).items
    actual_by_key = {table.table_key: table for table in tables}
    expected_by_key = {table.table_key: table for table in expected_tables}
    inventory_valid = (len(actual_by_key) == len(tables)
                       and len(expected_by_key) == len(expected_tables)
                       and actual_by_key == expected_by_key)
    sections, locators, chunks = base._report_checks(context, reports[0], outputs["chunks"].items, bindings.tree, path)
    try:
        segments = validate_statement_segments(
            [replace(table, extraction_version="tables-v2") for table in tables], bindings.tree, facts,
            xml_path=base._source_path(context, "extracted_xbrl_instance")) is True
    except ValueError:
        segments = False
    index_metadata = segmented.configuration_metadata(context.manifest, context.data_root)
    checks = {
        "supported_versions": True,
        "no_rejected_records": not any(result.rejected for result in outputs.values()),
        "no_unresolved_cells": counts["unresolved_cells"] == 0 and not any(result.unresolved_cells for result in outputs.values()),
        "fact_source_scope": bool(scope_valid),
        "fact_inline_correspondence": (correspondence["matched"] == len(facts) == correspondence["inline_elements"]
                                       and not correspondence["mismatches"] and not correspondence["inline_ids_without_fact"]),
        "numeric_values_compared": correspondence["numeric_values_not_compared"] == 0,
        "table_fact_references": references, "table_source_cells": cells,
        "report_text_matches_cleaner": reports[0].full_text == clean_report(path, form=context.manifest["form"]).full_text,
        "section_bounds_and_coverage": bool(sections), "section_source_locators": locators,
        "section_labels_not_promoted": all(section.get("validation_status") in {"detected", "unverified"}
                                           for section in reports[0].sections),
        "chunk_slices_and_coverage": chunks, "statement_segment_coverage": segments,
        "index_registrants_verified": bool(index_metadata["index_registrants"]),
        "source_only_markers_verified": representations_valid,
        "hidden_nil_bindings_verified": representations_valid,
        "financial_cell_inventory_complete": representations_valid and cells and inventory_valid,
    }
    return ValidationResult(dict(VERSIONS), checks)


def validate_ingestion(conn, context):
    return validate(context)


def get_dry_run_plan():
    plan = segmented.get_dry_run_plan()
    return replace(plan, stages=replace(plan.stages, tables=parse_tables, report=parse_report, validate=validate),
                   required_checks=REQUIRED_CHECKS)


def get_ingestion_plan():
    plan = segmented.get_ingestion_plan()
    return replace(plan, stages=replace(plan.stages, tables=write_tables, report=write_report, validate=validate_ingestion),
                   required_checks=REQUIRED_CHECKS, configuration_metadata=configuration_metadata)
