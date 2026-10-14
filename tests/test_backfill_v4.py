"""Queue and receipt gates distinguish faithful source capture from semantic review."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sec_pipeline import backfill, backfill_v4, document_sets
from tests.test_backfill import Connection, queue


VERSIONS = {'facts':'facts-v1','tables':'tables-v4','reports':'report-v1','chunks':'paragraph-v1'}


def receipt():
    expected = {'versions':VERSIONS, 'document_set_manifest_sha256':'a'*64,
                'source_hashes':{'main.htm':'b'*64,'extra.htm':'c'*64,'data.xml':'d'*64,'data.xsd':'e'*64}}
    roles={'main.htm':'main_inline_xbrl_filing','extra.htm':'supplemental_inline_xbrl_document',
           'data.xml':'extracted_xbrl_instance','data.xsd':'xbrl_extension_schema'}
    expected['source_documents']={path:{'role':roles[path],'source_url':'https://www.sec.gov/Archives/'+path,
                                      'sha256':sha,'size_bytes':123} for path,sha in expected['source_hashes'].items()}
    counts = dict(facts=9,tables=3,reports=2,chunks=3,inline_documents=2,quality_issues=1,
                  review_required_cells=1,unaccounted_cells=0)
    row = [deepcopy(expected),deepcopy(VERSIONS),{'complete':True},{'period_header_consistency':False},
           'review_required','passed',counts,'a'*64,2,'filing-id','processing-set-id','document-set-id',
           {'facts':'complete','tables':'partial','reports':'unknown','chunks':'complete'}]
    return expected, row


def registered_documents(expected):
    return [(path,record['role'],record['source_url'],record['sha256'],record['size_bytes'])
            for path,record in expected['source_documents'].items()]


class ReceiptConnection(Connection):
    def __init__(self,row,documents):
        super().__init__(row)
        self.documents = documents

    def execute(self,query,params):
        self.queries.append((query,params))
        return SimpleNamespace(fetchone=lambda:self.row,fetchall=lambda:self.documents)


def test_review_receipt_retains_status_in_actual_destination():
    expected,row=receipt()
    conn=ReceiptConnection(row,registered_documents(expected))
    assert backfill_v4.stored_status(conn,'accession','filing-import-v4',expected,['complete']) == 'review_required'
    assert conn.transactions == 1


def test_reused_receipt_returns_current_full_source_and_review_metadata():
    expected,row=receipt()
    conn=ReceiptConnection(row,registered_documents(expected))
    result=backfill_v4.stored_receipt(conn,'accession','filing-import-v4',expected,['complete'])
    assert result['mode']=='stored_receipt'
    assert result['validation_status']=='review_required'
    assert result['source_validation_status']=='passed'
    assert result['validation']=={'passed':False,'required_checks':['complete'],'checks':{'complete':True}}
    assert result['versions']==VERSIONS and result['configuration']==expected
    assert result['source_hashes']==expected['source_hashes']
    assert result['counts']['quality_issues']==1 and result['coverage']['reports']=='unknown'
    assert result['processing_set_id']=='processing-set-id' and result['document_set_id']=='document-set-id'
    result['counts']['quality_issues']=99
    result['configuration']['source_hashes'].clear()
    assert row[6]['quality_issues']==1 and expected['source_hashes']


def test_no_issue_receipt_is_fully_passed_without_hiding_source_checks():
    expected,row=receipt()
    row[3]['period_header_consistency']=True
    row[4]='passed'
    row[6].update(quality_issues=0,review_required_cells=0)
    conn=ReceiptConnection(row,registered_documents(expected))
    result=backfill_v4.stored_receipt(conn,'accession','filing-import-v4',expected,['complete'])
    assert result['validation_status']=='passed' and result['validation']['passed'] is True
    assert result['source_checks']=={'complete':True}


@pytest.mark.parametrize('mutation',[
    lambda r:r[0]['source_hashes'].update({'extra.htm':'e'*64}),
    lambda r:r[2].clear(), lambda r:r[3].update(period_header_consistency=True),
    lambda r:r.__setitem__(4,'passed'), lambda r:r.__setitem__(5,'failed'),
    lambda r:r[6].update(unaccounted_cells=1), lambda r:r[6].update(reports=0),
    lambda r:r[6].update(review_required_cells=0), lambda r:r[6].update(quality_issues=-1),
    lambda r:r[3].update(other_check=False), lambda r:r.__setitem__(7,'e'*64),
    lambda r:r.__setitem__(8,1), lambda r:r[2].update(unverified=False),
    lambda r:r[6].update(unaccounted_cells=False), lambda r:r[12].update(reports='absent'),
])
def test_incomplete_stale_or_falsely_passed_receipt_is_not_reused(mutation):
    expected,row=receipt()
    mutation(row)
    conn=ReceiptConnection(row,registered_documents(expected))
    assert backfill_v4.stored_status(conn,'accession','filing-import-v4',expected,['complete']) is None


def test_supplemental_document_hash_must_match_registered_source():
    expected,row=receipt()
    conn=ReceiptConnection(row,[record for record in registered_documents(expected) if record[0]!='extra.htm'])
    assert backfill_v4.stored_status(conn,'accession','filing-import-v4',expected,['complete']) is None


@pytest.mark.parametrize('column,value',[(1,'other_role'),(2,'https://www.sec.gov/Archives/other.xsd'),(3,'f'*64),(4,456)])
def test_registered_schema_role_url_hash_and_size_must_all_match(column,value):
    expected,row=receipt()
    documents=registered_documents(expected)
    altered=list(documents[-1])
    assert altered[0]=='data.xsd'
    altered[column]=value
    documents[-1]=tuple(altered)
    conn=ReceiptConnection(row,documents)
    assert backfill_v4.stored_receipt(conn,'accession','filing-import-v4',expected,['complete']) is None


@pytest.fixture
def source_queue(queue, monkeypatch):
    for manifest in queue.manifests.values():
        for record in [manifest['index_document'],*manifest['files']]:
            record['size_bytes']=123
    def acquire(client,path,root):
        target=path.with_name('source_document_set_manifest.json')
        target.write_text('{}')
        return target
    downloader=Mock(side_effect=acquire)
    monkeypatch.setattr(document_sets,'download_document_set',downloader)
    monkeypatch.setattr(document_sets,'verify_document_set',lambda path,root:queue.manifests[path.parent.name])
    result={'validation_status':'review_required','source_validation_status':'passed',
            'validation':{'passed':False},'counts':{'facts':9,'quality_issues':1}}
    profile=SimpleNamespace(VERSIONS=VERSIONS,REQUIRED_SOURCE_CHECKS=('complete',),
        run_dry_run=Mock(return_value=result),run_ingestion=Mock(return_value=result),
        configuration_from_manifest=Mock(side_effect=lambda manifest,digest:{'versions':VERSIONS,
            'source_hashes':{r['local_path']:r['sha256'] for r in [manifest['index_document'],*manifest['files']]},
            'source_documents':{r['local_path']:{key:r[key] for key in ('role','source_url','sha256','size_bytes')}
                                for r in [manifest['index_document'],*manifest['files']]},
            'document_set_manifest_sha256':digest}))
    monkeypatch.setattr(backfill,'adapter_profile',lambda version:(profile,'filing-import-v4'))
    monkeypatch.setattr(backfill_v4,'stored_receipt',lambda *args:None)
    return SimpleNamespace(queue=queue,profile=profile,downloader=downloader)


def test_dry_run_review_is_not_labeled_fully_validated(source_queue):
    q,p=source_queue.queue,source_queue.profile
    backfill.process_queue(q.path,q.state,None,q.root,tables_version='tables-v4',limit=1,emit=lambda e:None)
    assert q.state['filings'][q.accessions[0]]['status']=='validated_review_required'
    assert q.state['last_run']['review_required_successes']==1
    assert q.state['last_run']['complete'] is False
    assert p.run_dry_run.call_args.kwargs['manifest_path'].name=='source_document_set_manifest.json'


def test_validated_only_ingests_review_status_with_full_runner(source_queue):
    q,p=source_queue.queue,source_queue.profile
    q.state['filings'][q.accessions[0]]['status']='validated_review_required'
    backfill.process_queue(q.path,q.state,None,q.root,tables_version='tables-v4',mode='ingest',
        connection=Connection(),validated_only=True,emit=lambda e:None)
    p.run_ingestion.assert_called_once()
    assert q.state['filings'][q.accessions[0]]['status']=='ingested_review_required'
    assert q.state['last_run']['verified_successes']==1
    assert q.state['last_run']['complete'] is False


def test_download_archives_complete_set_without_extracting_tables(source_queue):
    q,p=source_queue.queue,source_queue.profile
    backfill.process_queue(q.path,q.state,None,q.root,tables_version='tables-v4',mode='download',limit=1,emit=lambda e:None)
    source_queue.downloader.assert_called_once()
    p.configuration_from_manifest.assert_not_called()
    p.run_dry_run.assert_not_called()
    assert q.state['filings'][q.accessions[0]]['manifest'].endswith('source_document_set_manifest.json')


def test_download_upgrade_records_full_set_path_even_when_core_was_already_downloaded(source_queue):
    q,p=source_queue.queue,source_queue.profile
    first=q.state['filings'][q.accessions[0]]
    first.update(status='downloaded',manifest='old/source_manifest.json')
    backfill.process_queue(q.path,q.state,None,q.root,tables_version='tables-v4',mode='download',limit=1,emit=lambda e:None)
    assert Path(first['manifest']).name=='source_document_set_manifest.json'
    p.run_dry_run.assert_not_called()
    p.run_ingestion.assert_not_called()


def test_database_receipt_preserves_review_status_without_reimport(source_queue,monkeypatch):
    q,p=source_queue.queue,source_queue.profile
    monkeypatch.setattr(backfill_v4,'stored_receipt',lambda *args:verified_queue_receipt(args[1],args[3]))
    backfill.process_queue(q.path,q.state,None,q.root,tables_version='tables-v4',mode='ingest',
        connection=Connection(),emit=lambda e:None)
    assert all(item['status']=='ingested_review_required' for item in q.state['filings'].values())
    assert q.state['last_run']['complete'] is True
    assert q.state['last_run']['review_required_successes']==3
    p.run_ingestion.assert_not_called()


def verified_queue_receipt(accession,configuration):
    return {'mode':'stored_receipt','accession_number':accession,'versions':deepcopy(VERSIONS),
            'configuration':deepcopy(configuration),'source_hashes':deepcopy(configuration['source_hashes']),
            'document_set_manifest_sha256':configuration['document_set_manifest_sha256'],
            'validation_status':'review_required','source_validation_status':'passed',
            'counts':{'facts':9,'quality_issues':1},'validation':{'passed':False,'checks':{'complete':True}},
            'source_checks':{'complete':True},'semantic_checks':{'period_header_consistency':False}}


def test_reused_v4_receipt_replaces_stale_versions_counts_validation_and_manifest(source_queue,monkeypatch):
    q,p=source_queue.queue,source_queue.profile
    first=q.state['filings'][q.accessions[0]]
    first.update(status='ingested',manifest='old/source_manifest.json',result={
        'versions':{**VERSIONS,'tables':'tables-v1'},'counts':{'facts':99},
        'validation':{'passed':True},'source_hashes':{'old':'0'*64}})
    monkeypatch.setattr(backfill_v4,'stored_receipt',lambda *args:verified_queue_receipt(args[1],args[3]))
    backfill.process_queue(q.path,q.state,None,q.root,tables_version='tables-v4',mode='ingest',
        connection=Connection(),emit=lambda e:None)
    assert first['status']=='ingested_review_required'
    assert Path(first['manifest']).name=='source_document_set_manifest.json'
    assert first['result']['versions']==VERSIONS
    assert first['result']['counts']=={'facts':9,'quality_issues':1}
    assert first['result']['validation']['passed'] is False
    assert first['result']['mode']=='stored_receipt'
    assert 'old' not in first['result']['source_hashes']
    assert first['result']['configuration']['source_hashes']==first['result']['source_hashes']
    p.run_ingestion.assert_not_called()
