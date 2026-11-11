"""Batch publication drives the real single-filing review command, filing by filing.

Only the database layer below that command is replaced: its evidence files,
exit codes, transactions and exceptions are the real ones.
"""

from contextlib import contextmanager, nullcontext
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest

from sec_pipeline.database import DataConflict


DESTINATION = {'host': 'database.example.com', 'port': 5432, 'dbname': 'sec_filings'}
V6 = {'facts': 'facts-v1', 'tables': 'tables-v6', 'reports': 'report-v3', 'chunks': 'paragraph-v2'}
V3 = V6 | {'tables': 'tables-v3', 'reports': 'report-v1', 'chunks': 'paragraph-v1'}
FIRST, SECOND, THIRD = '0000000001-26-000001', '0000000001-26-000002', '0000000001-26-000003'


def entry(status='ingested', versions=V6, *, set_id=True, destination=DESTINATION, **extra):
    result = {'processing_set_id': str(uuid4())} if set_id else {}
    return {'filing': {}, 'status': status, 'processing_versions': dict(versions), 'result': result,
            'destination': dict(destination), **extra}


@pytest.fixture
def batch(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    review = importlib.import_module('review_section_upgrade')
    cli = importlib.import_module('publish_reviewed_queue')
    filings = {
        FIRST: entry(), SECOND: entry(), THIRD: entry(),
        '0000000001-26-000004': entry('ingested_review_required'),
        '0000000001-26-000005': entry(versions=V3),
        '0000000001-26-000006': entry('pending'),
        '0000000001-26-000007': entry(receipt_status='stale_receipt'),
        '0000000001-26-000008': entry(destination=DESTINATION | {'dbname': 'sec_rehearsal'}),
        '0000000001-26-000009': entry(set_id=False),
        '0000000001-26-000010': entry('failed'),
    }
    state_path = tmp_path / 'queue.json'
    state_path.write_text(json.dumps({'filings': filings}))
    filing_ids = {accession: uuid4() for accession in filings}
    accessions = {identifier: accession for accession, identifier in filing_ids.items()}
    legacy = uuid4()
    world = SimpleNamespace(
        served={FIRST: ('legacy', legacy), SECOND: (None, None), THIRD: ('source', uuid4())},
        audits={}, history={}, calls=[], lose_commit_for=None, unreachable_for=None, current=None)

    class Connection:
        def __init__(self, changing=False):
            self.changing = changing

        def execute(self, statement, parameters=None):
            if statement.startswith('SELECT id FROM sec.filings'):
                world.current = parameters[0]
                return SimpleNamespace(fetchone=lambda: (filing_ids[parameters[0]],))
            return SimpleNamespace(fetchone=lambda: None)

        @contextmanager
        def transaction(self):
            yield
            if world.lose_commit_for == world.current and world.calls and world.calls[-1][0] in {'publish', 'rollback'} \
                    and world.calls[-1][1] == world.current and not getattr(self, 'lost', False):
                self.lost = True
                raise psycopg.OperationalError('server closed the connection unexpectedly')

    def connect(*args, **kwargs):
        if world.unreachable_for is not None and world.unreachable_for == len(world.calls):
            raise psycopg.OperationalError('could not connect: password=never-recorded')
        return nullcontext(Connection())

    def audit(conn, accession, set_id, data_root):
        world.calls.append(('audit', accession))
        return dict(world.audits.get(accession, {'status': 'passed', 'errors': []}))

    def publish(conn, filing_id, set_id, *, expected_current):
        accession = accessions[filing_id]
        world.calls.append(('publish', accession))
        current = world.served[accession]
        if current == ('source', set_id):
            return current
        if current != tuple(expected_current):
            raise DataConflict('Serving selection changed since review; publication was not applied.')
        world.history[accession] = current
        world.served[accession] = ('source', set_id)
        return world.served[accession]

    def restore(conn, filing_id, set_id):
        accession = accessions[filing_id]
        world.calls.append(('rollback', accession))
        if world.served[accession] != ('source', set_id):
            raise DataConflict('The requested set is not the current reader selection.')
        world.served[accession] = world.history.pop(accession)
        return world.served[accession]

    monkeypatch.setattr(review, 'connect', connect)
    monkeypatch.setattr(review, 'explicit_database_environment', nullcontext)
    monkeypatch.setattr(review, 'ingestion_settings', lambda *args: {})
    monkeypatch.setattr(review, 'check_connection_target', lambda *args: None)
    monkeypatch.setattr(review, 'served_selection', lambda conn, filing_id: world.served[accessions[filing_id]])
    monkeypatch.setattr(review, 'audit_corrected_set', audit)
    monkeypatch.setattr(review, 'publish_corrected_set', publish)
    monkeypatch.setattr(review, 'rollback_corrected_set', restore)
    monkeypatch.setattr(cli, 'ingestion_settings',
                        lambda env_file, database: DESTINATION | {'dbname': database, 'password': 'never-recorded'})
    set_ids = {accession: record['result'].get('processing_set_id') for accession, record in filings.items()}
    target = ['--env-file', 'unused.env', '--database', 'sec_filings']

    def run(action, directory, *options):
        return cli.main([action, *target, '--output-dir', str(tmp_path / directory), *options])

    def plan(directory='plan', *options):
        code = run('plan', directory, '--state', str(state_path), *options)
        path = tmp_path / directory / 'plan.json'
        return code, json.loads(path.read_text()) if path.exists() else None, path

    def publish_plan(path, directory='publish', *options, digest=None):
        code = run('publish', directory, '--plan', str(path),
                   '--plan-sha256', digest or hashlib.sha256(path.read_bytes()).hexdigest(), *options)
        summary = tmp_path / directory / 'publish-summary.json'
        return code, json.loads(summary.read_text()) if summary.exists() else None, summary

    return SimpleNamespace(cli=cli, world=world, state_path=state_path, set_ids=set_ids, legacy=legacy,
                           tmp=tmp_path, run=run, plan=plan, publish=publish_plan)


def by_accession(document):
    return {row['accession_number']: row for row in document['filings']}


def test_plan_selects_only_current_reviewed_receipts_in_this_database(batch, capsys):
    before = batch.state_path.read_bytes()
    batch.world.served[THIRD] = ('source', UUID(batch.set_ids[THIRD]))
    code, plan, path = batch.plan()
    assert code == 0 and batch.state_path.read_bytes() == before
    assert plan['skipped_queue_entries'] == {
        'ingested_into_another_database': 1, 'no_processing_set_id': 1, 'not_a_reviewed_profile': 1,
        'not_ingested': 2, 'review_required': 1, 'stale_receipt': 1}
    rows = by_accession(plan)
    assert set(rows) == {FIRST, SECOND, THIRD}
    assert rows[FIRST]['served_selection_before'] == ['legacy', str(batch.legacy)] and rows[FIRST]['action'] == 'publish'
    assert rows[SECOND]['served_selection_before'] == [None, None] and rows[SECOND]['action'] == 'publish'
    assert rows[THIRD]['action'] == 'already_published' and rows[THIRD]['currently_served'] is True
    assert plan['counts'] == {'already_published': 1, 'publish': 2} and plan['complete'] is True
    assert plan['destination'] == DESTINATION and 'never-recorded' not in path.read_text()
    assert plan['queue']['sha256'] == hashlib.sha256(before).hexdigest()
    for row in rows.values():
        assert json.loads((path.parent / row['evidence']).read_text())['status'] == 'passed'
    assert [call for call in batch.world.calls if call[0] != 'audit'] == []


def test_plan_prints_the_digest_that_publication_requires(batch, capsys):
    code, plan, path = batch.plan()
    output = capsys.readouterr().out
    summary = json.loads(output[output.rindex('{\n  "plan"'):])
    assert summary['plan_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert summary['counts'] == plan['counts']


def test_failed_audit_is_never_planned_for_publication(batch):
    batch.world.audits[SECOND] = {'status': 'failed', 'errors': [
        {'check': 'parser_revision_mismatch'}, {'check': 'stored_report_audit_failed', 'issues': []}]}
    batch.world.served[THIRD] = ('source', UUID(batch.set_ids[THIRD]))
    batch.world.audits[THIRD] = {'status': 'failed', 'errors': [{'check': 'original_source_unavailable'}]}
    code, plan, _ = batch.plan()
    rows = by_accession(plan)
    assert code == 2
    assert rows[SECOND]['validation'] == 'failed' and rows[SECOND]['action'] == 'none'
    assert rows[SECOND]['failed_checks'] == ['parser_revision_mismatch', 'stored_report_audit_failed']
    assert rows[THIRD]['action'] == 'none' and plan['served_sets_failing_audit'] == [THIRD]
    assert plan['counts'] == {'failed': 2, 'publish': 1}


def test_plan_limit_and_accession_filters_are_recorded(batch):
    code, plan, _ = batch.plan('limited', '--limit', '1')
    assert code == 0 and [row['accession_number'] for row in plan['filings']] == [FIRST]
    assert plan['deferred_by_limit'] == 2 and plan['complete'] is False
    code, plan, _ = batch.plan('one', '--accession', SECOND)
    assert code == 0 and list(by_accession(plan)) == [SECOND] and plan['complete'] is True
    code, plan, _ = batch.plan('absent', '--accession', '0000000009-26-000009')
    assert code == 1 and plan is None


def test_plan_stops_on_a_lost_connection_and_keeps_what_it_verified(batch, capsys):
    batch.world.unreachable_for = 1
    code, plan, _ = batch.plan()
    assert code == 3 and plan['complete'] is False and plan['stopped_reason'] == 'OperationalError'
    assert [row['accession_number'] for row in plan['filings']] == [FIRST]
    captured = capsys.readouterr()
    assert 'never-recorded' not in captured.out + captured.err


def test_publish_requires_the_reviewed_digest_and_the_same_database(batch, capsys):
    _, _, path = batch.plan()
    code, summary, _ = batch.publish(path, digest='0' * 64)
    assert code == 1 and summary is None
    code = batch.cli.main(['publish', '--env-file', 'unused.env', '--database', 'sec_rehearsal',
                           '--output-dir', str(batch.tmp / 'elsewhere'), '--plan', str(path),
                           '--plan-sha256', hashlib.sha256(path.read_bytes()).hexdigest()])
    assert code == 1
    assert not [call for call in batch.world.calls if call[0] == 'publish']
    assert 'different receiving database' in capsys.readouterr().err


def test_publish_switches_each_planned_filing_against_its_recorded_selection(batch):
    _, _, path = batch.plan()
    code, summary, summary_path = batch.publish(path)
    assert code == 0 and summary['counts'] == {'published': 3} and summary['complete'] is True
    for accession in (FIRST, SECOND, THIRD):
        assert batch.world.served[accession] == ('source', UUID(batch.set_ids[accession]))
    rows = by_accession(summary)
    assert rows[FIRST]['served_selection_before'] == ['legacy', str(batch.legacy)]
    assert rows[SECOND]['served_selection_before'] == [None, None]
    evidence = json.loads((summary_path.parent / rows[FIRST]['evidence']).read_text())
    assert evidence['transaction_status'] == 'committed'
    assert summary['external_retrieval_index_status'] == 'not_refreshed'
    assert summary['plan']['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    # Publishing the same plan again is a no-op for filings already switched.
    code, again, _ = batch.publish(path, 'publish-again')
    assert code == 0 and again['counts'] == {'published': 3}
    assert batch.world.history[FIRST] == ('legacy', batch.legacy)


def test_selection_changed_after_the_plan_is_not_published_and_others_continue(batch):
    _, _, path = batch.plan()
    batch.world.served[FIRST] = ('legacy', uuid4())
    code, summary, _ = batch.publish(path)
    rows = by_accession(summary)
    assert code == 2 and rows[FIRST]['outcome'] == 'selection_conflict' and rows[SECOND]['outcome'] == 'published'
    assert batch.world.served[FIRST][0] == 'legacy'


def test_publication_revalidates_and_skips_a_set_that_no_longer_passes(batch):
    _, _, path = batch.plan()
    batch.world.audits[FIRST] = {'status': 'failed', 'errors': [{'check': 'original_source_unavailable'}]}
    code, summary, _ = batch.publish(path)
    assert code == 2 and by_accession(summary)[FIRST]['outcome'] == 'validation_failed'
    assert batch.world.served[FIRST] == ('legacy', batch.legacy)
    assert ('publish', FIRST) not in batch.world.calls


def test_unacknowledged_commit_stops_the_batch_without_claiming_an_outcome(batch):
    _, _, path = batch.plan()
    batch.world.lose_commit_for = FIRST
    code, summary, summary_path = batch.publish(path)
    rows = by_accession(summary)
    assert code == 3 and summary['complete'] is False and summary['stopped_reason'] == 'OperationalError'
    assert rows[FIRST]['outcome'] == 'outcome_unknown' and SECOND not in rows
    evidence = json.loads((summary_path.parent / rows[FIRST]['evidence']).read_text())
    assert evidence['status'] == 'outcome_unknown' and 'served_selection_after' not in evidence
    assert ('publish', SECOND) not in batch.world.calls
    assert batch.world.served[SECOND] == (None, None)


def test_publish_limit_defers_the_rest(batch):
    _, _, path = batch.plan()
    code, summary, _ = batch.publish(path, 'canary', '--limit', '1')
    assert code == 0 and summary['counts'] == {'published': 1}
    assert summary['deferred_by_limit'] == 2 and summary['complete'] is False
    assert batch.world.served[SECOND] == (None, None)


def test_rollback_restores_each_exact_earlier_selection(batch):
    earlier_source = batch.world.served[THIRD]
    _, _, path = batch.plan()
    _, _, published = batch.publish(path)
    code = batch.run('rollback', 'rollback', '--published', str(published))
    summary = json.loads((batch.tmp / 'rollback' / 'rollback-summary.json').read_text())
    assert code == 0 and summary['counts'] == {'rolled_back': 3}
    assert batch.world.served[THIRD] == earlier_source
    assert batch.world.served[FIRST] == ('legacy', batch.legacy) and batch.world.served[SECOND] == (None, None)
    assert by_accession(summary)[FIRST]['served_selection_after'] == ['legacy', str(batch.legacy)]
    # Nothing is served by those sets any more, so a second withdrawal changes nothing.
    code = batch.run('rollback', 'rollback-again', '--published', str(published))
    again = json.loads((batch.tmp / 'rollback-again' / 'rollback-summary.json').read_text())
    assert code == 2 and again['counts'] == {'not_current_or_baseline_changed': 3}
    assert batch.world.served[FIRST] == ('legacy', batch.legacy)


def test_rollback_can_withdraw_one_filing_and_rejects_one_it_did_not_publish(batch):
    _, _, path = batch.plan()
    _, _, published = batch.publish(path)
    assert batch.run('rollback', 'unknown', '--published', str(published),
                     '--accession', '0000000001-26-000004') == 1
    assert not [call for call in batch.world.calls if call[0] == 'rollback']
    assert batch.run('rollback', 'one', '--published', str(published), '--accession', SECOND) == 0
    assert batch.world.served[SECOND] == (None, None)
    assert batch.world.served[FIRST] == ('source', UUID(batch.set_ids[FIRST]))
    assert batch.world.served[THIRD] == ('source', UUID(batch.set_ids[THIRD]))


def test_a_run_never_reuses_a_directory_that_already_holds_its_record(batch):
    _, _, path = batch.plan()
    with pytest.raises(SystemExit) as stopped:
        batch.plan()
    assert stopped.value.code == 2
    batch.publish(path)
    with pytest.raises(SystemExit):
        batch.publish(path)


def test_nothing_to_publish_is_not_reported_as_success(batch):
    for accession in (FIRST, SECOND, THIRD):
        batch.world.audits[accession] = {'status': 'failed', 'errors': [{'check': 'processing_set_not_passed'}]}
    code, plan, path = batch.plan()
    assert code == 2 and plan['counts'] == {'failed': 3}
    code, summary, _ = batch.publish(path)
    assert code == 2 and summary['counts'] == {} and not batch.world.served[FIRST][0] == 'source'


def test_commit_confirmed_with_incomplete_evidence_stops_as_published_not_unknown(batch, monkeypatch):
    """Exit 3 from the review command means the commit was confirmed."""
    _, _, path = batch.plan()
    review = importlib.import_module('review_section_upgrade')
    original = review._write_evidence

    def fail_final_write(output, result):
        if result.get('transaction_status') == 'committed':
            raise OSError('disk full')
        original(output, result)

    monkeypatch.setattr(review, '_write_evidence', fail_final_write)
    code, summary, _ = batch.publish(path)
    rows = by_accession(summary)
    assert code == 3 and summary['stopped_reason'] == 'evidence_write_failed_after_commit'
    assert rows[FIRST]['outcome'] == 'published_evidence_incomplete' and SECOND not in rows
    assert batch.world.served[FIRST] == ('source', UUID(batch.set_ids[FIRST]))
    assert batch.world.served[SECOND] == (None, None)


def test_interrupt_before_commit_is_recorded_without_claiming_a_change(batch, monkeypatch):
    _, _, path = batch.plan()
    review = importlib.import_module('review_section_upgrade')

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(review, 'publish_corrected_set', interrupted)
    code, summary, _ = batch.publish(path)
    assert code == 3 and summary['stopped_reason'] == 'KeyboardInterrupt'
    assert [row['outcome'] for row in summary['filings']] == ['stopped_before_commit']
    assert batch.world.served[FIRST] == ('legacy', batch.legacy)


@pytest.mark.parametrize('change', [
    lambda plan: plan['filings'][0].update(accession_number='../../outside'),
    lambda plan: plan['filings'][0].update(processing_set_id='not-a-uuid'),
    lambda plan: plan['filings'][0].update(served_selection_before=['source', None]),
    lambda plan: plan['filings'][0].update(served_selection_before=['other', str(uuid4())]),
    lambda plan: plan.update(kind='reviewed_publication_summary'),
    lambda plan: plan.update(filings='none'),
])
def test_malformed_batch_file_publishes_nothing_even_with_its_own_digest(batch, change):
    _, plan, path = batch.plan()
    change(plan)
    altered = batch.tmp / 'altered-plan.json'
    altered.write_text(json.dumps(plan))
    code, summary, _ = batch.publish(altered)
    assert code == 1 and summary is None
    assert not [call for call in batch.world.calls if call[0] == 'publish']
    assert not (batch.tmp / 'outside.json').exists()
