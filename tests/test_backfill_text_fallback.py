"""Facts and text still load, in a separate set, when a filing's v3 tables fail."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

import psycopg
import pytest

from sec_pipeline import backfill, filing_adapter_v3
from sec_pipeline import filing_adapter_v3_text as text
from sec_pipeline.filing_sources import verify_filing
from sec_pipeline.processing import DryRunContext, ParseResult, ProcessingError, SourceIds, WriteResult
from sec_pipeline.tables import TableExtractionError
from tests.test_backfill import Connection, queue  # noqa: F401 - fixtures
from tests.test_backfill_profiles import Destination, profiled_queue, result_for  # noqa: F401 - fixtures
import test_processing_ingest as ingestion_fixtures


ROOT = Path(__file__).resolve().parents[1]
TABLE_MESSAGE = "income_statement: ambiguous or incomplete statement tables"


def table_failure():
    error = ProcessingError("Ingestion failed during tables.")
    error.stage = "tables"
    error.__cause__ = TableExtractionError(TABLE_MESSAGE)
    return error


@pytest.fixture
def fallback_queue(profiled_queue, monkeypatch):
    monkeypatch.setattr(text, "configuration_metadata", lambda manifest, root: {
        "index_registrants": [{"cik": manifest["cik"], "name": manifest["company"], "is_canonical": True}]})
    monkeypatch.setattr(text, "get_dry_run_plan", lambda: SimpleNamespace(
        stages="text-stages", required_checks=text.REQUIRED_CHECKS))
    plans = []

    def ingestion_plan(reason):
        plans.append(reason)
        return "text-plan"

    monkeypatch.setattr(text, "get_ingestion_plan", ingestion_plan)
    profiled_queue.text_plans = plans
    return profiled_queue


def run(queue, mode="ingest", connection=None, **options):
    return backfill.process_queue(queue.path, queue.state, None, queue.root, mode=mode,
        connection=connection if connection is not None else (Destination() if mode == "ingest" else None),
        destination="db" if mode == "ingest" else None, limit=1, tables_version="tables-v3",
        emit=lambda event: None, **options)


def saved(queue, accession):
    return json.loads(queue.path.read_text())["filings"][accession]


def test_table_failure_loads_facts_and_text_in_a_separate_set(fallback_queue):
    queue = fallback_queue
    accession = queue.accessions[0]
    keys = []

    def ingest(connection, **kwargs):
        keys.append(kwargs["processing_set_key"])
        if kwargs["processing_set_key"] == "filing-import-v3":
            raise table_failure()
        return result_for(queue, accession, text)

    queue.ingest.side_effect = ingest
    target = Destination()
    run(queue, connection=target)
    entry = saved(queue, accession)
    detail = {"stage": "tables", "error_type": "ProcessingError", "cause_type": "TableExtractionError",
              "message": TABLE_MESSAGE}
    assert keys == ["filing-import-v3", "filing-import-v3-text"]
    assert entry["status"] == "ingested_review_required"
    assert entry["result"]["set_key"] == "filing-import-v3-text"
    assert entry["result"]["tables_unavailable"] == detail
    assert queue.text_plans == [detail]
    assert queue.ingest.call_args.kwargs["coverage"] == text.COVERAGE
    assert queue.ingest.call_args.kwargs["plan"] == "text-plan"
    assert target.rollbacks == 1
    assert "error_detail" not in entry


def test_unparseable_tables_skip_the_full_import(fallback_queue, monkeypatch):
    queue = fallback_queue
    monkeypatch.setattr(filing_adapter_v3, "configuration_metadata",
                        Mock(side_effect=TableExtractionError("cash_flow: Unexpected instant")))
    queue.ingest.side_effect = lambda connection, **kwargs: result_for(queue, queue.accessions[0], text)
    run(queue)
    entry = saved(queue, queue.accessions[0])
    assert [call.kwargs["processing_set_key"] for call in queue.ingest.call_args_list] == ["filing-import-v3-text"]
    assert entry["result"]["tables_unavailable"] == {
        "stage": "processing metadata", "error_type": "TableExtractionError", "message": "cash_flow: Unexpected instant"}


def test_dry_run_reports_text_only_validation_for_review(fallback_queue):
    queue = fallback_queue
    accession = queue.accessions[0]

    def dry_run(**kwargs):
        if kwargs["stages"] != "text-stages":
            raise table_failure()
        return result_for(queue, accession, text)

    queue.dry_run.side_effect = dry_run
    run(queue, mode="dry-run")
    entry = saved(queue, accession)
    assert entry["status"] == "validated_review_required"
    assert entry["result"]["set_key"] == "filing-import-v3-text"
    assert queue.dry_run.call_args.kwargs["required_checks"] == text.REQUIRED_CHECKS


def test_infrastructure_errors_never_fall_back(fallback_queue):
    queue = fallback_queue
    error = ProcessingError("Ingestion failed during tables.")
    error.stage = "tables"
    error.__cause__ = psycopg.OperationalError("connection to server failed: password=hunter2")
    queue.ingest.side_effect = error
    with pytest.raises(ProcessingError, match="infrastructure"):
        run(queue)
    assert queue.ingest.call_count == 1
    assert saved(queue, queue.accessions[0])["status"] == "failed"
    assert "hunter2" not in queue.path.read_text()


def test_failed_fallback_keeps_both_failure_details(fallback_queue):
    queue = fallback_queue

    def ingest(connection, **kwargs):
        if kwargs["processing_set_key"] == "filing-import-v3":
            raise table_failure()
        failure = ProcessingError("Validation failed or contains unverified checks.")
        failure.stage, failure.failed_checks = "validation", ("numeric_values_compared",)
        raise failure

    queue.ingest.side_effect = ingest
    run(queue)
    entry = saved(queue, queue.accessions[0])
    assert entry["status"] == "failed"
    assert entry["error_stage"] == "Validation failed or contains unverified checks."
    assert entry["error_detail"] == {
        "stage": "validation", "error_type": "ProcessingError", "failed_checks": ["numeric_values_compared"],
        "message": "Validation failed or contains unverified checks.",
        "full_import": {"stage": "tables", "error_type": "ProcessingError", "cause_type": "TableExtractionError",
                        "message": TABLE_MESSAGE}}


def text_receipt(queue, accession, **changes):
    from sec_pipeline.parser_policy import policy_metadata
    manifest = queue.manifests[accession]
    by_role = {record["role"]: record for record in manifest["files"]}
    xml, html = by_role["extracted_xbrl_instance"], by_role["main_inline_xbrl_filing"]
    configuration = {"versions": dict(text.VERSIONS),
                     **policy_metadata(text.VERSIONS),
                     "source_hashes": {record["local_path"]: record["sha256"]
                                       for record in [manifest["index_document"], *manifest["files"]]},
                     **text.configuration_metadata(manifest, queue.root),
                     "tables_unavailable": {"stage": "tables", "error_type": "TableExtractionError"}}
    row = {"configuration": configuration, "status": "passed",
           "counts": {"facts": 1622, "tables": 0, "reports": 1, "chunks": 210}, "coverage": dict(text.COVERAGE),
           "validation": {"checks": {check: True for check in text.REQUIRED_CHECKS}},
           "fact_path": xml["local_path"], "fact_sha": xml["sha256"], "report_path": html["local_path"],
           "report_sha": html["sha256"], "fact_version": text.VERSIONS["facts"],
           "report_version": text.VERSIONS["reports"], "chunk_version": text.VERSIONS["chunks"],
           "table_document": None, "table_version": None}
    row.update(changes)
    return list(row.values())


def test_sealed_text_only_set_is_reused_without_parsing(fallback_queue):
    queue = fallback_queue
    accession = queue.accessions[0]
    target = Destination({(accession, "filing-import-v3-text"): text_receipt(queue, accession)})
    run(queue, connection=target)
    entry = saved(queue, accession)
    # A verified receipt is not an attempt, so the bounded run continues to the next filing.
    assert all(call.kwargs["manifest_path"].parent.name != accession for call in queue.ingest.call_args_list)
    assert entry["status"] == "ingested_review_required"
    assert entry["result"]["mode"] == "stored_receipt"
    assert entry["result"]["tables_unavailable"] == {"stage": "tables", "error_type": "TableExtractionError"}


@pytest.mark.parametrize("changes", [
    {"table_document": UUID(int=5)}, {"table_version": "tables-v3"}, {"status": "failed"},
    {"coverage": {**text.COVERAGE, "tables": "partial"}}, {"counts": {"facts": 1, "tables": 2, "reports": 1, "chunks": 1}},
    {"validation": {"checks": {check: True for check in text.REQUIRED_CHECKS[1:]}}},
    {"chunk_version": "paragraph-v0"}, {"report_sha": "0" * 64},
])
def test_text_only_receipts_must_match_their_sealed_scope(fallback_queue, changes):
    from sec_pipeline.parser_policy import policy_metadata
    queue = fallback_queue
    accession = queue.accessions[0]
    manifest = queue.manifests[accession]
    expected = {"versions": dict(text.VERSIONS),
                **policy_metadata(text.VERSIONS),
                "source_hashes": {record["local_path"]: record["sha256"]
                                  for record in [manifest["index_document"], *manifest["files"]]},
                **text.configuration_metadata(manifest, queue.root)}
    assert backfill._stored_text_set(Connection(text_receipt(queue, accession)), accession, text, expected) is not None
    assert backfill._stored_text_set(Connection(text_receipt(queue, accession, **changes)), accession, text, expected) is None


def test_failure_details_keep_pipeline_messages_and_omit_database_text():
    cause = psycopg.errors.CheckViolation("new row violates check constraint; password=hunter2")
    error = ProcessingError("Ingestion failed during tables.")
    error.stage = "tables"
    error.__cause__ = cause
    assert backfill._failure_detail(error, "ingest") == {
        "stage": "tables", "error_type": "ProcessingError", "cause_type": "CheckViolation", "sqlstate": "23514"}
    assert backfill._failure_detail(KeyError("secret"), "ingest") == {"stage": "ingest", "error_type": "KeyError"}


def test_text_profile_drops_only_table_checks():
    assert text.SET_KEY != "filing-import-v3" and text.VERSIONS == filing_adapter_v3.VERSIONS
    assert set(text.REQUIRED_CHECKS) == set(filing_adapter_v3.REQUIRED_CHECKS) - text.TABLE_CHECKS
    assert {"fact_inline_correspondence", "numeric_values_compared", "chunk_slices_and_coverage",
            "index_registrants_verified"} <= set(text.REQUIRED_CHECKS)
    assert backfill.text_fallback_profile("tables-v3") is text
    assert backfill.text_fallback_profile("tables-v4") is None


@pytest.mark.parametrize("manifest", [
    "source_manifest.json",  # Tracked Microsoft sample.
    "data/raw/sec/0000026172-26-000016/source_manifest.json",  # Statement selection fails; facts and text pass.
])
def test_archived_filings_pass_every_text_only_check(manifest):
    path = ROOT / manifest
    if not path.is_file():
        pytest.skip("Optional archived source unavailable.")
    outputs = {}
    context = DryRunContext(manifest=verify_filing(path, ROOT), data_root=ROOT,
        source_ids=SourceIds(UUID(int=1), UUID(int=2), UUID(int=3)),
        versions=text.VERSIONS, outputs=outputs, report_id=UUID(int=4))
    plan = text.get_dry_run_plan()
    with patch("psycopg.connect", side_effect=AssertionError("Database access forbidden")), \
            patch("socket.create_connection", side_effect=AssertionError("Network access forbidden")):
        for stage in ("facts", "tables", "report", "chunks"):
            outputs[stage] = getattr(plan.stages, stage)(context)
        validation = plan.stages.validate(context)
    assert outputs["tables"].items == () and outputs["facts"].items and outputs["chunks"].items
    assert set(validation.checks) == set(text.REQUIRED_CHECKS)
    assert all(value is True for value in validation.checks.values()), validation.checks


@pytest.fixture
def ingestion():
    runner = ingestion_fixtures.ProcessingIngestTests(methodName="runTest")
    runner.setUp()
    try:
        yield runner
    finally:
        runner.doCleanups()


def without_tables(runner):
    return replace(runner.plan, stages=replace(runner.plan.stages,
        tables=lambda conn, context: WriteResult(ParseResult(()), ())))


def test_absent_tables_publish_a_set_without_a_table_scope(ingestion):
    with patch("sec_pipeline.catalog.store_processing_set", return_value=UUID(int=900)) as store:
        ingestion.run_import(plan=without_tables(ingestion), processing_set_key="fixture-text",
                             coverage={"facts": "complete", "tables": "absent", "reports": "unknown", "chunks": "complete"})
    record = store.call_args.args[1]
    assert (record.table_source_document_id, record.table_extraction_version) == (None, None)
    assert record.fact_extraction_version == ingestion.versions["facts"] and record.chunking_version == "paragraph-v1"
    assert (ingestion.conn.commits, ingestion.conn.rollbacks) == (1, 0)


def test_absent_tables_cannot_hide_stored_tables(ingestion):
    with patch("sec_pipeline.catalog.store_processing_set") as store:
        with pytest.raises(ProcessingError, match="must not store statement tables"):
            ingestion.run_import(processing_set_key="fixture-text",
                                 coverage={"facts": "complete", "tables": "absent", "reports": "unknown", "chunks": "complete"})
    store.assert_not_called()
    ingestion.assert_rollback()


@pytest.mark.parametrize("stage", ["facts", "reports", "chunks"])
def test_only_tables_may_be_absent(ingestion, stage):
    coverage = {"facts": "complete", "tables": "absent", "reports": "unknown", "chunks": "complete", stage: "absent"}
    with pytest.raises(ProcessingError, match="only tables may be absent"):
        ingestion.run_import(plan=without_tables(ingestion), processing_set_key="fixture-text", coverage=coverage)


def test_validation_failures_name_their_checks(ingestion):
    ingestion.validation = replace(ingestion.validation, checks={"fixture_rows": True, "table_source_cells": False})
    with pytest.raises(ProcessingError) as caught:
        ingestion.run_import()
    assert caught.value.failed_checks == ("table_source_cells",)
    assert caught.value.stage == "validation"
