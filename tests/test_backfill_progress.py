"""Bounded invocations advance through pending work instead of retry starvation."""

from copy import deepcopy

import pytest

from sec_pipeline import acquisition, backfill, filing_adapter
from tests.test_backfill import Connection, cli_queue, configuration, queue


def successful_result(queue, accession):
    return {
        'accession_number': accession,
        **deepcopy(configuration(queue.manifests[accession])),
        'validation': {'passed': True, 'required_checks': list(filing_adapter.REQUIRED_CHECKS),
                       'checks': {check: True for check in filing_adapter.REQUIRED_CHECKS}},
        'counts': {'facts': {'extracted': 10}, 'tables': {'extracted': 3},
                   'reports': {'extracted': 1}, 'chunks': {'extracted': 5}},
    }


@pytest.fixture
def progressing_queue(queue):
    queue.dry_run.side_effect = lambda **kwargs: successful_result(queue, kwargs['manifest_path'].parent.name)
    return queue


def test_repeated_bounded_dry_runs_advance_and_then_finish(progressing_queue):
    queue = progressing_queue
    for _ in queue.accessions:
        assert backfill.process_queue(queue.path, queue.state, None, queue.root, mode='dry-run',
                                      limit=1, emit=lambda event: None) == 1
    processed = [call.kwargs['manifest_path'].parent.name for call in queue.dry_run.call_args_list]
    assert processed == queue.accessions
    assert all(entry['status'] == 'validated' for entry in queue.state['filings'].values())
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, mode='dry-run',
                                  limit=1, emit=lambda event: None) == 0
    assert queue.dry_run.call_count == 3


def test_failed_only_retry_leaves_pending_and_successful_work_alone(progressing_queue):
    queue = progressing_queue
    failed, pending, done = queue.accessions
    queue.state['filings'][failed]['status'] = 'failed'
    queue.state['filings'][done]['status'] = 'ingested'
    before_pending = deepcopy(queue.state['filings'][pending])
    before_done = deepcopy(queue.state['filings'][done])
    assert backfill.process_queue(queue.path, queue.state, None, queue.root,
        retry_failed_only=True, emit=lambda event: None) == 1
    assert queue.dry_run.call_args.kwargs['manifest_path'].parent.name == failed
    assert queue.state['filings'][pending] == before_pending
    assert queue.state['filings'][done] == before_done


def test_validated_only_ingestion_filters_work_without_trusting_the_dry_run(queue):
    selected, pending, failed = queue.accessions
    queue.state['filings'][selected]['status'] = 'validated'
    queue.state['filings'][failed]['status'] = 'failed'
    untouched = {key: deepcopy(queue.state['filings'][key]) for key in (pending, failed)}
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, mode='ingest',
        connection=Connection(), validated_only=True, emit=lambda event: None) == 1
    assert queue.verifier.call_count == 1
    queue.ingest.assert_called_once()
    assert queue.ingest.call_args.kwargs['manifest_path'].parent.name == selected
    assert queue.state['filings'][selected]['status'] == 'ingested'
    assert all(queue.state['filings'][key] == entry for key, entry in untouched.items())
    assert queue.state['last_run']['complete'] is False


def test_database_size_limit_preserves_pending_entry_and_does_not_import(queue, monkeypatch):
    from types import SimpleNamespace
    class SizedConnection(Connection):
        def execute(self, query, params=None):
            assert query == 'SELECT pg_database_size(current_database())'
            return SimpleNamespace(fetchone=lambda: (1500,))
    monkeypatch.setattr(backfill, '_stored_set', lambda *args: False)
    before = deepcopy(queue.state['filings'])
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, mode='ingest',
        connection=SizedConnection(), max_database_bytes=1000, emit=lambda event: None) == 0
    queue.ingest.assert_not_called()
    assert queue.state['filings'] == before
    assert queue.state['last_run']['stopped_reason'] == 'database_size_limit'
    assert queue.state['last_run']['observed_database_bytes'] == 1500
    assert queue.state['last_run']['complete'] is False


def test_a_failed_latest_filing_does_not_starve_older_pending_filings(progressing_queue):
    queue = progressing_queue
    failed = queue.accessions[0]

    def parse(**kwargs):
        accession = kwargs['manifest_path'].parent.name
        if accession == failed:
            raise ValueError('Unsupported source layout')
        return successful_result(queue, accession)

    queue.dry_run.side_effect = parse
    for _ in queue.accessions:
        backfill.process_queue(queue.path, queue.state, None, queue.root,
                               limit=1, emit=lambda event: None)
    assert [call.kwargs['manifest_path'].parent.name for call in queue.dry_run.call_args_list] == queue.accessions
    assert queue.state['filings'][failed]['status'] == 'failed'
    assert [queue.state['filings'][accession]['status'] for accession in queue.accessions[1:]] == ['validated', 'validated']
    # Once pending work is exhausted, a subsequent invocation may retry failure.
    backfill.process_queue(queue.path, queue.state, None, queue.root, limit=1, emit=lambda event: None)
    assert queue.dry_run.call_args.kwargs['manifest_path'].parent.name == failed
    assert queue.state['filings'][failed]['attempts'] == 2


