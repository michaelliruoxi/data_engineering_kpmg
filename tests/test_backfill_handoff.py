"""Narrow terminal-handoff checks for fatal errors and ordinary filing failures."""
from unittest.mock import Mock
import json
import psycopg
import pytest
from sec_pipeline import backfill
from sec_pipeline.processing import ProcessingError
from tests.test_backfill import Connection, queue


@pytest.mark.parametrize('cause', [psycopg.errors.UndefinedTable('missing schema'),
                                 psycopg.errors.InsufficientPrivilege('wrong role')])
def test_infrastructure_failure_stops_and_saves_checkpoint(queue, cause):
    def fail(**kwargs):
        raise ProcessingError('Source processing failed.') from cause
    queue.dry_run.side_effect = fail
    with pytest.raises(ProcessingError, match='infrastructure error'):
        backfill.process_queue(queue.path, queue.state, None, queue.root, emit=lambda e: None)
    assert queue.dry_run.call_count == 1
    saved = json.loads(queue.path.read_text())
    assert saved['last_run']['stopped_reason'] == 'infrastructure_error'
    assert saved['last_run']['complete'] is False
    assert sum(e['status'] == 'pending' for e in saved['filings'].values()) == 2


def test_unsupported_filing_still_allows_remaining_filings(queue):
    queue.dry_run.side_effect = [ProcessingError('Unsupported statement layout.'),
                               {'counts': {}}, {'counts': {}}]
    backfill.process_queue(queue.path, queue.state, None, queue.root, emit=lambda e: None)
    assert queue.dry_run.call_count == 3
    assert [queue.state['filings'][key]['status'] for key in queue.accessions] == ['failed', 'validated', 'validated']


def test_lost_connection_stops_even_when_rollback_fails(queue):
    conn = Connection()
    conn.rollback = Mock(side_effect=psycopg.OperationalError('connection closed'))
    queue.verifier.side_effect = ValueError('source verification failed')
    with pytest.raises(ProcessingError, match='infrastructure error'):
        backfill.process_queue(queue.path, queue.state, None, queue.root, mode='ingest', connection=conn, emit=lambda e: None)
    assert queue.verifier.call_count == 1
    assert json.loads(queue.path.read_text())['last_run']['stopped_reason'] == 'infrastructure_error'
