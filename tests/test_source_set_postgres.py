"""Opt-in V4 PostgreSQL acceptance on dedicated localhost test databases only."""

from dataclasses import replace
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import psycopg
from psycopg.types.json import Jsonb
import pytest

from sec_pipeline import database as db
from sec_pipeline.source_cells_v3 import fact_reference
from sec_pipeline.source_set_storage import (
    SOURCE_CHECKS, DocumentMemberInput, DocumentSetInput, FactSourceOccurrenceInput, ReportMembershipInput,
    V4ProcessingSetInput, store_document_set, store_source_occurrences, store_v4_processing_set,
)
from sec_pipeline.v4_table_cells import store_v4_table_cells
from tests.test_v4_table_cells import conflict_fixture


@pytest.fixture
def local_connection():
    if os.environ.get('SEC_DB_TEST')!='1':
        pytest.skip('Dedicated local PostgreSQL checks are opt-in.')
    from tests.test_catalog_postgres import local_test_options
    conn=psycopg.connect(**local_test_options())
    try:
        with conn.transaction(force_rollback=True):
            if conn.execute("SELECT to_regclass('sec.source_processing_sets')").fetchone()[0] is None:
                raise RuntimeError('Migrate the dedicated test database through migration008 first.')
            yield conn
    finally:
        conn.close()


