"""Version-specific receipts and current-destination completion evidence."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sec_pipeline import backfill, filing_adapter, filing_adapter_v2, filing_adapter_v3
from tests.test_backfill import Connection, cli_queue, configuration, queue, selection_row


def result_for(queue, accession, profile):
    return {
        "accession_number": accession,
        "versions": dict(profile.VERSIONS),
        "source_hashes": configuration(queue.manifests[accession])["source_hashes"],
        "validation": {"passed": True, "required_checks": list(profile.REQUIRED_CHECKS),
                       "checks": {check: True for check in profile.REQUIRED_CHECKS}},
        "counts": {"facts": {"stored": 10}, "tables": {"stored": 4},
                   "reports": {"stored": 1}, "chunks": {"stored": 5}},
    }


@pytest.fixture
def profiled_queue(queue, monkeypatch):
    monkeypatch.setattr(filing_adapter_v2, "configuration_metadata", lambda manifest, root: {
        "index_registrants": [{"cik": manifest["cik"], "name": manifest["company"], "is_canonical": True}],
    })
    monkeypatch.setattr(filing_adapter_v2, "get_dry_run_plan", lambda: SimpleNamespace(
        stages=object(), required_checks=filing_adapter_v2.REQUIRED_CHECKS))
    monkeypatch.setattr(filing_adapter_v2, "get_ingestion_plan", lambda: object())
    monkeypatch.setattr(filing_adapter_v3, "configuration_metadata", lambda manifest, root: {
        **filing_adapter_v2.configuration_metadata(manifest, root),
        "source_value_policy": "source-marker-v1", "source_cell_counts": {"source_only_marker_cells": 1},
    })
    monkeypatch.setattr(filing_adapter_v3, "get_dry_run_plan", lambda: SimpleNamespace(
        stages=object(), required_checks=filing_adapter_v3.REQUIRED_CHECKS))
    monkeypatch.setattr(filing_adapter_v3, "get_ingestion_plan", lambda: object())

    def result(**kwargs):
        profile, _ = backfill.adapter_profile(kwargs["versions"]["tables"])
        return result_for(queue, kwargs["manifest_path"].parent.name, profile)

    queue.dry_run.side_effect = result
    queue.ingest.side_effect = lambda connection, **kwargs: result(**kwargs)
    return queue


def receipt(queue, accession, profile):
    row = selection_row(queue.manifests[accession])
    row[0] = {"versions": dict(profile.VERSIONS),
              "source_hashes": configuration(queue.manifests[accession])["source_hashes"]}
    if hasattr(profile, "configuration_metadata"):
        row[0].update(profile.configuration_metadata(queue.manifests[accession], queue.root))
    row[7:11] = [profile.VERSIONS[key] for key in ("facts", "tables", "reports", "chunks")]
    row[11] = {"checks": {check: True for check in profile.REQUIRED_CHECKS}}
    return row


class Destination(Connection):
    def __init__(self, receipts=None):
        super().__init__()
        self.receipts = receipts or {}

    def execute(self, query, params):
        self.row = self.receipts.get(params)
        return super().execute(query, params)


def test_profile_selection_keeps_v1_default_and_distinct_v2_set_identity():
    assert backfill.adapter_profile("tables-v1") == (filing_adapter, "filing-import-v1")
    assert backfill.adapter_profile("tables-v2") == (filing_adapter_v2, "filing-import-v2")
    assert backfill.adapter_profile("tables-v3") == (filing_adapter_v3, "filing-import-v3")
    with pytest.raises(ValueError, match="Unsupported"):
        backfill.adapter_profile("tables-unknown")


def test_download_does_not_require_statement_extraction(profiled_queue, monkeypatch):
    metadata = Mock(side_effect=ValueError("Unsupported statement layout"))
    monkeypatch.setattr(filing_adapter_v3, "configuration_metadata", metadata)
    queue = profiled_queue
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, mode="download",
        tables_version="tables-v3", limit=1, emit=lambda event: None) == 1
    assert queue.state["filings"][queue.accessions[0]]["status"] == "downloaded"
    metadata.assert_not_called()
    queue.dry_run.assert_not_called()
    queue.ingest.assert_not_called()


@pytest.mark.parametrize("tables_version", ["tables-v1", "tables-v2", "tables-v3"])
def test_bounded_new_destination_cannot_reuse_historical_complete_status(profiled_queue, tables_version):
    queue = profiled_queue
    for entry in queue.state["filings"].values():
        entry.update(status="ingested", destination="old-database")
    queue.state["last_run"] = {"complete": True, "destination": "old-database"}
    target = Destination()
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, mode="ingest",
        connection=target, destination="new-database", limit=1, tables_version=tables_version, emit=lambda event: None) == 1
    current = json.loads(queue.path.read_text())["last_run"]
    assert current["complete"] is False
    assert current["verified_successes"] == 1 and current["queue_size"] == 3
    assert current["destination"] == "new-database"
    assert current["versions"]["tables"] == tables_version
    assert queue.ingest.call_count == 1
    assert queue.ingest.call_args.kwargs["processing_set_key"] == "filing-import-" + tables_version.rsplit('-', 1)[1]


@pytest.mark.parametrize('check', ['source_only_markers_verified', 'hidden_nil_bindings_verified', 'financial_cell_inventory_complete'])
def test_v3_sealed_receipts_require_each_source_representation_check(profiled_queue, check):
    queue = profiled_queue
    accession = queue.accessions[0]
    row = receipt(queue, accession, filing_adapter_v3)
    expected = deepcopy(row[0])
    row[11]['checks'].pop(check)
    assert backfill._stored_set(Connection(row), accession, 'filing-import-v3', expected) is False


def test_changing_profile_cannot_count_cached_v1_results_as_v2_successes(profiled_queue):
    queue = profiled_queue
    for accession, entry in queue.state["filings"].items():
        entry.update(status="validated", result=result_for(queue, accession, filing_adapter))
    backfill.process_queue(queue.path, queue.state, None, queue.root, tables_version="tables-v2",
                           limit=1, emit=lambda event: None)
    assert queue.state["last_run"]["complete"] is False
    assert queue.state["last_run"]["verified_successes"] == 1
    assert queue.dry_run.call_count == 1
    assert queue.dry_run.call_args.kwargs["versions"] == filing_adapter_v2.VERSIONS


@pytest.mark.parametrize("missing", ["statement_segment_coverage", "index_registrants_verified"])
def test_v2_sealed_receipt_missing_required_profile_check_is_rejected(profiled_queue, missing):
    queue = profiled_queue
    accession = queue.accessions[0]
    row = receipt(queue, accession, filing_adapter_v2)
    expected = deepcopy(row[0])
    row[11]["checks"].pop(missing)
    assert backfill._stored_set(Connection(row), accession, "filing-import-v2", expected) is False


def test_v2_sealed_receipt_requires_current_verified_registrant_configuration(profiled_queue):
    queue = profiled_queue
    accession = queue.accessions[0]
    row = receipt(queue, accession, filing_adapter_v2)
    assert backfill._stored_set(Connection(row), accession, "filing-import-v2", deepcopy(row[0])) is True
    expected = deepcopy(row[0])
    row[0]["index_registrants"][0]["cik"] = "0000000001"
    assert backfill._stored_set(Connection(row), accession, "filing-import-v2", expected) is False


def test_matching_receipts_are_checked_in_current_destination_before_complete(profiled_queue):
    queue = profiled_queue
    for entry in queue.state["filings"].values():
        entry.update(status="ingested", destination="old-database")
    receipts = {(accession, "filing-import-v2"): receipt(queue, accession, filing_adapter_v2)
                for accession in queue.accessions}
    target = Destination(receipts)
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, mode="ingest",
        connection=target, destination="current-database", tables_version="tables-v2", limit=1,
        emit=lambda event: None) == 0
    assert queue.state["last_run"]["complete"] is True
    assert queue.state["last_run"]["verified_successes"] == 3
    assert len(target.queries) == 3 and queue.verifier.call_count == 3
    queue.ingest.assert_not_called()


def test_v1_database_receipt_never_skips_a_requested_v2_import(profiled_queue):
    queue = profiled_queue
    receipts = {(accession, "filing-import-v1"): receipt(queue, accession, filing_adapter)
                for accession in queue.accessions}
    target = Destination(receipts)
    backfill.process_queue(queue.path, queue.state, None, queue.root, mode="ingest", connection=target,
                           tables_version="tables-v2", limit=1, emit=lambda event: None)
    assert target.queries[0][1] == (queue.accessions[0], "filing-import-v2")
    assert queue.ingest.call_count == 1
    assert queue.state["last_run"]["complete"] is False


def test_retrying_only_failures_cannot_claim_other_entries_verified(profiled_queue):
    queue = profiled_queue
    for entry in queue.state["filings"].values():
        entry["status"] = "ingested"
    queue.state["filings"][queue.accessions[0]]["status"] = "failed"
    backfill.process_queue(queue.path, queue.state, None, queue.root, retry_failed_only=True,
                           tables_version="tables-v2", emit=lambda event: None)
    assert queue.state["last_run"]["verified_successes"] == 1
    assert queue.state["last_run"]["complete"] is False


def test_missing_offline_sources_cannot_reuse_previous_completion(profiled_queue):
    queue = profiled_queue
    for accession, entry in queue.state["filings"].items():
        entry["status"] = "ingested"
        (queue.root / "data/raw/sec" / accession / "source_manifest.json").unlink()
    queue.state["last_run"] = {"complete": True}
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, mode="ingest",
        connection=Destination(), tables_version="tables-v2", emit=lambda event: None) == 0
    assert queue.state["last_run"]["complete"] is False
    assert queue.state["last_run"]["verified_successes"] == 0
    queue.ingest.assert_not_called()


def test_empty_queue_does_not_claim_a_completed_import(profiled_queue):
    queue = profiled_queue
    queue.state["filings"] = {}
    backfill.process_queue(queue.path, queue.state, None, queue.root, emit=lambda event: None)
    assert queue.state["last_run"]["complete"] is False
    assert queue.state["last_run"]["queue_size"] == 0


def test_v2_cached_dry_run_needs_segment_check_even_if_overall_passed(profiled_queue):
    queue = profiled_queue
    accession = queue.accessions[0]
    old = result_for(queue, accession, filing_adapter_v2)
    old["validation"]["checks"].pop("statement_segment_coverage")
    queue.state["filings"][accession].update(status="validated", result=old)
    backfill.process_queue(queue.path, queue.state, None, queue.root, tables_version="tables-v2",
                           limit=1, emit=lambda event: None)
    queue.dry_run.assert_called_once()
    assert queue.state["last_run"]["complete"] is False


def test_cli_bounded_import_to_new_destination_returns_incomplete_despite_old_statuses(
        profiled_queue, cli_queue, monkeypatch, capsys):
    queue = profiled_queue
    for accession, entry in queue.state["filings"].items():
        entry.update(status="ingested", destination="old-database", result=result_for(queue, accession, filing_adapter))
    queue.state["companies"] = {f"{123456 + number:010d}": {"status": "discovered"} for number in range(490)}
    queue.state["last_run"] = {"complete": True, "destination": "old-database"}
    backfill.save_state(queue.path, queue.state)
    target = Destination()
    target.info = SimpleNamespace(host="127.0.0.1", hostaddr="127.0.0.1", port=5432, dbname="new_database")
    target.close = Mock()
    settings = {"host": "127.0.0.1", "hostaddr": "127.0.0.1", "port": 5432, "dbname": "new_database"}
    monkeypatch.setattr(cli_queue.cli, "ingestion_settings", lambda env, database: settings)
    connection = Mock(return_value=target)
    monkeypatch.setattr(cli_queue.cli, "connect", connection)
    code = cli_queue.cli.main(cli_queue.arguments + [
        "--ingest", "--offline", "--tables-version", "tables-v2", "--limit", "1",
        "--env-file", str(queue.root / "fixture.env"), "--database", "new_database",
    ])
    assert code == 2
    assert queue.ingest.call_count == 1
    assert queue.ingest.call_args.args[0] is target
    persisted = json.loads(queue.path.read_text())["last_run"]
    assert persisted["complete"] is False and persisted["verified_successes"] == 1
    assert persisted["destination"] == {"host": "127.0.0.1", "port": 5432, "dbname": "new_database"}
    target.close.assert_called_once()
    connection.assert_called_once()
    assert capsys.readouterr().err == ""
