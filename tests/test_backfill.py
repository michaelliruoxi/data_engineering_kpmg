"""Checkpoint and destination-aware resume behavior with fake transports and SQL."""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sec_pipeline import acquisition, backfill, filing_adapter, processing, universe


class Connection:
    def __init__(self, row=None):
        self.row = row
        self.queries = []
        self.rollbacks = 0
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield

    def execute(self, query, params):
        self.queries.append((query, params))
        return SimpleNamespace(fetchone=lambda: self.row)

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def queue(tmp_path, monkeypatch):
    path = tmp_path / "queue.json"
    filings, manifests = {}, {}
    for number in range(1, 4):
        accession = f"0000123456-26-{number:06d}"
        filing = {
            "cik": "0000123456", "company": "Example Company", "accession_number": accession,
            "form": "10-Q", "filing_date": f"2026-05-0{number}", "reporting_period_end": "2026-03-31",
            "primary_document": "report.htm",
            "filing_index_url": f"https://www.sec.gov/Archives/edgar/data/123456/{accession.replace('-', '')}/{accession}-index.html",
        }
        filings[accession] = {"filing": filing, "status": "pending", "attempts": 0}
        directory = tmp_path / "data" / "raw" / "sec" / accession
        directory.mkdir(parents=True)
        (directory / "source_manifest.json").write_text("{}", encoding="utf-8")
        records = [{"role": role, "local_path": f"data/raw/sec/{accession}/{name}", "sha256": str(index) * 64,
                    "source_url": filing["filing_index_url"].rsplit("/", 1)[0] + "/" + name}
                   for index, (role, name) in enumerate((
                       ("filing_index", "index.html"), ("main_inline_xbrl_filing", "report.htm"),
                       ("extracted_xbrl_instance", "report.xml"), ("xbrl_extension_schema", "report.xsd")), 1)]
        manifests[accession] = dict(filing, status="verified", index_document=records[0], files=records[1:])
    state = {"schema_version": 1, "configuration": {"since": "2026-01-01", "until": "2026-12-31"},
             "companies": {}, "filings": filings}
    backfill.save_state(path, state)
    verifier = Mock(side_effect=lambda manifest_path, root: manifests[manifest_path.parent.name])
    monkeypatch.setattr(backfill, "verify_filing", verifier)
    monkeypatch.setattr(filing_adapter, "get_dry_run_plan", lambda: SimpleNamespace(stages=object(), required_checks=("source",)))
    monkeypatch.setattr(filing_adapter, "get_ingestion_plan", lambda: object())
    result = {"counts": {"facts": {"stored": 10}, "tables": {"stored": 3}, "reports": {"stored": 1}, "chunks": {"stored": 5}}}
    dry_run, ingest = Mock(return_value=result), Mock(return_value=result)
    monkeypatch.setattr(processing, "run_dry_run", dry_run)
    monkeypatch.setattr(processing, "run_ingestion", ingest)
    return SimpleNamespace(path=path, root=tmp_path, state=state, manifests=manifests,
                           verifier=verifier, dry_run=dry_run, ingest=ingest,
                           accessions=sorted(filings, reverse=True))


def configuration(manifest):
    return {"versions": filing_adapter.VERSIONS,
            "source_hashes": {record["local_path"]: record["sha256"]
                              for record in [manifest["index_document"], *manifest["files"]]}}


def selection_row(manifest):
    by_role = {record["role"]: record for record in manifest["files"]}
    xml, html = by_role["extracted_xbrl_instance"], by_role["main_inline_xbrl_filing"]
    return [configuration(manifest), "passed",
            {"facts": 1622, "tables": 3, "reports": 1, "chunks": 210, "sections": 16, "table_fact_links": 283},
            xml["local_path"], xml["sha256"], html["local_path"], html["sha256"],
            *(filing_adapter.VERSIONS[key] for key in ("facts", "tables", "reports", "chunks")),
            {"checks": {check: True for check in filing_adapter.REQUIRED_CHECKS}}]