def test_ingestion_failure_does_not_starve_pending_filings(queue, monkeypatch):
    failed = queue.accessions[0]

    def ingest(connection, **kwargs):
        accession = kwargs['manifest_path'].parent.name
        if accession == failed:
            raise ValueError('Unsupported source layout')
        return successful_result(queue, accession)

    queue.ingest.side_effect = ingest
    monkeypatch.setattr(backfill, '_stored_set', lambda conn, accession, key, expected:
                        queue.state['filings'][accession]['status'] == 'ingested')
    connection = Connection()
    for _ in queue.accessions:
        backfill.process_queue(queue.path, queue.state, None, queue.root, mode='ingest',
                               connection=connection, limit=1, emit=lambda event: None)
    assert [call.kwargs['manifest_path'].parent.name for call in queue.ingest.call_args_list] == queue.accessions
    assert [queue.state['filings'][accession]['status'] for accession in queue.accessions] == ['failed', 'ingested', 'ingested']
    assert connection.rollbacks == 1


def test_discovery_failure_does_not_starve_unattempted_companies(tmp_path, monkeypatch):
    companies = [{'cik': f'{number:010d}', 'tickers': [f'T{number}']} for number in range(1, 4)]
    state = {'configuration': {'since': '2026-01-01', 'until': '2026-12-31'}, 'companies': {}, 'filings': {}}
    seen = []

    def discover(client, cik, since, until):
        seen.append(cik)
        if cik == companies[0]['cik']:
            raise ValueError('Temporary company discovery failure')
        return []

    monkeypatch.setattr(acquisition, 'discover_filings', discover)
    for _ in companies:
        backfill.discover_queue(tmp_path / 'queue.json', state, {'companies': companies}, object(),
                                company_limit=1, emit=lambda event: None)
    assert seen == [company['cik'] for company in companies]
    assert [state['companies'][company['cik']]['status'] for company in companies] == ['failed', 'discovered', 'discovered']


@pytest.mark.parametrize('invalid', ['versions', 'source_hashes', 'false_check', 'missing_check', 'passed'])
def test_cache_requires_current_versions_sources_and_all_successful_checks(progressing_queue, invalid):
    queue = progressing_queue
    accession = queue.accessions[0]
    result = successful_result(queue, accession)
    if invalid == 'versions':
        result['versions']['facts'] = 'older-version'
    elif invalid == 'source_hashes':
        result['source_hashes']['changed-source'] = '0' * 64
    elif invalid == 'false_check':
        result['validation']['checks'][filing_adapter.REQUIRED_CHECKS[0]] = False
    elif invalid == 'missing_check':
        result['validation']['checks'].pop(filing_adapter.REQUIRED_CHECKS[0])
    else:
        result['validation']['passed'] = False
    queue.state['filings'][accession].update(status='validated', result=result)
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, limit=1, emit=lambda event: None) == 1
    queue.dry_run.assert_called_once()
    assert queue.dry_run.call_args.kwargs['manifest_path'].parent.name == accession


def test_explicit_replay_revalidates_a_matching_dry_run_cache(progressing_queue):
    queue = progressing_queue
    accession = queue.accessions[0]
    queue.state['filings'][accession].update(status='validated', result=successful_result(queue, accession))
    backfill.process_queue(queue.path, queue.state, None, queue.root,
                           replay=True, limit=1, emit=lambda event: None)
    queue.dry_run.assert_called_once()
    assert queue.dry_run.call_args.kwargs['manifest_path'].parent.name == accession


def test_cached_validation_is_not_used_before_source_verification(progressing_queue):
    queue = progressing_queue
    accession = queue.accessions[0]
    queue.state['filings'][accession].update(status='validated', result=successful_result(queue, accession))
    queue.state['filings'][accession]['error_stage'] = 'Stale previous failure'
    queue.verifier.side_effect = ValueError('Archived source changed')
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, limit=1, emit=lambda event: None) == 1
    assert queue.state['filings'][accession]['status'] == 'failed'
    assert queue.state['filings'][accession]['error_stage'] == 'Backfill failed during source verification.'
    assert 'Archived source changed' not in queue.path.read_text()
    queue.dry_run.assert_not_called()


def test_dry_run_does_not_downgrade_ingested_status_when_using_valid_cache(progressing_queue):
    queue = progressing_queue
    for accession in queue.accessions:
        queue.state['filings'][accession].update(status='ingested', result=successful_result(queue, accession))
    assert backfill.process_queue(queue.path, queue.state, None, queue.root,
                                  limit=1, emit=lambda event: None) == 0
    assert all(entry['status'] == 'ingested' for entry in queue.state['filings'].values())
    queue.dry_run.assert_not_called()


def test_cli_dry_run_reports_complete_for_verified_ingested_caches(cli_queue, capsys):
    queue = cli_queue.queue
    for accession in queue.accessions:
        queue.state['filings'][accession].update(status='ingested', result=successful_result(queue, accession))
    queue.state['companies'] = {f'{123456 + number:010d}': {'status': 'discovered'} for number in range(490)}
    backfill.save_state(queue.path, queue.state)
    assert cli_queue.cli.main(cli_queue.arguments + ['--dry-run', '--offline', '--limit', '1']) == 0
    queue.dry_run.assert_not_called()
    assert capsys.readouterr().err == ''