def database_fixture(conn, *, review=True, with_occurrences=True, corrupt_core=False):
    """Reusable SQL-contract fixture; the caller supplies and owns its connection."""
    company=db.register_company(conn,db.CompanyInput(cik='0000009908',name='Document set fixture'))
    accession='0000009908-26-000001'
    archive='https://www.sec.gov/Archives/edgar/data/9908/000000990826000001/'
    filing=db.register_filing(conn,db.FilingInput(company_id=company,accession_number=accession,form='10-Q',
        filing_date=date(2026,8,1),source_url=archive+accession+'-index.html',company_name_as_filed='Document set fixture'))
    _,_,source=conflict_fixture(issue=review)
    secondary=b'<html><body>Supplemental inline source</body></html>'
    definitions=[('primary','main_inline_xbrl_filing','primary.htm',hashlib.sha256(source).hexdigest(),'10-Q'),
                 ('secondary','supplemental_inline_xbrl_document','secondary.htm',hashlib.sha256(secondary).hexdigest(),'EX-99.1'),
                 ('xml','extracted_xbrl_instance','primary_htm.xml','b'*64,'EX-101.INS'),
                 ('index','filing_index',accession+'-index.html','c'*64,None),
                 ('schema','xbrl_extension_schema','primary.xsd','e'*64,'EX-101.SCH')]
    sources={name:db.register_source_document(conn,db.SourceDocumentInput(filing_id=filing,role=role,source_url=archive+filename,
        local_path='v4-fixture/'+filename,sha256=digest,size_bytes=100,retrieved_at=datetime(2026,8,2,tzinfo=timezone.utc),document_type=doctype))
        for name,role,filename,digest,doctype in definitions}
    proof={'member_source_urls':[archive+'primary.htm',archive+'secondary.htm'],'inline_target':'',
           'excluded_inline_documents':[],'source_checks':{'index_membership_complete':True,'single_primary_target':True,'excluded_targets_separate':True}}
    document_input=DocumentSetInput(filing_id=filing,set_key='primary-inline-set',xml_document_id=sources['xml'],
        primary_html_document_id=sources['primary'],index_document_id=sources['index'],manifest_sha256='d'*64,evidence=proof)
    members=[DocumentMemberInput(source_document_id=sources[name],source_sha256=digest,sequence=index+1,
        document_type=doctype,is_primary=index==0,index_locator={'source_xpath':f'/html/body/table/tr[{index+2}]','source_sha256':'c'*64})
        for index,(name,_,_,digest,doctype) in enumerate(definitions[:2])]
    document_set=store_document_set(conn,document_input,members)
    table,fact,_=conflict_fixture(filing,sources['primary'],sources['xml'],issue=review)
    entity={'scheme':'http://www.sec.gov/CIK','identifier':'0000009908'}
    fact=replace(fact,source_reference=fact.source_reference|{'entity':entity})
    other_fact=replace(fact,occurrence_key='F2',concept_name='OtherSourceAmount',raw_value='1',numeric_value=Decimal('1'),
                       source_reference={'element_id':'F2','context_ref':'c2','unit_ref':'usd','entity':entity})
    source_facts=[fact,other_fact]
    raw_paths={'F1':'/html/body/table/tr/td[2]/ix:nonfraction','F2':'/html/body/ix:nonfraction'}
    executable_paths={'F1':"/html/body/table/tr/td[2]/*[name()='ix:nonfraction']",'F2':"/html/body/*[name()='ix:nonfraction']"}
    if review:
        review_cell=table.structure['rows'][0][1]
        column=review_cell['column_period']
        for index,name in enumerate(['NetCashProvidedByUsedInOperatingActivities','NetCashProvidedByUsedInInvestingActivities',
                                     'NetCashProvidedByUsedInFinancingActivities','CashAndCashEquivalentsAtCarryingValue']):
            identifier=f'F{index+3}'
            core=replace(fact,occurrence_key=identifier,concept_name=name,raw_value='100',numeric_value=Decimal('100'),
                period_kind='instant' if index==3 else 'duration',instant_date=date(2026,6,30) if index==3 else None,
                period_start=None if index==3 else date(2026,1,1),period_end=None if index==3 else date(2026,6,30),
                source_reference={'element_id':identifier,'context_ref':f'core{index}','unit_ref':'usd','entity':entity})
            source_facts.append(core)
            row=deepcopy(table.structure['rows'][0])
            label='Cash at end of period' if index==3 else name
            for cell in row:
                cell.update(row=index+1,cell_id=f'r{index+1:03d}c{cell["column"]:03d}',row_heading=label,
                            source_xpath=f'/html/body/table/tr[{index+2}]/td[{cell["column"]+1}]')
                if cell['column']==0:cell['display_text']=label
                else:
                    raw=f'/html/body/table/tr[{index+2}]/td[2]/ix:nonfraction'
                    raw_paths[identifier]=raw
                    executable_paths[identifier]=f"/html/body/table/tr[{index+2}]/td[2]/*[name()='ix:nonfraction']"
                    cell.update(classification='resolved',display_text='100',fact_references=[fact_reference(core)|{
                        'xml_id':identifier,'scope':table.structure['fact_scope'],'method':'inline_id','inline_document_id':str(sources['primary']),'inline_xpath':raw}])
                    for key in ('source_fact_references','quality_issue_keys','quality_issues'):cell.pop(key)
            table.structure['rows'].append(row)
            table.structure['grid'].append([cell['cell_id'] for cell in row])
        review_cell['quality_issues'][0]['evidence']={
            'html_document_id':str(sources['primary']),'html_sha256':table.structure['html_sha256'],'xml_sha256':'b'*64,
            'cell_xpath':review_cell['source_xpath'],'inline_xpath':raw_paths['F1'],'numeric_source_xpath':executable_paths['F1'],
            'inline_id':'F1','display_text':review_cell['display_text'],'row_heading':review_cell['row_heading'],
            'column_headings':review_cell['column_headings'],'source_correspondence':'exact_inline_xml',
            'core_coverage':{'flow_xml_ids':[['F3'],['F4'],['F5']],'closing_cash_xml_ids':['F6']}}
        if corrupt_core:
            review_cell['quality_issues'][0]['evidence']['core_coverage']['flow_xml_ids'][0]=['F2']
    fact_ids={value.occurrence_key:db.store_fact(conn,value) for value in source_facts}
    occurrence_inputs=[]
    for value in source_facts:
        name='secondary' if value.occurrence_key=='F2' else 'primary'
        digest=definitions[1 if name=='secondary' else 0][3]
        raw=raw_paths[value.occurrence_key]
        path=executable_paths[value.occurrence_key]
        occurrence_inputs.append(FactSourceOccurrenceInput(filing_id=filing,document_set_id=document_set,
            fact_id=fact_ids[value.occurrence_key],html_document_id=sources[name],inline_id=value.occurrence_key,source_xpath=path,
            evidence=fact_reference(value)|{'xml_id':value.occurrence_key,'inline_mapped_value':value.raw_value,'inline_is_nil':False,
                'html_sha256':digest,'xml_sha256':'b'*64,'inline_xpath':raw}))
    occurrence_values=store_source_occurrences(conn,occurrence_inputs) if with_occurrences else []
    occurrences=dict(zip(fact_ids,occurrence_values))
    reports=[]
    for name in ['primary','secondary']:
        text='Original '+name+' document text.'
        report=db.store_report(conn,db.ReportInput(filing_id=filing,source_document_id=sources[name],extraction_version='report-v1',full_text=text))
        db.store_chunk(conn,db.ChunkInput(filing_id=filing,report_id=report,chunking_version='paragraph-v1',chunk_index=0,
            text=text,offset_start=0,offset_end=len(text)))
        reports.append(ReportMembershipInput(report_id=report))
    table_id=db.store_financial_table(conn,table)
    cell_result=store_v4_table_cells(conn,table_id,table,fact_ids,occurrences,document_set) if with_occurrences else None
    selection=V4ProcessingSetInput(filing_id=filing,set_key='filing-import-v4',document_set_id=document_set,
        source_checks={key:True for key in SOURCE_CHECKS},semantic_checks={'period_header_consistency':not review},
        coverage={'facts':'complete','tables':'partial','reports':'complete','chunks':'complete'},
        configuration={'fixture':True,'source_hashes':{'v4-fixture/'+filename:digest for _,_,filename,digest,_ in definitions}})
    return SimpleNamespace(filing=filing,sources=sources,document_set=document_set,document_input=document_input,members=members,
        table=table,table_id=table_id,facts=source_facts,fact_ids=fact_ids,occurrences=occurrences,occurrence_inputs=occurrence_inputs,
        reports=reports,cell_result=cell_result,selection=selection)


