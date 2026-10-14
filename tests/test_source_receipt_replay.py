"""Sealed source registration reuses URL identity and never rewrites receipts."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
from unittest.mock import patch

import psycopg
import pytest

from sec_pipeline import database as db
from tests.test_source_set_postgres import database_fixture, seal


ROOT=Path(__file__).resolve().parents[1]


def test_additive_guard_change_is_limited_to_canonical_source_insert_replay():
    old=(ROOT/'src/sec_pipeline/migrations/003_processing_sets.sql').read_text(encoding='utf-8')
    old=re.search(r'CREATE FUNCTION sec\.guard_processing_artifact\(\) RETURNS trigger.*?\n\$\$;',old,re.S).group()
    new=(ROOT/'src/sec_pipeline/migrations/009_source_receipt_replay.sql').read_text(encoding='utf-8')
    body=new[new.index('CREATE OR REPLACE FUNCTION'):].strip().replace('CREATE OR REPLACE FUNCTION','CREATE FUNCTION',1)
    begin=body.index("        IF TG_TABLE_NAME='source_documents' THEN",body.index("    IF TG_OP='INSERT' THEN"))
    end=body.index('        identity_predicate :=',begin)
    assert body[:begin]+body[end:]==old
    addition=body[begin:end]
    for field in ('filing_id','source_url','role','sha256','size_bytes'):
        assert 'x.'+field in addition
    assert 'UPDATE ' not in addition and 'x.id=' not in addition
    assert 'local_path' not in addition and 'retrieved_at' not in addition


@pytest.fixture
def local_connection():
    if os.environ.get('SEC_DB_TEST')!='1':pytest.skip('Dedicated local PostgreSQL checks are opt-in.')
    from tests.test_catalog_postgres import local_test_options
    conn=psycopg.connect(**local_test_options())
    try:
        with conn.transaction(force_rollback=True):
            version=conn.execute("SELECT pg_get_functiondef('sec.guard_processing_artifact()'::regprocedure)").fetchone()[0]
            if 'x.source_url=new_data' not in version:
                raise RuntimeError('Apply migration009 to the dedicated local test database first.')
            yield conn
    finally:
        conn.close()


def source_records(conn,filing_id):
    rows=conn.execute('''SELECT id,role,source_url,local_path,sha256,size_bytes,retrieved_at,title,document_type,storage_uri
        FROM sec.source_documents WHERE filing_id=%s ORDER BY role''',(filing_id,)).fetchall()
    return {row[1]:(row[0],db.SourceDocumentInput(filing_id=filing_id,role=row[1],source_url=row[2],local_path=row[3],
        sha256=row[4],size_bytes=row[5],retrieved_at=row[6],title=row[7],document_type=row[8],storage_uri=row[9])) for row in rows}


def snapshot_sources(conn,filing_id):
    return conn.execute('SELECT id,to_jsonb(s) FROM sec.source_documents s WHERE filing_id=%s ORDER BY id',(filing_id,)).fetchall()


def exercise_seed_replay(conn):
    """Run the real metadata seed path; only synthetic fixture byte checking is stubbed."""
    item=database_fixture(conn,review=False)
    conn.execute("UPDATE sec.filings SET reporting_period_end='2026-06-30' WHERE id=%s",(item.filing,))
    seal(conn,item)
    before=snapshot_sources(conn,item.filing)
    records=source_records(conn,item.filing)
    def entry(role):
        _,record=records[role]
        return {'role':record.role,'source_url':record.source_url,'local_path':record.local_path,
                'sha256':record.sha256,'size_bytes':record.size_bytes,'retrieved_at_utc':record.retrieved_at.isoformat()}
    manifest={'cik':'0000009908','company':'Document set fixture','accession_number':'0000009908-26-000001','form':'10-Q',
        'filing_date':'2026-08-01','reporting_period_end':'2026-06-30','filing_index_url':records['filing_index'][1].source_url,
        'index_document':entry('filing_index'),
        'files':[entry(role) for role in ('main_inline_xbrl_filing','extracted_xbrl_instance','xbrl_extension_schema')]}
    with patch('sec_pipeline.database._verified_manifest',return_value=manifest):
        first=db.seed_manifest(conn,Path('fixture/source_manifest.json'),Path('fixture'))
        second=db.seed_manifest(conn,Path('fixture/source_manifest.json'),Path('fixture'))
    assert first==second
    assert first['filing_id']==item.filing
    assert first['document_ids']=={record.local_path:identifier for role,(identifier,record) in records.items()
                                   if role!='supplemental_inline_xbrl_document'}
    # The seed supplies titles/document types different from the fixture's first
    # receipt. Identical bytes retain every original row, not just the UUID.
    assert snapshot_sources(conn,item.filing)==before
    return item,records,before


def test_postgres_full_metadata_seed_replays_after_v4_seal_without_changing_any_source(local_connection):
    exercise_seed_replay(local_connection)


def test_postgres_refetch_metadata_reuses_original_receipt_and_changed_identity_fails(local_connection):
    conn=local_connection
    item,records,before=exercise_seed_replay(conn)
    for identifier,record in records.values():
        replay=replace(record,retrieved_at=record.retrieved_at+timedelta(days=1),
            title='Alternative retrieval title',document_type='Alternative descriptor',storage_uri='https://example.invalid/archive')
        assert db.register_source_document(conn,replay)==identifier
    assert snapshot_sources(conn,item.filing)==before
    _,schema=records['xbrl_extension_schema']
    for changes in ({'sha256':'f'*64},{'role':'another_role'},{'size_bytes':schema.size_bytes+1},
                    {'source_url':schema.source_url+'different'}):
        with pytest.raises((psycopg.Error,db.DataConflict)),conn.transaction():
            db.register_source_document(conn,replace(schema,**changes))
    assert snapshot_sources(conn,item.filing)==before


def test_postgres_receipt_replay_does_not_enable_updates_or_deletes(local_connection):
    conn=local_connection
    item,records,before=exercise_seed_replay(conn)
    identifier,_=records['xbrl_extension_schema']
    for query in ("UPDATE sec.source_documents SET role='other' WHERE id=%s",
                  'UPDATE sec.source_documents SET retrieved_at=now() WHERE id=%s',
                  'DELETE FROM sec.source_documents WHERE id=%s'):
        with pytest.raises(psycopg.errors.CheckViolation),conn.transaction():
            conn.execute(query,(identifier,))
    assert snapshot_sources(conn,item.filing)==before