@pytest.fixture
def cli_queue(queue, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    cli = importlib.import_module("backfill_sec")
    source = ("<table id='constituents'><tr><th>Symbol</th><th>Security</th><th>CIK</th></tr>"
              + "".join(f"<tr><td>T{number}</td><td>Company {number}</td><td>{123456 + number}</td></tr>"
                        for number in range(490)) + "</table>").encode()
    snapshot_path = queue.root / "membership.json"
    snapshot_path.with_suffix(".html").write_bytes(source)
    snapshot_path.write_text(json.dumps(universe.snapshot(source, as_of="2026-10-13")), encoding="utf-8")
    queue.state["configuration"].update(
        universe_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(), forms=["10-K", "10-Q"])
    backfill.save_state(queue.path, queue.state)
    forbidden = Mock(side_effect=AssertionError("Unexpected network or database connection"))
    monkeypatch.setattr(cli, "SecClient", forbidden)
    monkeypatch.setattr(cli, "connect", forbidden)
    arguments = ["--universe", str(snapshot_path), "--state", str(queue.path),
                 "--since", "2026-01-01", "--until", "2026-12-31", "--data-root", str(queue.root)]
    return SimpleNamespace(cli=cli, queue=queue, arguments=arguments, forbidden=forbidden)


def test_checkpoint_reopens_only_identical_membership_and_window(tmp_path, monkeypatch):
    universe = {"companies": [{"cik": "0000123456", "tickers": ["EXAMPLE"]}]}
    monkeypatch.setattr(backfill, "load_snapshot", lambda path: universe)
    source, queue_path = tmp_path / "membership.json", tmp_path / "queue.json"
    source.write_text("first snapshot", encoding="utf-8")
    state, members = backfill.open_state(queue_path, source, "2026-01-01", "2026-12-31")
    state["companies"]["0000123456"] = {"status": "discovered"}
    backfill.save_state(queue_path, state)
    resumed, _ = backfill.open_state(queue_path, source, "2026-01-01", "2026-12-31")
    assert resumed == state
    assert members == universe
    with pytest.raises(ValueError, match="configuration changed"):
        backfill.open_state(queue_path, source, "2026-02-01", "2026-12-31")
    source.write_text("changed membership snapshot", encoding="utf-8")
    with pytest.raises(ValueError, match="configuration changed"):
        backfill.open_state(queue_path, source, "2026-01-01", "2026-12-31")


def test_queue_lock_excludes_concurrent_writer_and_releases_on_failure(tmp_path):
    queue_path = tmp_path / "queue.json"
    with pytest.raises(RuntimeError, match="fixture"):
        with backfill.queue_lock(queue_path):
            with pytest.raises(ValueError, match="locked"):
                with backfill.queue_lock(queue_path):
                    raise AssertionError("Second writer entered")
            raise RuntimeError("fixture")
    assert not Path(str(queue_path) + ".lock").exists()


def test_one_failure_preserves_other_successes_and_sanitizes_errors(queue):
    failed = queue.accessions[1]

    def run(**kwargs):
        if kwargs["manifest_path"].parent.name == failed:
            raise RuntimeError("postgresql://private-password@private-host/database")
        return {"counts": {"facts": {"extracted": 10}}}

    queue.dry_run.side_effect = run
    events = []
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, emit=events.append) == 3
    stored = json.loads(queue.path.read_text(encoding="utf-8"))
    assert stored["filings"][failed]["status"] == "failed"
    assert [stored["filings"][accession]["status"] for accession in queue.accessions if accession != failed] == ["validated", "validated"]
    assert all(stored["filings"][accession]["attempts"] == 1 for accession in queue.accessions)
    assert "private-password" not in queue.path.read_text(encoding="utf-8") + "".join(events)