def seal(conn,item,**changes):
    options={'table_ids':[item.table_id],'reports':item.reports,'issue_ids':list(item.cell_result.issue_ids.values())}
    return store_v4_processing_set(conn,item.selection,**(options|changes))


def test_postgres_review_set_exposes_quality_preserves_facts_and_replays_original_ids(local_connection):
    conn=local_connection
    item=database_fixture(conn)
    set_id=seal(conn,item)
    row=conn.execute('SELECT validation_status,source_validation_status,counts FROM sec.source_processing_sets WHERE id=%s',(set_id,)).fetchone()
    assert row[0:2]==('review_required','passed')
    assert row[2]['inline_documents']==2 and row[2]['facts']==6 and row[2]['review_required_cells']==1 and row[2]['quality_issues']==1
    assert conn.execute('SELECT count(*) FROM sec.source_report_catalog WHERE processing_set_id=%s',(set_id,)).fetchone()[0]==2
    assert conn.execute('SELECT count(*) FROM sec.analysis_ready_statement_cells WHERE processing_set_id=%s',(set_id,)).fetchone()[0]==0
    view=conn.execute("SELECT quality_issue_codes,review_source_facts FROM sec.source_statement_cells WHERE processing_set_id=%s AND resolution_status='review_required'",(set_id,)).fetchone()
    assert view[0]==['instant_context_under_duration_header'] and view[1][0]['instant_date']=='2026-06-30'
    assert conn.execute('SELECT count(*) FROM sec.cell_fact_links WHERE table_id=%s',(item.table_id,)).fetchone()[0]==4
    assert store_document_set(conn,item.document_input,item.members)==item.document_set
    assert store_source_occurrences(conn,item.occurrence_inputs)==list(item.occurrences.values())
    assert store_v4_table_cells(conn,item.table_id,item.table,item.fact_ids,item.occurrences,item.document_set)==item.cell_result
    assert seal(conn,item)==set_id
    conn.execute('SET CONSTRAINTS ALL IMMEDIATE')


def test_postgres_clean_set_has_analysis_rows_and_no_legacy_preference(local_connection):
    conn=local_connection
    item=database_fixture(conn,review=False)
    set_id=seal(conn,item)
    assert conn.execute('SELECT validation_status FROM sec.source_processing_sets WHERE id=%s',(set_id,)).fetchone()[0]=='passed'
    assert conn.execute('SELECT count(*) FROM sec.analysis_ready_statement_cells WHERE processing_set_id=%s',(set_id,)).fetchone()[0]==2
    assert conn.execute('SELECT count(*) FROM sec.preferred_processing_sets WHERE filing_id=%s',(item.filing,)).fetchone()[0]==0


