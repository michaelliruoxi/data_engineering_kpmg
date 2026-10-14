"""Real V3 marker/nil acceptance remains distinct from V1 and V2 records."""

from copy import deepcopy
from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import UUID
from unittest.mock import patch

import pytest

from sec_pipeline import filing_adapter_v2, filing_adapter_v3
from sec_pipeline.filing_sources import verify_filing
from sec_pipeline.processing import DryRunContext, SourceIds
from sec_pipeline.source_cells_v3 import source_cell_counts


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("accession,source_only,hidden_nil", [
    ("0001104659-26-102442", 7, 0), ("0001104659-26-101932", 1, 2),
])
def test_archived_markers_and_hidden_nil_are_faithfully_represented(accession, source_only, hidden_nil):
    path = ROOT / "data/raw/sec" / accession / "source_manifest.json"
    if not path.is_file():
        pytest.skip("Optional archived source unavailable.")
    manifest = verify_filing(path, ROOT)
    outputs = {}
    context = DryRunContext(manifest=manifest, data_root=ROOT,
        source_ids=SourceIds(UUID(int=1), UUID(int=2), UUID(int=3)),
        versions=filing_adapter_v3.VERSIONS, outputs=outputs, report_id=UUID(int=4))
    plan = filing_adapter_v3.get_dry_run_plan()
    with patch("psycopg.connect", side_effect=AssertionError("Database access forbidden")), \
            patch("socket.create_connection", side_effect=AssertionError("Network access forbidden")):
        for stage in ("facts", "tables", "report", "chunks"):
            outputs[stage] = getattr(plan.stages, stage)(context)
        validation = plan.stages.validate(context)
        metadata = filing_adapter_v3.configuration_metadata(manifest, ROOT)
    counts = source_cell_counts(outputs["tables"].items)
    assert counts["source_only_marker_cells"] == source_only
    assert counts["hidden_nil_cells"] == hidden_nil
    assert counts["unresolved_cells"] == 0
    assert metadata["source_cell_counts"] == counts
    assert all(value is True for value in validation.checks.values()), validation.checks
    original = filing_adapter_v2.get_dry_run_plan().stages.tables(replace(context, versions=filing_adapter_v2.VERSIONS))
    assert original.unresolved_cells == source_only + hidden_nil
    assert all(table.extraction_version == "tables-v2" and table.structure["schema_version"] == 1 for table in original.items)


def test_v3_plan_adds_explicit_representation_checks_and_metadata_only():
    versions = deepcopy(filing_adapter_v2.VERSIONS)
    checks = filing_adapter_v2.REQUIRED_CHECKS
    plan = filing_adapter_v3.get_ingestion_plan()
    assert plan.configuration_metadata is filing_adapter_v3.configuration_metadata
    assert set(plan.required_checks) == set(checks) | {
        "source_only_markers_verified", "hidden_nil_bindings_verified", "financial_cell_inventory_complete",
    }
    assert filing_adapter_v2.VERSIONS == versions and filing_adapter_v2.REQUIRED_CHECKS == checks


@pytest.fixture(scope="module")
def archived_marker_context():
    path = ROOT / "data/raw/sec/0001104659-26-102442/source_manifest.json"
    if not path.is_file():
        pytest.skip("Optional archived source unavailable.")
    outputs = {}
    context = DryRunContext(manifest=verify_filing(path, ROOT), data_root=ROOT,
        source_ids=SourceIds(UUID(int=1), UUID(int=2), UUID(int=3)),
        versions=filing_adapter_v3.VERSIONS, outputs=outputs, report_id=UUID(int=4))
    plan = filing_adapter_v3.get_dry_run_plan()
    with patch("psycopg.connect", side_effect=AssertionError("Database access forbidden")), \
            patch("socket.create_connection", side_effect=AssertionError("Network access forbidden")):
        for stage in ("facts", "tables", "report", "chunks"):
            outputs[stage] = getattr(plan.stages, stage)(context)
    return context


@pytest.mark.parametrize("classification,clear_period", [
    ("source_only_marker", True), ("resolved", False),
])
def test_source_inventory_rejects_financial_cells_reclassified_as_formatting(
        archived_marker_context, classification, clear_period):
    context = archived_marker_context
    tables = deepcopy(context.outputs["tables"].items)
    selected = next((table, cell) for table in tables for row in table.structure["rows"]
                    for cell in row if cell["classification"] == classification)
    table, cell = selected
    cell.update(classification="formatting", fact_references=[])
    cell.pop("source_only_evidence", None)
    if clear_period:
        cell["column_period"] = None
    table.structure["inventory"] = dict(Counter(
        item["classification"] for row in table.structure["rows"] for item in row))
    outputs = {**context.outputs, "tables": replace(context.outputs["tables"], items=tables)}
    with patch("psycopg.connect", side_effect=AssertionError("Database access forbidden")), \
            patch("socket.create_connection", side_effect=AssertionError("Network access forbidden")):
        validation = filing_adapter_v3.validate(replace(context, outputs=outputs))
    assert validation.checks["financial_cell_inventory_complete"] is False
