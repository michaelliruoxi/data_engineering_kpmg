from copy import deepcopy
import importlib
from pathlib import Path
from types import SimpleNamespace
import pytest
from sec_pipeline.capacity import database_limit_bytes, database_at_limit, GIB
from sec_pipeline import backfill
from tests.test_backfill import Connection, queue


@pytest.mark.parametrize('limit',[1,7,14,25,100])
@pytest.mark.parametrize('delta,blocked',[(-1,False),(0,True),(1,True)])
def test_capacity_boundary(limit,delta,blocked):
    ceiling=database_limit_bytes(limit)
    assert ceiling==limit*GIB
    assert database_at_limit(ceiling+delta,ceiling) is blocked


@pytest.mark.parametrize('value',[0,-1,True,1.5,'14'])
def test_invalid_capacity(value):
    with pytest.raises(ValueError): database_limit_bytes(value)


def test_preflight_parses_default_and_custom_without_any_db(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'scripts'))
    preflight=importlib.import_module('check_backfill_ready')
    assert preflight.parse_args([]).max_database_gib==14
    assert preflight.parse_args(['--max-database-gib','25']).max_database_gib==25
    with pytest.raises(SystemExit): preflight.parse_args(['--max-database-gib','0'])


def test_launcher_passes_same_value_to_preflight_and_ingestion():
    text=(Path(__file__).parents[1]/'scripts/start_backfill.ps1').read_text()
    assert "'scripts\\check_backfill_ready.py' '--max-database-gib' \"$MaxDatabaseGiB\"" in text
    assert "'--max-database-gib', \"$MaxDatabaseGiB\"" in text
    assert '[int]$MaxDatabaseGiB = 14' in text


@pytest.mark.parametrize('limit',[7,25])
@pytest.mark.parametrize('delta',[-1,0,1])
def test_ingestion_guard_at_custom_boundary_preserves_unprocessed_queue(queue,monkeypatch,limit,delta):
    ceiling=database_limit_bytes(limit)
    class SizedConnection(Connection):
        def execute(self,query,params=None):
            assert query=='SELECT pg_database_size(current_database())'
            return SimpleNamespace(fetchone=lambda:(ceiling+delta,))
    monkeypatch.setattr(backfill,'_stored_set',lambda *args:False)
    before=deepcopy(queue.state['filings'])
    processed=backfill.process_queue(queue.path,queue.state,None,queue.root,mode='ingest',
                                    connection=SizedConnection(),max_database_bytes=ceiling,limit=1,emit=lambda e:None)
    if delta>=0:
        assert processed==0
        queue.ingest.assert_not_called()
        assert queue.state['filings']==before
        assert queue.state['last_run']['stopped_reason']=='database_size_limit'
    else:
        assert processed==1
        queue.ingest.assert_called_once()
    assert queue.state['last_run']['max_database_bytes']==ceiling
