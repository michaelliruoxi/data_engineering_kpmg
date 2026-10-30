"""Segmented plan validation and metadata checks before transaction commit."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from sec_pipeline import filing_adapter as base
from sec_pipeline import filing_adapter_v2 as adapter
from sec_pipeline.processing import (
    DryRunContext, ParseResult, ProcessingError, SourceIds, check_ingestion_options,
)
from sec_pipeline.filing_sources import verify_filing
import test_processing_ingest as ingestion_fixtures


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def segmented_sample():
    path = ROOT / "data/raw/sec/0000016918-26-000040/source_manifest.json"
    if not path.is_file():
        pytest.skip("Optional archived segmented filing is unavailable.")
    manifest = verify_filing(path, ROOT)
    outputs = {}
    context = DryRunContext(
        manifest=manifest, data_root=ROOT, source_ids=SourceIds(UUID(int=1), UUID(int=2), UUID(int=3)),
        versions=dict(adapter.VERSIONS), outputs=outputs, report_id=UUID(int=4),
    )
    plan = adapter.get_dry_run_plan()
    with patch("psycopg.connect", side_effect=AssertionError("Database access forbidden")), \
            patch("socket.create_connection", side_effect=AssertionError("Network access forbidden")):
        for stage in ("facts", "tables", "report", "chunks"):
            outputs[stage] = getattr(plan.stages, stage)(context)
    return context


def test_profiles_have_distinct_versions_and_required_validation_without_mutating_v1():
    assert base.VERSIONS["tables"] == "tables-v1"
    assert adapter.VERSIONS == dict(base.VERSIONS, tables="tables-v2")
    dry, ingestion = adapter.get_dry_run_plan(), adapter.get_ingestion_plan()
    assert dry.required_checks == ingestion.required_checks == adapter.REQUIRED_CHECKS
    assert set(adapter.REQUIRED_CHECKS) == set(base.REQUIRED_CHECKS) | {
        "statement_segment_coverage", "index_registrants_verified",
    }
    assert ingestion.configuration_metadata is adapter.configuration_metadata
    assert base.get_ingestion_plan().configuration_metadata is None
    check_ingestion_options(ingestion, adapter.VERSIONS, None)


def test_cofilers_are_registered_only_after_source_proof_in_the_caller_transaction(segmented_sample):
    connection = object()
    metadata = {"index_registrants": [
        {"cik": "0000000042", "name": "Archive Owner", "is_canonical": True},
        {"cik": "0000000043", "name": "Reporting Parent", "is_canonical": False},
    ]}
    with patch.object(adapter, 'configuration_metadata', return_value=metadata) as verify, \
            patch.object(adapter, 'register_company') as register, \
            patch.object(base, 'write_facts', return_value='stored-facts') as write:
        assert adapter.write_facts(connection, segmented_sample) == 'stored-facts'
    verify.assert_called_once_with(segmented_sample.manifest, segmented_sample.data_root)
    assert [call.args[1].cik for call in register.call_args_list] == ['0000000042', '0000000043']
    assert all(call.args[0] is connection for call in register.call_args_list)
    write.assert_called_once_with(connection, segmented_sample, bulk=True)
    with patch.object(adapter, 'configuration_metadata', side_effect=ValueError('invalid source')), \
            patch.object(adapter, 'register_company') as register:
        with pytest.raises(ValueError, match='invalid source'):
            adapter.write_facts(connection, segmented_sample)
    register.assert_not_called()


def test_real_segmented_filing_requires_all_source_tables_and_index_registrants(segmented_sample):
    result = adapter.validate(segmented_sample)
    assert len(segmented_sample.outputs["tables"].items) == 4
    assert result.versions == adapter.VERSIONS
    assert set(result.checks) == set(adapter.REQUIRED_CHECKS)
    assert all(value is True for value in result.checks.values()), result.checks
    assert adapter.configuration_metadata(segmented_sample.manifest, ROOT)["index_registrants"]


def test_segmented_numeric_cell_cannot_be_hidden_by_claimed_inventory(segmented_sample):
    from collections import Counter
    tables = deepcopy(list(segmented_sample.outputs["tables"].items))
    cell = next(cell for row in tables[0].structure["rows"] for cell in row if cell["classification"] == 'resolved')
    cell.update(classification='formatting', fact_references=[], unresolved_reason=None)
    tables[0].structure['inventory'] = dict(Counter(cell['classification'] for row in tables[0].structure['rows'] for cell in row))
    context = replace(segmented_sample, outputs={**segmented_sample.outputs, 'tables': ParseResult(tables)})
    assert adapter.validate(context).checks['table_source_cells'] is False


@pytest.mark.parametrize("change", ["missing", "reordered", "group_metadata"])
def test_segment_tampering_cannot_pass_even_with_other_stages_present(segmented_sample, change):
    outputs = dict(segmented_sample.outputs)
    tables = deepcopy(list(outputs["tables"].items))
    if change == "missing":
        tables.pop()
    elif change == "reordered":
        tables[-1], tables[-2] = tables[-2], tables[-1]
    else:
        tables[-1].structure["statement_group"]["segment_index"] = 999
    outputs["tables"] = ParseResult(tables)
    validation = adapter.validate(replace(segmented_sample, outputs=outputs))
    assert validation.checks["statement_segment_coverage"] is False


def test_v2_rejects_v1_requested_versions_before_reading_source(segmented_sample, monkeypatch):
    validation = Mock(side_effect=AssertionError("Wrong profile reached base validation"))
    monkeypatch.setattr(base, "validate", validation)
    with pytest.raises(ProcessingError, match="requires tables-v2"):
        adapter.validate(replace(segmented_sample, versions=dict(base.VERSIONS)))
    validation.assert_not_called()


def test_empty_or_failed_index_metadata_cannot_pass(segmented_sample, monkeypatch):
    monkeypatch.setattr(adapter, "configuration_metadata", lambda manifest, root: {"index_registrants": []})
    assert adapter.validate(segmented_sample).checks["index_registrants_verified"] is False
    failure = ValueError("Index evidence does not match sources")
    metadata = Mock(side_effect=failure)
    monkeypatch.setattr(adapter, "configuration_metadata", metadata)
    with pytest.raises(ValueError) as caught:
        adapter.validate(segmented_sample)
    assert caught.value is failure


@pytest.fixture
def ingestion():
    runner = ingestion_fixtures.ProcessingIngestTests(methodName="runTest")
    runner.setUp()
    try:
        yield runner
    finally:
        runner.doCleanups()


def import_with_metadata(runner, metadata):
    return runner.run_import(
        plan=replace(runner.plan, configuration_metadata=metadata),
        processing_set_key="fixture-import-v2",
        coverage={key: "partial" for key in runner.versions},
    )


def test_verified_configuration_metadata_is_serialized_inside_the_import_transaction(ingestion):
    metadata = {"index_registrants": [{"cik": "0000789019", "name": "Microsoft Corporation", "is_canonical": True}]}
    calls = []

    def hook(manifest, root):
        assert ingestion.conn.commits == 0
        assert ingestion.conn.info.transaction_status.name == "INTRANS"
        assert dict(manifest) == ingestion.manifest
        assert root == ingestion.root.resolve()
        calls.append("metadata")
        ingestion.events.append("configuration_metadata")
        return metadata

    with patch("sec_pipeline.catalog.store_processing_set", return_value=UUID(int=900)) as store:
        result = import_with_metadata(ingestion, hook)
    assert calls == ["metadata"]
    record = store.call_args.args[1]
    assert record.configuration["index_registrants"] == metadata["index_registrants"]
    assert record.configuration["versions"] == ingestion.versions
    assert record.configuration["source_hashes"] == result["source_hashes"]
    assert ingestion.events[-3:] == ["validate", "configuration_metadata", "verify"]
    assert (ingestion.conn.commits, ingestion.conn.rollbacks) == (1, 0)
    assert result["processing_set_id"] == str(UUID(int=900))


def test_explicit_source_cell_counts_are_returned_without_changing_legacy_summaries(ingestion):
    counts = {"financial": 5, "linked": 3, "source_only_marker": 2, "hidden_nil": 1, "unresolved": 0}
    with patch("sec_pipeline.catalog.store_processing_set", return_value=UUID(int=900)) as store:
        result = import_with_metadata(ingestion, lambda manifest, root: {"source_cell_counts": counts})
    assert result["source_cell_counts"] == counts
    assert store.call_args.args[1].configuration["source_cell_counts"] == counts
    counts["source_only_marker"] = 999
    assert result["source_cell_counts"]["source_only_marker"] == 2


@pytest.mark.parametrize("metadata", [{"versions": {}}, {"source_hashes": {}}, {"parser_revisions": {}}, [], None])
def test_reserved_or_nonmapping_metadata_cannot_commit_or_publish_a_set(ingestion, metadata):
    with patch("sec_pipeline.catalog.store_processing_set") as store:
        with pytest.raises(ProcessingError, match="must not replace verified versions or source hashes"):
            import_with_metadata(ingestion, lambda manifest, root: metadata)
    store.assert_not_called()
    ingestion.assert_rollback()


def test_nonserializable_metadata_rolls_back_before_publication(ingestion):
    with patch("sec_pipeline.catalog.store_processing_set") as store:
        with pytest.raises(ProcessingError, match="during validation"):
            import_with_metadata(ingestion, lambda manifest, root: {"index_registrants": object()})
    store.assert_not_called()
    ingestion.assert_rollback()


def test_source_changed_by_metadata_callback_is_reverified_before_commit(ingestion):
    source = ingestion.root / "source-payload.htm"
    source.write_bytes(b"verified source bytes")

    def verify(path, root):
        if source.read_bytes() != b"verified source bytes":
            raise ValueError("Source hash changed")
        return json.loads(path.read_text())

    def metadata(manifest, root):
        source.write_bytes(b"changed after metadata verification")
        return {"index_registrants": [{"cik": "0000789019"}]}

    with patch("sec_pipeline.catalog.store_processing_set") as store:
        with pytest.raises(ProcessingError, match="Source verification failed before ingestion commit"):
            ingestion.run_import(
                plan=replace(ingestion.plan, configuration_metadata=metadata), verify_inputs=verify,
                processing_set_key="fixture-import-v2", coverage={key: "partial" for key in ingestion.versions},
            )
    store.assert_not_called()
    ingestion.assert_rollback()


def test_invalid_metadata_callback_is_rejected_before_any_stage(ingestion):
    with pytest.raises(ProcessingError, match="callable verifier"):
        ingestion.run_import(plan=replace(ingestion.plan, configuration_metadata={}))
    assert ingestion.events == []
    assert ingestion.conn.commits == ingestion.conn.rollbacks == 0
