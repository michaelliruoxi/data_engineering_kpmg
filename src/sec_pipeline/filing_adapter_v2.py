"""Versioned filing plans preserving each physical statement segment."""

from dataclasses import replace

from lxml import html

from . import filing_adapter as base
from .database import CompanyInput, register_company
from .processing import ProcessingError, ValidationResult
from .tables import validate_statement_segments


VERSIONS = {**base.VERSIONS, "tables": "tables-v2"}
REQUIRED_CHECKS = (*base.REQUIRED_CHECKS, "statement_segment_coverage", "index_registrants_verified")


def configuration_metadata(manifest, data_root):
    from .registrants import index_registrants
    return {"index_registrants": index_registrants(dict(manifest), data_root)}


def validate(context):
    if dict(context.versions) != VERSIONS:
        raise ProcessingError("The segmented filing plan requires tables-v2 and the unchanged fact/report/chunk versions.")
    validation = base.validate(context, allow_segments=True)
    tables = base._parsed(context, "tables").items
    facts = base._parsed(context, "facts").items
    path = base._source_path(context, "main_inline_xbrl_filing")
    document = html.document_fromstring(path.read_bytes(), parser=html.HTMLParser(encoding="utf-8", no_network=True)).getroottree()
    try:
        groups_valid = validate_statement_segments(
            tables, document, facts, xml_path=base._source_path(context, 'extracted_xbrl_instance')) is True
    except ValueError:
        groups_valid = False
    return ValidationResult(dict(context.versions), {
        **validation.checks, "statement_segment_coverage": groups_valid,
        "index_registrants_verified": bool(configuration_metadata(context.manifest, context.data_root)["index_registrants"]),
    })


def validate_ingestion(conn, context):
    return validate(context)


def write_facts(conn, context):
    """Register verified co-filers in the same transaction as their source data."""
    metadata = configuration_metadata(context.manifest, context.data_root)
    for registrant in metadata["index_registrants"]:
        register_company(conn, CompanyInput(cik=registrant["cik"], name=registrant["name"]))
    return base.write_facts(conn, context, bulk=True)


def get_dry_run_plan():
    plan = base.get_dry_run_plan()
    return replace(plan, stages=replace(plan.stages, validate=validate), required_checks=REQUIRED_CHECKS)


def get_ingestion_plan():
    plan = base.get_ingestion_plan()
    return replace(plan, stages=replace(plan.stages, facts=write_facts, validate=validate_ingestion), required_checks=REQUIRED_CHECKS,
                   configuration_metadata=configuration_metadata)