def test_access_block_stops_new_requests_and_preserves_previous_download(queue, monkeypatch):
    for accession in queue.accessions:
        (queue.root / "data" / "raw" / "sec" / accession / "source_manifest.json").unlink()
    calls = []

    def download(client, filing, root):
        calls.append(filing["accession_number"])
        if len(calls) == 2:
            raise acquisition.AccessBlocked("SEC blocked access")
        path = root / "data" / "raw" / "sec" / filing["accession_number"] / "source_manifest.json"
        path.write_text("{}", encoding="utf-8")
        return path

    monkeypatch.setattr(acquisition, "download_filing", download)
    with pytest.raises(acquisition.AccessBlocked):
        backfill.process_queue(queue.path, queue.state, object(), queue.root, mode="download", emit=lambda event: None)
    assert calls == queue.accessions[:2]
    stored = json.loads(queue.path.read_text(encoding="utf-8"))["filings"]
    assert [stored[accession]["status"] for accession in queue.accessions] == ["downloaded", "access_blocked", "pending"]


def test_discovery_failure_is_checkpointed_and_other_companies_continue(tmp_path, monkeypatch):
    companies = [{"cik": f"{number:010d}", "tickers": [f"T{number}"]} for number in range(1, 4)]
    state = {"configuration": {"since": "2026-01-01", "until": "2026-12-31"}, "companies": {}, "filings": {}}
    path = tmp_path / "queue.json"
    discover = Mock(side_effect=[[], ValueError("private detail"), []])
    monkeypatch.setattr(acquisition, "discover_filings", discover)
    result = backfill.discover_queue(path, state, {"companies": companies}, object(), emit=lambda event: None)
    assert result["companies"] == {"discovered": 2, "failed": 1}
    assert json.loads(path.read_text(encoding="utf-8"))["companies"] == state["companies"]
    backfill.discover_queue(path, state, {"companies": companies}, object(), company_limit=0, emit=lambda event: None)
    assert discover.call_count == 3


def test_discovery_access_block_stops_remaining_companies(tmp_path, monkeypatch):
    companies = [{"cik": f"{number:010d}", "tickers": [f"T{number}"]} for number in range(1, 4)]
    state = {"configuration": {"since": "2026-01-01", "until": "2026-12-31"}, "companies": {}, "filings": {}}
    discover = Mock(side_effect=[[], acquisition.AccessBlocked("SEC blocked access"), []])
    monkeypatch.setattr(acquisition, "discover_filings", discover)
    path = tmp_path / "queue.json"
    with pytest.raises(acquisition.AccessBlocked):
        backfill.discover_queue(path, state, {"companies": companies}, object(), emit=lambda event: None)
    assert discover.call_count == 2
    assert json.loads(path.read_text(encoding="utf-8"))["companies"]["0000000002"]["status"] == "access_blocked"


@pytest.mark.parametrize("changed", ["accession_number", "cik", "form", "filing_date", "reporting_period_end"])
def test_existing_manifest_must_match_queued_filing_identity(queue, changed):
    accession = queue.accessions[0]
    manifest = queue.manifests[accession]
    replacements = {"accession_number": queue.accessions[1], "cik": "0000654321", "form": "10-K",
                    "filing_date": "2026-06-01", "reporting_period_end": "2025-12-31"}
    manifest[changed] = replacements[changed]
    backfill.process_queue(queue.path, queue.state, None, queue.root, limit=1, emit=lambda event: None)
    assert queue.state["filings"][accession]["status"] == "failed"
    queue.dry_run.assert_not_called()


def test_failed_download_counts_toward_limit(queue, monkeypatch):
    for accession in queue.accessions:
        (queue.root / "data" / "raw" / "sec" / accession / "source_manifest.json").unlink()
    download = Mock(side_effect=ValueError("unsupported filing"))
    monkeypatch.setattr(acquisition, "download_filing", download)
    attempted = backfill.process_queue(queue.path, queue.state, object(), queue.root, limit=1, emit=lambda event: None)
    assert attempted == 1
    assert download.call_count == 1
    assert queue.state["filings"][queue.accessions[0]]["attempts"] == 1
    assert queue.state["filings"][queue.accessions[1]]["status"] == "pending"


