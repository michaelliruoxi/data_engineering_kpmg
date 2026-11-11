"""Regression cases from the independent November operational review."""
from contextlib import nullcontext
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sec_pipeline import backfill
from sec_pipeline.backfill_status import receipt_status
from sec_pipeline.database import DatabaseError
from sec_pipeline.filing_ingestion_v5 import MigrationPrerequisiteError, preflight
from sec_pipeline.processing import ParserRevisionConflict
from sec_pipeline.shortlist_table_validation import restrict_additional_inputs, restrict_inventory
from sec_pipeline.universe import scoped_snapshot, load_snapshot
from tests.test_backfill import Connection, queue
from tests.test_backfill_status import receipt
from tests.test_shortlist_table_validation import scope_files
from sec_pipeline.shortlist_table_validation import load_scope


def test_stale_receipts_keep_import_history_and_do_not_starve_pending(queue, monkeypatch):
    stale = queue.accessions[:2]
    for accession in stale:
        queue.state['filings'][accession].update(status='ingested', result={'historical': True})
    original = deepcopy(queue.state['filings'])
    def ingest(conn, **kwargs):
        if kwargs['manifest_path'].parent.name in stale:
            raise ParserRevisionConflict('explicit versioned upgrade required')
        return {'validation_status': 'passed'}
    queue.ingest.side_effect = ingest
    monkeypatch.setattr(backfill, '_stored_set', lambda *args: False)
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, mode='ingest',
        connection=Connection(), limit=1, emit=lambda e: None) == 1
    for accession in stale:
        entry = queue.state['filings'][accession]
        assert entry['status'] == 'ingested'
        assert entry['receipt_status'] == 'stale_receipt'
        assert entry['result'] == original[accession]['result']
        assert entry['attempts'] == original[accession]['attempts']
    assert queue.state['filings'][queue.accessions[2]]['status'] == 'ingested'
    assert queue.state['last_run']['stale_receipts'] == 2
    assert queue.state['last_run']['complete'] is False


def test_schema_preflight_stops_before_changing_any_filing(queue, monkeypatch):
    original = deepcopy(queue.state['filings'])
    profile, key = backfill.adapter_profile('tables-v1')
    mock = Mock(side_effect=MigrationPrerequisiteError('migration missing'))
    monkeypatch.setattr(profile, 'preflight', mock, raising=False)
    with pytest.raises(MigrationPrerequisiteError):
        backfill.process_queue(queue.path, queue.state, None, queue.root, mode='ingest',
                               connection=Connection(), emit=lambda e: None)
    assert queue.state['filings'] == original
    assert queue.state['last_run']['stopped_reason'] == 'infrastructure_error'
    assert mock.call_count == 1
    queue.ingest.assert_not_called()


def test_missing_corrected_migration_is_global_prerequisite():
    conn = SimpleNamespace(autocommit=False, closed=False,
        info=SimpleNamespace(transaction_status=SimpleNamespace(name='IDLE')),
        transaction=nullcontext, execute=lambda *args: SimpleNamespace(fetchone=lambda: None))
    with pytest.raises(MigrationPrerequisiteError):
        preflight(conn)


def test_unchanged_receipt_skips_write_queue_only_at_run_boundaries(queue, monkeypatch):
    for entry in queue.state['filings'].values():
        entry.update(status='ingested', destination=None,
                     processing_versions=dict(backfill.adapter_profile('tables-v1')[0].VERSIONS))
    monkeypatch.setattr(backfill, '_stored_set', lambda *args: True)
    writer = Mock(wraps=backfill.save_state)
    monkeypatch.setattr(backfill, 'save_state', writer)
    backfill.process_queue(queue.path, queue.state, None, queue.root, mode='ingest',
                           connection=Connection(), emit=lambda e: None)
    assert writer.call_count == 2
    assert queue.state['last_run']['complete'] is True


def test_scoped_universe_loads_ten_issuers_without_sp500_claim(tmp_path):
    snapshot = scoped_snapshot([{'ticker': f'T{i}', 'cik': str(i)} for i in range(1, 11)], as_of='2026-11-10')
    target = tmp_path / 'scope.json'
    target.write_text(json.dumps(snapshot))
    assert load_snapshot(target) == snapshot
    state, universe = backfill.open_state(tmp_path/'queue.json', target, '2021-01-01', '2026-11-10')
    assert backfill.summarize(state, universe)['companies_expected'] == 10
    snapshot['universe'] = 'current-sp500'
    target.write_text(json.dumps(snapshot))
    with pytest.raises((ValueError, FileNotFoundError)):
        load_snapshot(target)


@pytest.mark.parametrize('change', ['duplicate_cik', 'duplicate_ticker', 'unconfirmed', 'count'])
def test_invalid_scoped_universe_rejected(tmp_path, change):
    value=scoped_snapshot([{'ticker':'A','cik':'1'}, {'ticker':'B','cik':'2'}], as_of='2026-11-10')
    if change=='duplicate_cik': value['companies'][1]['cik']=value['companies'][0]['cik']
    elif change=='duplicate_ticker': value['companies'][1]['tickers']=['A']
    elif change=='unconfirmed': value['scope_status']='draft'
    else: value['company_count']=3
    target=tmp_path/'scope.json'; target.write_text(json.dumps(value))
    with pytest.raises(ValueError): load_snapshot(target)


