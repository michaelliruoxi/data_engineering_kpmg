"""Evidence failures must not silently commit a reader-selection change."""

from contextlib import contextmanager, nullcontext
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest


@pytest.fixture
def review(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    cli = importlib.import_module("review_section_upgrade")
    events = []
    filing, selected, previous = uuid4(), uuid4(), uuid4()

    @contextmanager
    def transaction():
        events.append("begin")
        try:
            yield
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    conn = SimpleNamespace(transaction=transaction,
                           execute=lambda *args: SimpleNamespace(fetchone=lambda: (filing,)))
    monkeypatch.setattr(cli, "connect", Mock(return_value=nullcontext(conn)))
    monkeypatch.setattr(cli, "explicit_database_environment", nullcontext)
    monkeypatch.setattr(cli, "ingestion_settings", lambda *args: {})
    monkeypatch.setattr(cli, "check_connection_target", lambda *args: None)
    monkeypatch.setattr(cli, "served_selection", lambda *args: ("source", previous))
    monkeypatch.setattr(cli, "audit_corrected_set", lambda *args: {"status": "passed"})
    monkeypatch.setattr(cli, "publish_corrected_set", lambda *args, **kw: events.append("publish") or ("source", selected))
    monkeypatch.setattr(cli, "rollback_corrected_set", lambda *args: events.append("restore") or ("source", previous))
    return SimpleNamespace(cli=cli, events=events, selected=selected, previous=previous)


def arguments(review, output, action="publish"):
    args = [action, "--accession", "0000000001-26-000001", "--processing-set-id", str(review.selected),
            "--env-file", "unused.env", "--database", "unused_test", "--output", str(output)]
    if action == "publish":
        args += ["--expected-family", "source", "--expected-set-id", str(review.previous)]
    return args


@pytest.mark.parametrize("action", ["publish", "rollback"])
def test_unwritable_output_fails_before_connection(review, tmp_path, action):
    output = tmp_path / "directory"
    output.mkdir()
    with pytest.raises(OSError):
        review.cli.main(arguments(review, output, action))
    review.cli.connect.assert_not_called()
    assert not review.events


@pytest.mark.parametrize("action", ["publish", "rollback"])
def test_evidence_flush_failure_rolls_back_selection(review, tmp_path, monkeypatch, action):
    def fail(*args):
        review.events.append("write_failed")
        raise OSError("injected disk failure")
    monkeypatch.setattr(review.cli, "_write_evidence", fail)
    with pytest.raises(OSError):
        review.cli.main(arguments(review, tmp_path / "result.json", action))
    assert review.events[-2:] == ["write_failed", "rollback"]


def test_success_writes_before_commit_and_records_commit(review, tmp_path, monkeypatch):
    original = review.cli._write_evidence
    def write(output, result):
        review.events.append("write_" + result["transaction_status"])
        original(output, result)
    monkeypatch.setattr(review.cli, "_write_evidence", write)
    output = tmp_path / "result.json"
    assert review.cli.main(arguments(review, output)) == 0
    assert review.events[-4:] == ["publish", "write_prepared", "commit", "write_committed"]
    assert json.loads(output.read_text())["transaction_status"] == "committed"


def test_post_commit_evidence_failure_reports_actual_outcome(review, tmp_path, monkeypatch, capsys):
    original = review.cli._write_evidence
    def write(output, result):
        if result["transaction_status"] == "committed":
            raise OSError("final evidence failure")
        original(output, result)
    monkeypatch.setattr(review.cli, "_write_evidence", write)
    output = tmp_path / "result.json"
    assert review.cli.main(arguments(review, output)) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "committed_evidence_incomplete"
    assert json.loads(output.read_text())["transaction_status"] == "prepared"
    assert review.events[-1] == "commit"


@pytest.mark.parametrize("action", ["publish", "rollback"])
def test_commit_error_records_unknown_outcome_without_confirmed_selection(review, tmp_path, monkeypatch, action):
    transactions = 0

    @contextmanager
    def transaction():
        nonlocal transactions
        transactions += 1
        review.events.append("begin")
        yield
        if transactions == 2:
            review.events.append("commit_error")
            raise ConnectionError("private connection details must not enter evidence")
        review.events.append("commit")

    conn = review.cli.connect.return_value.enter_result
    monkeypatch.setattr(conn, "transaction", transaction)
    output = tmp_path / "result.json"
    with pytest.raises(ConnectionError):
        review.cli.main(arguments(review, output, action))
    evidence = json.loads(output.read_text())
    assert evidence["status"] == "outcome_unknown"
    assert evidence["transaction_status"] == "unknown"
    assert "served_selection_after" not in evidence
    assert evidence["error_type"] == "ConnectionError"
    assert "private connection details" not in output.read_text()


def test_failure_before_evidence_does_not_leave_empty_reserved_file(review, tmp_path):
    review.cli.connect.side_effect = ConnectionError("private connection details")
    output = tmp_path / "result.json"
    with pytest.raises(ConnectionError):
        review.cli.main(arguments(review, output))
    assert not output.exists()
