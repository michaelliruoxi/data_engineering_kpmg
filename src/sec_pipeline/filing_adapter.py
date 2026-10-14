"""Complete filing plans with source checks and caller-owned transactions.

The table extractor currently supports selected primary statements. Passing
these checks establishes correspondence for those tables, not completeness of
all financial disclosures. Report section labels remain heuristic.
"""

from collections import Counter
from dataclasses import replace
from functools import partial
from pathlib import Path
from uuid import UUID

from lxml import html

from .database import ChunkInput, FactInput, ReportInput, store_report
from .fact_extraction import IX, check_inline_correspondence
from .facts_bridge import build_dry_run_plan, build_ingestion_plan, write_facts
from .processing import (
    ParseResult, ParsedReport, ProcessingError, ValidationResult, WriteResult, store_ingestion_chunks,
)
from .report_cleaning import clean_report
from .sections import normalize_sections
from .source_locators import inline_elements, inline_source_xpath, source_xpath
from .table_cells import relational_cells
from .tables import layout_grid
from .tables_bridge import parse_tables, write_tables


VERSIONS = {
    "facts": "facts-v1", "tables": "tables-v1",
    "reports": "report-v1", "chunks": "paragraph-v1",
}
REQUIRED_CHECKS = (
    "supported_versions", "no_rejected_records", "no_unresolved_cells",
    "fact_source_scope", "fact_inline_correspondence", "numeric_values_compared",
    "table_fact_references", "table_source_cells", "report_text_matches_cleaner",
    "section_bounds_and_coverage", "section_source_locators",
    "section_labels_not_promoted", "chunk_slices_and_coverage",
)


def _source_path(context, role):
    records = [record for record in context.manifest["files"] if record.get("role") == role]
    if len(records) != 1:
        raise ProcessingError(f"Filing adapter requires exactly one {role} source.")
    root = Path(context.data_root).resolve()
    path = Path(records[0]["local_path"])
    resolved = (root / path).resolve()
    if path.is_absolute() or not resolved.is_relative_to(root):
        raise ProcessingError("Filing sources must remain within the verified data root.")
    return resolved


def _require_versions(context):
    expected = dict(VERSIONS)
    expected["tables"] = context.versions.get("tables")
    if expected["tables"] not in {"tables-v1", "tables-v2"} or dict(context.versions) != expected:
        raise ProcessingError("Filing adapter requires facts-v1, tables-v1 or tables-v2, report-v1, and paragraph-v1.")


def parse_report(context):
    """Clean the verified primary HTML without storing or promoting labels."""
    _require_versions(context)
    report = clean_report(
        _source_path(context, "main_inline_xbrl_filing"),
        form=context.manifest["form"],
    )
    return ParseResult([ParsedReport(report.full_text, report.sections)])


def write_report(conn, context):
    """Store the report and normalized sections through the existing helper."""
    parsed = parse_report(context).items[0]
    report = ReportInput(
        filing_id=context.source_ids.filing_id,
        source_document_id=context.source_ids.html_document_id,
        extraction_version=context.versions["reports"],
        full_text=parsed.full_text, sections=[dict(section) for section in parsed.sections],
    )
    return WriteResult(ParseResult([report]), [store_report(conn, report)])


def _parsed(context, name):
    result = context.outputs[name]
    return result.parsed if isinstance(result, WriteResult) else result


def _covers_text(full_text, ranges):
    """Allow overlap and whitespace gaps, never dropped visible characters."""
    end = 0
    for start, stop in sorted(ranges):
        if full_text[end:start].strip():
            return False
        end = max(end, stop)
    return not full_text[end:].strip()


def _fact_reference(fact):
    period = ({"instant": fact.instant_date.isoformat()} if fact.period_kind == "instant"
              else {"startDate": fact.period_start.isoformat(), "endDate": fact.period_end.isoformat()}
              if fact.period_kind == "duration" else {"forever": ""})
    return {
        "concept": "{" + fact.concept_namespace + "}" + fact.concept_name,
        "context_ref": fact.source_reference.get("context_ref"),
        "unit_ref": fact.source_reference.get("unit_ref"), "unit": fact.unit,
        "raw_value": "" if fact.is_nil else fact.raw_value,
        "is_nil": fact.is_nil, "period": period, "dimensions": fact.dimensions,
    }