def test_postgres_seal_rejects_missing_report_or_issue_and_semantic_false_pass(local_connection):
    conn=local_connection
    item=database_fixture(conn)
    for changes in ({'reports':item.reports[:1]},{'issue_ids':[]}):
        with pytest.raises(psycopg.errors.CheckViolation),conn.transaction():
            seal(conn,item,**changes)
    with pytest.raises(psycopg.errors.CheckViolation),conn.transaction():
        store_v4_processing_set(conn,replace(item.selection,semantic_checks={'period_header_consistency':True}),
            table_ids=[item.table_id],reports=item.reports,issue_ids=list(item.cell_result.issue_ids.values()))


def test_postgres_v4_seals_all_selected_artifacts_and_original_source_receipts(local_connection):
    conn=local_connection
    item=database_fixture(conn)
    set_id=seal(conn,item)
    actions=[
        ('UPDATE sec.source_documents SET retrieved_at=now() WHERE id=%s',(item.sources['primary'],)),
        ('UPDATE sec.source_documents SET retrieved_at=now() WHERE id=%s',(item.sources['schema'],)),
        ("UPDATE sec.source_documents SET role='unrelated_source' WHERE id=%s",(item.sources['schema'],)),
        ("UPDATE sec.source_documents SET sha256=repeat('f',64) WHERE id=%s",(item.sources['schema'],)),
        ('UPDATE sec.financial_facts SET numeric_value=99 WHERE id=%s',(item.fact_ids['F1'],)),
        ("UPDATE sec.reports SET full_text='changed' WHERE id=%s",(item.reports[0].report_id,)),
        ("UPDATE sec.chunks SET text='changed' WHERE report_id=%s",(item.reports[0].report_id,)),
        ('DELETE FROM sec.source_processing_tables WHERE processing_set_id=%s',(set_id,)),
        ('DELETE FROM sec.source_quality_issues WHERE document_set_id=%s',(item.document_set,)),
        ('TRUNCATE sec.source_document_sets CASCADE',()),
    ]
    for query,args in actions:
        with pytest.raises(psycopg.Error),conn.transaction():
            conn.execute(query,args)


def test_postgres_direct_sql_numeric_mismatch_and_wrong_source_membership_rejected(local_connection):
    conn=local_connection
    item=database_fixture(conn,with_occurrences=False)
    record=item.occurrence_inputs[0]
    for evidence,document in [(record.evidence|{'inline_mapped_value':'999'},record.html_document_id),
                              (record.evidence,item.sources['xml'])]:
        with pytest.raises(psycopg.Error),conn.transaction():
            conn.execute('''INSERT INTO sec.fact_source_occurrences(filing_id,document_set_id,fact_id,html_document_id,inline_id,source_xpath,evidence)
                VALUES(%s,%s,%s,%s,%s,%s,%s)''',(record.filing_id,record.document_set_id,record.fact_id,document,
                                               record.inline_id,record.source_xpath,Jsonb(evidence)))


def test_postgres_review_cannot_replace_a_missing_or_wrong_core_fact(local_connection):
    with pytest.raises(psycopg.errors.CheckViolation,match='Declared cash core'),local_connection.transaction():
        database_fixture(local_connection,corrupt_core=True)


def test_migration_declares_exact_v4_scope_and_keeps_legacy_preferences_closed():
    source=(Path(__file__).resolve().parents[1]/'src/sec_pipeline/migrations/008_source_document_sets.sql').read_text(encoding='utf-8')
    for token in ('fact_source_occurrences','source_quality_issues','source_processing_reports','v4_processing_artifact_is_sealed',
                  'Inline/XML value mismatch is a hard source failure','membership_sha256 := encode(sha256',
                  "parent.extraction_version NOT IN ('tables-v3','tables-v4')",'review_required_cells','quality_issue_codes',
                  'guard_v4_source_receipt','guard_v4_truncate'):
        assert token in source
    assert 'CREATE OR REPLACE VIEW sec.filing_overview' not in source
    assert 'INSERT INTO sec.preferred_processing_sets' not in source
