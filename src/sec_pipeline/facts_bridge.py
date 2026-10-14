"""Connect Bryce's facts-v1 parser/loader to Jace's runner contracts.

The parser is supplied by PR #7. Imports are lazy so importing this bridge or
showing command help does not require unpublished parser modules or open a DB.
Complete plans still require the table, cleaner, and validation adapters.
"""

from collections.abc import Mapping
from pathlib import Path

from .database import FactInput
from .processing import (
    DryRunPlan, DryRunStages, IngestionPlan, ParseResult, PipelineStages,
    ProcessingError, WriteResult, store_ingestion_chunks,
)


FACTS_VERSION = "facts-v1"
XML_ROLE = "extracted_xbrl_instance"


def _version(context):
    if context.versions.get("facts") != FACTS_VERSION:
        raise ProcessingError("The current financial facts bridge requires --facts-version facts-v1.")


def _xml_path(context):
    records = [record for record in context.manifest["files"] if record.get("role") == XML_ROLE]
    if len(records) != 1:
        raise ProcessingError("The facts bridge requires exactly one XML source in the manifest.")
    path = Path(records[0]["local_path"])
    root = Path(context.data_root).resolve()
    resolved = (root / path).resolve()
    if path.is_absolute() or not resolved.is_relative_to(root):
        raise ProcessingError("The facts XML path must remain within the verified data root.")
    return resolved


def _extract(context):
    _version(context)
    try:
        from .fact_extraction import extract_facts
    except ModuleNotFoundError as exc:
        if exc.name != "sec_pipeline.fact_extraction":
            raise
        raise ProcessingError("Financial facts code from PR #7 is required before using this bridge.") from exc
    return extract_facts(
        _xml_path(context), filing_id=context.source_ids.filing_id,
        source_document_id=context.source_ids.xml_document_id,
        extraction_version=FACTS_VERSION,
    )


def parse_facts(context):
    """Pure facts stage: preserve accepted records and extraction failures."""
    extraction = _extract(context)
    return ParseResult(extraction.facts, rejected=extraction.failures)


def write_facts(conn, context, *, bulk=False):
    """Write a complete extraction through the caller's existing transaction."""
    extraction = _extract(context).require_complete()
    from .fact_extraction import store_facts

    stored = store_facts(conn, extraction.facts, bulk=True) if bulk else store_facts(conn, extraction.facts)
    if (
        stored.filing_id != context.source_ids.filing_id
        or stored.source_document_id != context.source_ids.xml_document_id
        or stored.extraction_version != FACTS_VERSION
    ):
        raise ProcessingError("Stored fact IDs belong to a different filing, XML source, or version.")
    if not isinstance(stored.fact_ids, Mapping) or set(stored.fact_ids) != {
        fact.occurrence_key for fact in extraction.facts
    }:
        raise ProcessingError("Stored fact IDs do not cover exactly the accepted occurrence keys.")
    return WriteResult(
        ParseResult(extraction.facts),
        [stored.fact_ids[fact.occurrence_key] for fact in extraction.facts],
    )


def fact_ids_for_tables(context):
    """Resolve inline IDs using this run's facts, never amounts or another DB."""
    _version(context)
    result = context.outputs.get("facts")
    if not isinstance(result, WriteResult):
        raise ProcessingError("Table writes need the WriteResult from the preceding facts stage.")
    mapping = {}
    for record, ident in zip(result.parsed.items, result.stored_ids):
        if (
            not isinstance(record, FactInput)
            or record.filing_id != context.source_ids.filing_id
            or record.source_document_id != context.source_ids.xml_document_id
            or record.extraction_version != FACTS_VERSION
        ):
            raise ProcessingError("Table fact IDs must use this filing, XML source, and facts-v1.")
        key = record.occurrence_key
        if not isinstance(key, str) or not key or key in mapping:
            raise ProcessingError("Table fact IDs require distinct non-empty occurrence keys.")
        inline_id = record.source_reference.get("element_id")
        if inline_id is not None and inline_id != key:
            raise ProcessingError("The fact occurrence key disagrees with its original inline ID.")
        mapping[key] = ident
    return mapping


def _require_other_stages(tables, report, validate):
    if any(not callable(stage) for stage in (tables, report, validate)):
        raise ProcessingError("Supply the table, report, and validation adapters to build a full plan.")


def build_dry_run_plan(*, tables, report, validate, required_checks):
    """Wire the facts parser and real chunker with the other pure bridges."""
    _require_other_stages(tables, report, validate)
    return DryRunPlan(DryRunStages(
        facts=parse_facts, tables=tables, report=report, validate=validate,
    ), required_checks)


def build_ingestion_plan(*, tables, report, validate, required_checks):
    """Wire the facts writer and real chunk writer with the other bridges."""
    _require_other_stages(tables, report, validate)
    return IngestionPlan(PipelineStages(
        facts=write_facts, tables=tables, report=report,
        chunks=store_ingestion_chunks, validate=validate,
    ), required_checks)