def _table_checks(context, tables, facts, document):
    references_valid = cells_valid = bool(tables)
    unresolved = False
    scope = {"filing_id": str(context.source_ids.filing_id),
             "xml_document_id": str(context.source_ids.xml_document_id),
             "facts_version": context.versions["facts"]}
    source_hashes = {record["role"]: record["sha256"] for record in context.manifest["files"]}
    keys = [table.table_key for table in tables]
    cells_valid &= len(set(keys)) == len(keys)
    fields = ("cell_id", "row", "column", "rowspan", "colspan", "display_text", "source_xpath")
    inline_sources = {inline_source_xpath(document, node): node
                      for node in inline_elements(document, IX, "nonfraction")}
    for table in tables:
        structure = table.structure
        cells = relational_cells(UUID(int=0), table)
        unresolved |= any(cell.resolution_status == "unresolved" for cell, _ in cells)
        references_valid &= (
            table.filing_id == context.source_ids.filing_id
            and table.source_document_id == context.source_ids.html_document_id
            and table.extraction_version == context.versions["tables"]
            and structure.get("fact_scope") == scope
        )
        anchors = document.xpath(structure["source_xpath"])
        if len(anchors) != 1 or anchors[0].tag.lower() != "table":
            cells_valid = False
        else:
            source_rows, grid = layout_grid(anchors[0], document)
            actual = [{name: cell.get(name) for name in fields}
                      for row in structure["rows"] for cell in row]
            expected = [{name: cell.get(name) for name in fields}
                        for row in source_rows for cell in row]
            cells_valid &= actual == expected and structure.get("grid") == grid
        inventory = dict(Counter(cell["classification"] for row in structure["rows"] for cell in row))
        cells_valid &= (
            structure.get("inventory") == inventory
            and structure.get("html_sha256") == source_hashes["main_inline_xbrl_filing"]
            and structure.get("xml_sha256") == source_hashes["extracted_xbrl_instance"]
        )
        unresolved |= bool(structure.get("inventory", {}).get("unresolved", 0))
        for cell, refs in cells:
            for ref in refs:
                fact = facts.get(ref["xml_id"])
                references_valid &= fact is not None and ref.get("scope") == scope
                if fact is not None:
                    references_valid &= all(ref.get(name) == value for name, value in _fact_reference(fact).items())
                source = inline_sources.get(ref["inline_xpath"])
                references_valid &= source is not None and source.get("id") == ref["xml_id"]
                if source is not None:
                    cell_xpath = cell.source_locator["xpath"]
                    ancestor_paths = {source_xpath(document, node) for node in source.iterancestors()}
                    if ref["method"] == "inline_id":
                        references_valid &= cell_xpath in ancestor_paths
                    else:
                        target = document.xpath(cell_xpath)
                        source_row = next((node for node in source.iterancestors() if node.tag == "tr"), None)
                        target_row = next((node for node in target[0].iterancestors() if node.tag == "tr"), None) if target else None
                        references_valid &= source_row is not None and source_row is target_row
                        references_valid &= ref.get("repeated_from_inline_xpath") == ref["inline_xpath"]
    return bool(references_valid), bool(cells_valid), not unresolved


def _report_checks(context, report, chunks, document, html_path):
    full_text = report.full_text
    record = ReportInput(
        filing_id=context.source_ids.filing_id, source_document_id=context.source_ids.html_document_id,
        extraction_version=context.versions["reports"], full_text=full_text,
        sections=[dict(section) for section in report.sections],
    )
    sections = normalize_sections(record)
    sections_valid = bool(sections) and _covers_text(full_text, [(s.offset_start, s.offset_end) for s in sections])
    locators_valid = True
    for section in sections:
        locator = section.source_locator
        matches = document.xpath(locator["xpath"]) if locator.get("xpath") else []
        locators_valid &= len(matches) == 1 and locator.get("source_file") == html_path.name
        if matches and locator.get("element_id"):
            locators_valid &= matches[0].get("id") == locator["element_id"]
    report_result = context.outputs["report"]
    report_id = report_result.stored_ids[0] if isinstance(report_result, WriteResult) else context.report_id
    by_key = {section.section_key: section for section in sections}
    chunk_valid = bool(chunks)
    previous_start, previous_end = -1, -1
    ranges = []
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, ChunkInput):
            return sections_valid, bool(locators_valid), False
        start, end = chunk.offset_start, chunk.offset_end
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(full_text):
            chunk_valid = False
            continue
        section = by_key.get(chunk.section)
        chunk_valid &= (
            chunk.filing_id == context.source_ids.filing_id and chunk.report_id == report_id
            and chunk.chunking_version == context.versions["chunks"] and chunk.chunk_index == index
            and chunk.text == full_text[start:end] and bool(chunk.text.strip())
            and start > previous_start and end > previous_end
            and section is not None
        )
        if section is not None:
            chunk_valid &= (section.offset_start <= start < end <= section.offset_end
                            and chunk.metadata.get("section_id") == section.section_key
                            and chunk.metadata.get("source_locator") == section.source_locator)
        ranges.append((start, end))
        previous_start, previous_end = start, end
    return sections_valid, bool(locators_valid), bool(chunk_valid and _covers_text(full_text, ranges))


