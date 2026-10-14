"""Source-grounded complete adapter checks; no live database or network."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest

from sec_pipeline import filing_adapter as adapter
from sec_pipeline.database import ReportInput
from sec_pipeline.processing import (
    DryRunContext, ParseResult, PipelineContext, ProcessingError, SourceIds,
    WriteResult, _check_validation, run_dry_run, store_ingestion_chunks,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sample():
    outputs = {}
    context = DryRunContext(
        manifest=json.loads((ROOT / "source_manifest.json").read_text(encoding="utf-8")),
        data_root=ROOT, source_ids=SourceIds(UUID(int=1), UUID(int=2), UUID(int=3)),
        versions=dict(adapter.VERSIONS), outputs=outputs, report_id=UUID(int=4),
    )
    plan = adapter.get_dry_run_plan()
    with patch("psycopg.connect", side_effect=AssertionError("Unexpected database connection")), \
            patch("socket.create_connection", side_effect=AssertionError("Unexpected network request")):
        for name in ("facts", "tables", "report", "chunks"):
            outputs[name] = getattr(plan.stages, name)(context)
    return context


def test_sample_sources_validate_without_promoting_semantic_labels(sample):
    result = adapter.validate(sample)
    assert set(result.checks) == set(adapter.REQUIRED_CHECKS)
    assert all(value is True for value in result.checks.values()), result.checks
    assert len(sample.outputs["facts"].items) == 1622
    assert len(sample.outputs["tables"].items) == 3
    assert len(sample.outputs["chunks"].items) > 100
    assert {section["validation_status"] for section in sample.outputs["report"].items[0].sections} <= {
        "detected", "unverified",
    }
    assert not any("complete" in check for check in result.checks)


def test_plan_factories_and_report_storage_use_existing_contracts(sample):
    dry, ingestion = adapter.get_dry_run_plan(), adapter.get_ingestion_plan()
    assert dry.required_checks == ingestion.required_checks == adapter.REQUIRED_CHECKS
    assert ingestion.stages.chunks.func is store_ingestion_chunks
    assert ingestion.stages.chunks.keywords == {"bulk": True}
    conn = object()
    with patch.object(adapter, "store_report", return_value=UUID(int=90)) as store:
        result = adapter.write_report(conn, sample)
    record = result.parsed.items[0]
    assert isinstance(record, ReportInput)
    assert record.full_text == sample.outputs["report"].items[0].full_text
    assert record.sections == list(sample.outputs["report"].items[0].sections)
    assert record.source_document_id == sample.source_ids.html_document_id
    assert result.stored_ids == (UUID(int=90),)
    store.assert_called_once_with(conn, record)


def test_validation_accepts_transaction_stage_outputs(sample):
    outputs = {}
    for number, (name, parsed) in enumerate(sample.outputs.items()):
        if name == "report":
            report = parsed.items[0]
            parsed = ParseResult([ReportInput(
                filing_id=sample.source_ids.filing_id,
                source_document_id=sample.source_ids.html_document_id,
                extraction_version=adapter.VERSIONS["reports"],
                full_text=report.full_text, sections=list(report.sections),
            )])
            ids = [sample.report_id]
        else:
            ids = [UUID(int=10000 * (number + 1) + i) for i in range(len(parsed.items))]
        outputs[name] = WriteResult(parsed, ids)
    context = PipelineContext(
        manifest=sample.manifest, data_root=sample.data_root, source_ids=sample.source_ids,
        versions=sample.versions, outputs=outputs,
    )
    assert all(adapter.validate_ingestion(object(), context).checks.values())


@pytest.mark.parametrize("stage,diagnostic", [("facts", "rejected"), ("tables", "unresolved_cells")])
def test_diagnostics_block_success_even_when_records_match(sample, stage, diagnostic):
    outputs = dict(sample.outputs)
    outputs[stage] = replace(outputs[stage], **{diagnostic: ["unsupported"] if diagnostic == "rejected" else 1})
    result = adapter.validate(replace(sample, outputs=outputs))
    assert result.checks["no_rejected_records" if diagnostic == "rejected" else "no_unresolved_cells"] is False
    with pytest.raises(ProcessingError, match="Validation failed"):
        _check_validation(result, sample.versions, adapter.REQUIRED_CHECKS)


def test_actual_inline_value_mismatch_blocks_validation(sample):
    outputs = dict(sample.outputs)
    facts = list(outputs["facts"].items)
    index = next(index for index, fact in enumerate(facts) if fact.numeric_value is not None)
    facts[index] = replace(facts[index], numeric_value=facts[index].numeric_value + Decimal("1"))
    outputs["facts"] = ParseResult(facts)
    assert adapter.validate(replace(sample, outputs=outputs)).checks["fact_inline_correspondence"] is False


def test_absent_inline_occurrence_blocks_validation(sample):
    outputs = dict(sample.outputs)
    outputs["facts"] = ParseResult(outputs["facts"].items[1:])
    assert adapter.validate(replace(sample, outputs=outputs)).checks["fact_inline_correspondence"] is False


def test_table_references_must_match_current_extracted_facts(sample):
    outputs = dict(sample.outputs)
    tables = deepcopy(list(outputs["tables"].items))
    ref = next(ref for row in tables[0].structure["rows"] for cell in row for ref in cell["fact_references"])
    ref["raw_value"] = "999999999999999999999"
    outputs["tables"] = ParseResult(tables)
    assert adapter.validate(replace(sample, outputs=outputs)).checks["table_fact_references"] is False


def test_missing_source_table_row_cannot_pass_with_matching_inventory(sample):
    outputs = dict(sample.outputs)
    tables = deepcopy(list(outputs["tables"].items))
    tables[0].structure["rows"].pop()
    outputs["tables"] = ParseResult(tables)
    assert adapter.validate(replace(sample, outputs=outputs)).checks["table_source_cells"] is False


def test_unresolved_cell_cannot_hide_behind_zero_inventory(sample):
    outputs = dict(sample.outputs)
    tables = deepcopy(list(outputs["tables"].items))
    cell = next(cell for row in tables[0].structure["rows"] for cell in row if cell["classification"] == "resolved")
    cell.update(classification="unresolved", fact_references=[], unresolved_reason="unsupported")
    outputs["tables"] = ParseResult(tables)
    result = adapter.validate(replace(sample, outputs=outputs))
    assert result.checks["no_unresolved_cells"] is False
    assert result.checks["table_source_cells"] is False


def test_numeric_cell_cannot_be_removed_by_reclassifying_it_as_formatting(sample):
    from collections import Counter
    outputs = dict(sample.outputs)
    tables = deepcopy(list(outputs["tables"].items))
    cell = next(cell for row in tables[0].structure["rows"] for cell in row if cell["classification"] == "resolved")
    cell.update(classification="formatting", fact_references=[], unresolved_reason=None)
    tables[0].structure["inventory"] = dict(Counter(cell["classification"] for row in tables[0].structure["rows"] for cell in row))
    outputs["tables"] = ParseResult(tables)
    result = adapter.validate(replace(sample, outputs=outputs))
    assert result.checks["table_source_cells"] is False


@pytest.mark.parametrize("change", ["text", "missing", "bounds", "section", "report_id"])
def test_corrupt_or_missing_chunks_block_validation(sample, change):
    outputs = dict(sample.outputs)
    chunks = list(outputs["chunks"].items)
    if change == "missing":
        chunks.pop()
    else:
        changes = {
            "text": {"text": chunks[0].text + "changed"},
            "bounds": {"offset_end": len(sample.outputs["report"].items[0].full_text) + 1},
            "section": {"section": "wrong-section"},
            "report_id": {"report_id": UUID(int=999)},
        }
        chunks[0] = replace(chunks[0], **changes[change])
    outputs["chunks"] = ParseResult(chunks)
    assert adapter.validate(replace(sample, outputs=outputs)).checks["chunk_slices_and_coverage"] is False


def test_section_content_gap_is_rejected(sample):
    outputs = dict(sample.outputs)
    report = deepcopy(outputs["report"].items[0])
    report.sections[0]["offset_start"] = 1
    outputs["report"] = ParseResult([report])
    assert adapter.validate(replace(sample, outputs=outputs)).checks["section_bounds_and_coverage"] is False


def test_section_labels_cannot_claim_independent_validation(sample):
    outputs = dict(sample.outputs)
    report = deepcopy(outputs["report"].items[0])
    report.sections[0]["validation_status"] = "validated"
    outputs["report"] = ParseResult([report])
    assert adapter.validate(replace(sample, outputs=outputs)).checks["section_labels_not_promoted"] is False


def test_visible_text_coverage_allows_whitespace_gaps_only():
    assert adapter._covers_text("alpha\n\n beta", [(0, 5), (8, 12)])
    assert not adapter._covers_text("alpha lost beta", [(0, 5), (11, 15)])


def test_report_rejects_unsupported_version_and_escaping_source(sample):
    with pytest.raises(ProcessingError, match="requires facts-v1"):
        adapter.parse_report(replace(sample, versions=dict(sample.versions, reports="report-v2")))
    manifest = deepcopy(sample.manifest)
    next(record for record in manifest["files"] if record["role"] == "main_inline_xbrl_filing")["local_path"] = "../outside.htm"
    with pytest.raises(ProcessingError, match="verified data root"):
        adapter.parse_report(replace(sample, manifest=manifest))


def test_runner_blocks_actual_source_validation_failure(sample):
    plan = adapter.get_dry_run_plan()

    def broken_facts(context):
        result = plan.stages.facts(context)
        return replace(result, rejected=["unsupported source occurrence"])

    with patch("psycopg.connect", side_effect=AssertionError("Unexpected database connection")):
        with pytest.raises(ProcessingError, match="Validation failed"):
            run_dry_run(
                manifest_path=ROOT / "source_manifest.json", data_root=ROOT,
                versions=adapter.VERSIONS, stages=replace(plan.stages, facts=broken_facts),
                required_checks=plan.required_checks,
                verify_inputs=lambda path, root: json.loads(path.read_text(encoding="utf-8")),
            )