def test_failed_verification_counts_toward_limit(queue):
    queue.verifier.side_effect = ValueError("source bytes changed")
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, limit=1, emit=lambda event: None) == 1
    assert queue.verifier.call_count == 1
    assert queue.state["filings"][queue.accessions[1]]["status"] == "pending"
    queue.dry_run.assert_not_called()


def test_queue_success_does_not_skip_empty_actual_destination(queue):
    for entry in queue.state["filings"].values():
        entry.update(status="ingested", destination="previous-database")
    connection = Connection()
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, mode="ingest",
                                  connection=connection, destination="new-database", emit=lambda event: None) == 3
    assert queue.ingest.call_count == 3
    assert len(connection.queries) == 3
    assert all(call.args[0] is connection for call in queue.ingest.call_args_list)
    assert all(entry["destination"] == "new-database" for entry in queue.state["filings"].values())


def test_ingestion_failure_rolls_back_current_filing_and_continues(queue):
    queue.ingest.side_effect = [{"counts": {}}, RuntimeError("private password"), {"counts": {}}]
    connection = Connection()
    backfill.process_queue(queue.path, queue.state, None, queue.root, mode="ingest",
                           connection=connection, destination="target", emit=lambda event: None)
    assert connection.rollbacks == 1
    assert [queue.state["filings"][accession]["status"] for accession in queue.accessions] == ["ingested", "failed", "ingested"]


def test_verified_matching_sealed_selection_skips_rewrite_in_actual_connection(queue, monkeypatch):
    checks = []
    connection = Connection()

    def stored(conn, accession, set_key, expected):
        checks.append((conn, accession, set_key, expected))
        return True

    monkeypatch.setattr(backfill, "_stored_set", stored)
    attempted = backfill.process_queue(queue.path, queue.state, None, queue.root, mode="ingest",
        connection=connection, destination="actual-target", emit=lambda event: None)
    assert attempted == 0
    assert queue.verifier.call_count == 3
    queue.ingest.assert_not_called()
    assert len(checks) == 3
    for conn, accession, key, expected in checks:
        assert conn is connection
        assert key == "filing-import-v1"
        assert expected == configuration(queue.manifests[accession])
        assert queue.state["filings"][accession]["destination"] == "actual-target"


def test_replay_bypasses_completed_selection_shortcut(queue, monkeypatch):
    stored = Mock(return_value=True)
    monkeypatch.setattr(backfill, "_stored_set", stored)
    backfill.process_queue(queue.path, queue.state, None, queue.root, mode="ingest", replay=True,
        connection=Connection(), destination="actual-target", limit=1, emit=lambda event: None)
    stored.assert_not_called()
    assert queue.ingest.call_count == 1


def test_corrupt_local_source_is_checked_before_destination_skip(queue, monkeypatch):
    queue.verifier.side_effect = ValueError("hash changed")
    stored = Mock(return_value=True)
    monkeypatch.setattr(backfill, "_stored_set", stored)
    backfill.process_queue(queue.path, queue.state, None, queue.root, mode="ingest",
        connection=Connection(), limit=1, emit=lambda event: None)
    stored.assert_not_called()
    queue.ingest.assert_not_called()
    assert queue.state["filings"][queue.accessions[0]]["status"] == "failed"


def test_matching_database_selection_uses_flat_measured_counts(queue):
    accession = queue.accessions[0]
    row = selection_row(queue.manifests[accession])
    connection = Connection(row)
    assert backfill._stored_set(connection, accession, "filing-import-v1", row[0]) is True
    assert connection.transactions == 1
    query, params = connection.queries[0]
    assert params == (accession, "filing-import-v1")
    assert "sec.source_documents" in query and "r.source_document_id=td.id" in query


@pytest.mark.parametrize("change", ["missing", "configuration", "validation", "empty_stage", "source_hash",
                                    "scope_version", "missing_check", "false_check"])