def validate(context, *, allow_segments=False):
    """Validate actual stage records against source evidence; never trust counts alone."""
    _require_versions(context)
    if context.versions["tables"] == "tables-v2" and not allow_segments:
        raise ProcessingError("tables-v2 requires the segmented filing adapter and its group validation.")
    outputs = {name: _parsed(context, name) for name in ("facts", "tables", "report", "chunks")}
    if any(not isinstance(result, ParseResult) for result in outputs.values()):
        raise ProcessingError("Validation requires materialized parser results for every stage.")
    reports = outputs["report"].items
    if len(reports) != 1 or not isinstance(reports[0], (ParsedReport, ReportInput)):
        raise ProcessingError("Validation requires one cleaned report.")
    facts = outputs["facts"].items
    if any(not isinstance(fact, FactInput) for fact in facts):
        raise ProcessingError("Validation requires extracted FactInput records.")
    by_id = {fact.occurrence_key: fact for fact in facts}
    scope_valid = bool(facts) and len(by_id) == len(facts) and all(
        fact.filing_id == context.source_ids.filing_id
        and fact.source_document_id == context.source_ids.xml_document_id
        and fact.extraction_version == context.versions["facts"]
        and fact.source_reference.get("element_id") == fact.occurrence_key for fact in facts)
    html_path = _source_path(context, "main_inline_xbrl_filing")
    correspondence = check_inline_correspondence(html_path, facts)
    document = html.document_fromstring(
        html_path.read_bytes(), parser=html.HTMLParser(encoding="utf-8", no_network=True),
    ).getroottree()
    references, cells, resolved = _table_checks(context, outputs["tables"].items, by_id, document)
    # Source locators alone cannot prove classifications, headings or periods:
    # a changed payload could otherwise hide a numeric cell as formatting.
    # Reconstruct the complete selected layout from archived HTML/XML so the
    # validation boundary is independent of the adapter's retained claims.
    try:
        original_tables = parse_tables(context).items
        actual_tables = outputs["tables"].items
        cells = bool(cells and len(actual_tables) == len(original_tables)
                     and {table.table_key: table for table in actual_tables}
                     == {table.table_key: table for table in original_tables})
    except (ValueError, ProcessingError):
        cells = False
    sections, locators, chunks = _report_checks(context, reports[0], outputs["chunks"].items, document, html_path)
    checks = {
        "supported_versions": True,
        "no_rejected_records": not any(result.rejected for result in outputs.values()),
        "no_unresolved_cells": resolved and not any(result.unresolved_cells for result in outputs.values()),
        "fact_source_scope": bool(scope_valid),
        "fact_inline_correspondence": (
            correspondence["matched"] == len(facts) == correspondence["inline_elements"]
            and not correspondence["mismatches"] and not correspondence["inline_ids_without_fact"]),
        "numeric_values_compared": correspondence["numeric_values_not_compared"] == 0,
        "table_fact_references": references, "table_source_cells": cells,
        "report_text_matches_cleaner": reports[0].full_text == clean_report(html_path, form=context.manifest["form"]).full_text,
        "section_bounds_and_coverage": bool(sections), "section_source_locators": locators,
        "section_labels_not_promoted": all(section.get("validation_status") in {"detected", "unverified"}
                                           for section in reports[0].sections),
        "chunk_slices_and_coverage": chunks,
    }
    return ValidationResult(dict(context.versions), checks)


def validate_ingestion(conn, context):
    """The runner additionally verifies stored IDs and source hashes before commit."""
    return validate(context)


def get_dry_run_plan():
    return build_dry_run_plan(tables=parse_tables, report=parse_report, validate=validate,
                              required_checks=REQUIRED_CHECKS)


def get_ingestion_plan():
    plan = build_ingestion_plan(tables=partial(write_tables, bulk=True), report=write_report,
                                validate=validate_ingestion, required_checks=REQUIRED_CHECKS)
    return replace(plan, stages=replace(plan.stages, facts=partial(write_facts, bulk=True),
                                       chunks=partial(store_ingestion_chunks, bulk=True)))