def test_clean_import_status_preserves_unknown_semantic_coverage():
    r=receipt(); r['coverage'].update(tables='partial', reports='unknown')
    assert receipt_status(r)=='fully_imported'
    assert r['coverage']['reports']=='unknown'
    r['counts']['unaccounted_cells']=1
    assert receipt_status(r)=='review_required'


@pytest.mark.parametrize('ticker', [None, 'aapl', 'OLD'])
def test_scoped_inventory_uses_cik_and_retains_display_ticker(tmp_path, ticker):
    scope=load_scope(*scope_files(tmp_path))
    inventory={'filings':[{'id':'1', 'accession_number':'0000000001-26-000001',
        'cik':'320193', 'ticker':ticker, 'form':'10-Q', 'filing_date':'2026-10-28'}]}
    result=restrict_inventory(inventory, scope, {'AAPL'})
    assert len(result['filings'])==1
    assert result['filings'][0]['ticker']=='AAPL'
    assert result['filings'][0]['database_display_ticker']==ticker


def test_out_of_scope_date_cannot_hide_confirmed_accession_conflict(tmp_path):
    scope=load_scope(*scope_files(tmp_path))
    filing=dict(scope['filings'][0], filing_date='2020-01-01')
    with pytest.raises(DatabaseError, match='conflicts'):
        restrict_additional_inputs([('supplemental', {'filings':[filing]})], scope, scope['companies'])


def acquire_module():
    path=Path(__file__).parents[1]/'scripts/acquire_section_validation.py'
    spec=importlib.util.spec_from_file_location('review_acquire', path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_atomic_download_interruption_does_not_publish_partial_file(tmp_path, monkeypatch):
    module=acquire_module()
    target=tmp_path/'filing.htm'
    original=Path.replace
    def fail(self, other):
        if other==target: raise OSError('interrupted before publication')
        return original(self, other)
    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError): module.atomic_bytes(target, b'full source bytes')
    assert not target.exists()
    assert list(tmp_path.iterdir())==[]

from tests.test_backfill import cli_queue
from tests.test_backfill_progress import successful_result
from tests.test_acquire_section_validation import harness, acquisition as acquire_test_module


def test_fully_processed_ten_company_scope_exits_zero(cli_queue, capsys):
    import hashlib
    q=cli_queue.queue
    universe_path=Path(cli_queue.arguments[1])
    companies=[{'cik':str(123456+i), 'ticker':f'T{i}'} for i in range(10)]
    value=scoped_snapshot(companies, as_of='2026-11-10')
    universe_path.write_text(json.dumps(value))
    q.state['configuration']['universe_sha256']=hashlib.sha256(universe_path.read_bytes()).hexdigest()
    q.state['companies']={c['cik']:{'status':'discovered'} for c in value['companies']}
    for accession in q.accessions:
        q.state['filings'][accession].update(status='validated', result=successful_result(q, accession))
    backfill.save_state(q.path, q.state)
    assert cli_queue.cli.main(cli_queue.arguments+['--dry-run','--offline'])==0
    q.dry_run.assert_not_called()
    assert capsys.readouterr().err==''


def test_source_receipt_recovers_crash_after_bytes_before_manifest(harness, monkeypatch):
    original=acquire_test_module.atomic_bytes
    interrupted=[]
    def publish_then_interrupt(path, data):
        original(path,data)
        if path.suffix=='.htm' and not interrupted:
            interrupted.append(str(path))
            raise RuntimeError('simulated interrupt after publish')
    monkeypatch.setattr(acquire_test_module, 'atomic_bytes', publish_then_interrupt)
    with pytest.raises(RuntimeError): acquire_test_module.acquire(harness.args)
    seen=len(harness.requests)
    result=acquire_test_module.acquire(harness.args)
    assert result['coverage_status']=='acquired'
    assert len(harness.requests[seen:])==1  # Only the other, still missing source.
    assert all(row.get('sha256') for row in result['filings'])


def test_scoped_validation_without_git_keeps_code_hash_evidence(tmp_path, monkeypatch):
    from sec_pipeline import shortlist_table_validation as scoped
    import subprocess
    config,audit=scope_files(tmp_path)
    capture=tmp_path/'capture';capture.mkdir()
    (capture/'shared-inventory.json').write_text(json.dumps({'filings':[]}))
    def unavailable(*args, **kwargs): raise subprocess.CalledProcessError(128, 'git')
    monkeypatch.setattr(scoped.subprocess, 'check_output', unavailable)
    output=tmp_path/'out'
    assert scoped.run_scoped_validation(config,audit,capture,[tmp_path],output)==2
    metadata=json.loads((output/'run_metadata.json').read_text())
    assert metadata['run_started_from_git_head'] is None
    assert metadata['code_sha256']
    assert (output/'validation_report.md').is_file()