def test_nonmatching_database_selection_cannot_skip(queue, change):
    accession = queue.accessions[0]
    expected = configuration(queue.manifests[accession])
    row = deepcopy(selection_row(queue.manifests[accession]))
    if change == "configuration":
        row[0]["versions"]["facts"] = "different-version"
    elif change == "validation":
        row[1] = "failed"
    elif change == "empty_stage":
        row[2]["tables"] = 0
    elif change == "source_hash":
        row[4] = "different source hash"
    elif change == "scope_version":
        row[7] = "different selected version"
    elif change == "missing_check":
        row[11]["checks"].pop(filing_adapter.REQUIRED_CHECKS[0])
    elif change == "false_check":
        row[11]["checks"][filing_adapter.REQUIRED_CHECKS[0]] = False
    row = None if change == "missing" else row
    assert backfill._stored_set(Connection(row), accession, "filing-import-v1", expected) is False


def test_cli_status_reads_checkpoint_while_writer_lock_exists(cli_queue, capsys):
    queue, cli = cli_queue.queue, cli_queue.cli
    lock = Path(str(queue.path) + ".lock")
    lock.write_text("fixture writer", encoding="utf-8")
    original = queue.path.read_bytes()
    assert cli.main(cli_queue.arguments + ["--status"]) == 0
    output = capsys.readouterr()
    summary = json.loads(output.out)
    assert summary["companies_expected"] == 490
    assert summary["filings"] == {"pending": 3}
    assert output.err == ""
    assert queue.path.read_bytes() == original
    assert lock.read_text(encoding="utf-8") == "fixture writer"
    cli_queue.forbidden.assert_not_called()
    queue.verifier.assert_not_called()


def test_cli_dry_run_still_respects_existing_writer_lock(cli_queue, capsys):
    queue = cli_queue.queue
    lock = Path(str(queue.path) + ".lock")
    lock.write_text("fixture writer", encoding="utf-8")
    original = queue.path.read_bytes()
    assert cli_queue.cli.main(cli_queue.arguments + ["--dry-run", "--offline"]) == 1
    assert "Backfill stopped (ValueError)" in capsys.readouterr().err
    assert queue.path.read_bytes() == original
    assert lock.read_text(encoding="utf-8") == "fixture writer"
    queue.verifier.assert_not_called()


@pytest.mark.parametrize("accession_key", ["../outside", "not-an-accession", "0000123456-26-999999"])
def test_cli_rejects_noncanonical_or_mismatched_accession_before_source_access(cli_queue, capsys, accession_key):
    queue = cli_queue.queue
    entry = queue.state["filings"][queue.accessions[0]]
    queue.state["filings"] = {accession_key: entry}
    backfill.save_state(queue.path, queue.state)
    original = queue.path.read_bytes()
    assert cli_queue.cli.main(cli_queue.arguments + ["--dry-run", "--offline"]) == 1
    assert "Backfill stopped (ValueError)" in capsys.readouterr().err
    assert queue.path.read_bytes() == original
    assert not Path(str(queue.path) + ".lock").exists()
    queue.verifier.assert_not_called()
    queue.dry_run.assert_not_called()


def test_cli_limit_counts_failed_source_validation_and_preserves_pending_work(cli_queue, capsys):
    queue = cli_queue.queue
    queue.verifier.side_effect = ValueError("fixture source hash mismatch")
    assert cli_queue.cli.main(cli_queue.arguments + ["--dry-run", "--offline", "--limit", "1"]) == 2
    queue.verifier.assert_called_once()
    queue.dry_run.assert_not_called()
    persisted = json.loads(queue.path.read_text(encoding="utf-8"))["filings"]
    assert [persisted[accession]["status"] for accession in queue.accessions] == ["failed", "pending", "pending"]
    assert [persisted[accession]["attempts"] for accession in queue.accessions] == [1, 0, 0]
    assert not Path(str(queue.path) + ".lock").exists()
    cli_queue.forbidden.assert_not_called()
    assert capsys.readouterr().err == ""
