"""Review-required cells preserve raw source links without accepted fact links."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import hashlib
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from lxml import html
import pytest

from sec_pipeline.database import FactInput, FinancialTableInput
from sec_pipeline.source_cells_v3 import fact_reference
from sec_pipeline.table_cells import TableCellError
from sec_pipeline.tables import layout_grid
from sec_pipeline.v4_table_cells import store_v4_table_cells, v4_relational_cells
from tests.test_source_cell_storage_v3 import StorageRecorder


def conflict_fixture(filing=UUID(int=1), html_id=UUID(int=2), xml_id=UUID(int=3), *, issue=True):
    source=b'<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body><table><tr><td>Issuance for dividend reinvestment</td><td><ix:nonFraction id="F1" name="g:DividendsPayableCurrentAndNoncurrent" contextRef="c1" unitRef="usd" scale="6">24</ix:nonFraction></td></tr></table></body></html>'
    tree=html.document_fromstring(source).getroottree()
    element=tree.xpath('//table')[0]
    rows,grid=layout_grid(element,tree)
    period={'startDate':'2026-01-01','endDate':'2026-06-30'}
    fact=FactInput(filing_id=filing,source_document_id=xml_id,extraction_version='facts-v1',occurrence_key='F1',
        concept_namespace='http://fasb.org/us-gaap/2026',concept_name='DividendsPayableCurrentAndNoncurrent',
        numeric_value=Decimal('24000000'),raw_value='24000000',period_kind='instant',instant_date=date(2026,6,30),
        unit={'numerator':['{http://www.xbrl.org/2003/iso4217}USD'],'denominator':[]},
        source_reference={'element_id':'F1','context_ref':'c1','unit_ref':'usd'})
    scope={'filing_id':str(filing),'xml_document_id':str(xml_id),'facts_version':'facts-v1'}
    ref=fact_reference(fact)|{'xml_id':'F1','scope':scope,'method':'inline_id','inline_document_id':str(html_id),
                            'inline_xpath':'/html/body/table/tr/td[2]/ix:nonfraction'}
    for row in rows:
        for cell in row:
            cell.pop('_element')
            cell.update(row_heading='Issuance for dividend reinvestment',column_headings=['Six Months Ended June30,2026'] if cell['column'] else [],
                        column_period=period if cell['column'] else None,classification='source_conflict' if cell['column'] else 'label',
                        fact_references=[],unresolved_reason=None,is_total=False)
    cell=rows[0][1]
    descriptor={'issue_key':'cash/r000c001/period','code':'instant_context_under_duration_header','rule_version':'cash-period-review-v1',
                'expected':{'period':period},'observed':{'period':{'instant':'2026-06-30'}},
                'evidence':{'source_xpath':cell['source_xpath'],'xml_id':'F1'}}
    cell.update(source_fact_references=[ref],quality_issue_keys=[descriptor['issue_key']],quality_issues=[descriptor])
    if not issue:
        cell.update(classification='resolved',column_period=ref['period'],fact_references=[ref])
        for key in ('source_fact_references','quality_issue_keys','quality_issues'):
            cell.pop(key)
    table=FinancialTableInput(filing_id=filing,source_document_id=html_id,extraction_version='tables-v4',table_key='cash_flow',
        statement_type='cash_flow',readable_text='Issuance for dividend reinvestment\t24',structure={
            'schema_version':3,'rows':rows,'grid':grid,'source_xpath':tree.getpath(element),'fact_scope':scope,
            'html_sha256':hashlib.sha256(source).hexdigest(),'xml_sha256':'b'*64,
            'inventory':{'label':1,'source_conflict' if issue else 'resolved':1}})
    return table,fact,source


def test_review_cell_retains_original_value_and_fact_context_without_accepted_links():
    table,_,_=conflict_fixture()
    before=deepcopy(table)
    parsed=v4_relational_cells(UUID(int=10),table)
    cell,accepted,descriptor=parsed[1]
    assert (cell.kind,cell.resolution_status,accepted)==('data','review_required',[])
    assert cell.column_period=={'startDate':'2026-01-01','endDate':'2026-06-30'}
    assert descriptor['source_fact_references'][0]['period']=={'instant':'2026-06-30'}
    assert descriptor['source_fact_references'][0]['raw_value']=='24000000'
    assert table==before


@pytest.mark.parametrize('mutation',[
    lambda c:c.update(fact_references=c['source_fact_references']),
    lambda c:c.update(quality_issue_keys=[]),
    lambda c:c['quality_issues'][0]['observed'].update(period={'instant':'2026-06-29'}),
    lambda c:c['source_fact_references'][0].update(inline_document_id=str(UUID(int=99))),
    lambda c:c.update(classification='formatting'),
    lambda c:c.update(source_fact_references=[]),
])
def test_invalid_review_representations_are_rejected_before_storage(mutation):
    table,_,_=conflict_fixture()
    mutation(table.structure['rows'][0][1])
    with pytest.raises(TableCellError):
        v4_relational_cells(UUID(int=10),table)


def test_loader_writes_raw_binding_and_issue_separately_without_numeric_fact_or_accepted_link():
    table,_,_=conflict_fixture()
    conn=StorageRecorder()
    stored=[]
    def insert(connection,target,values,keys,**kwargs):
        stored.append((target,values))
        identifier=UUID(int=100+len(stored))
        if target=='table_cells':conn.cells[values['cell_key']]=identifier
        return identifier
    with patch('sec_pipeline.v4_table_cells._insert_exact',side_effect=insert), \
            patch('sec_pipeline.v4_table_cells.store_review_bindings',return_value=[UUID(int=201)]) as raw, \
            patch('sec_pipeline.v4_table_cells.store_quality_issues',return_value=[UUID(int=202)]) as issues:
        result=store_v4_table_cells(conn,UUID(int=10),table,{'F1':UUID(int=20)},{'F1':UUID(int=21)},UUID(int=30))
    assert [target for target,_ in stored]==['table_cells','table_cells']
    assert result.binding_ids=={'r000c001':UUID(int=201)}
    assert result.issue_ids=={'cash/r000c001/period':UUID(int=202)}
    assert raw.call_args.args[1][0].fact_id==UUID(int=20)
    assert issues.call_args.args[1][0].observed['period']=={'instant':'2026-06-30'}


def test_missing_source_occurrence_rejects_before_sql():
    table,_,_=conflict_fixture()
    conn=StorageRecorder()
    with pytest.raises(TableCellError):
        store_v4_table_cells(conn,UUID(int=10),table,{'F1':UUID(int=20)},{},UUID(int=30))
    assert conn.queries==[]


@pytest.mark.parametrize('accession,expected_count',[('0001047862-26-000142',2),('0001193125-26-380207',1)])
def test_real_period_conflict_outputs_fit_database_cell_contract(accession,expected_count):
    from sec_pipeline import filing_adapter_v3
    from sec_pipeline.filing_sources import verify_filing
    from sec_pipeline.processing import DryRunContext, SourceIds
    from sec_pipeline.source_cells_v4 import transform_tables
    root=Path(__file__).resolve().parents[1]
    path=root/'data/raw/sec'/accession/'source_manifest.json'
    if not path.is_file():pytest.skip('Optional archived source unavailable.')
    manifest=verify_filing(path,root)
    outputs={}
    context=DryRunContext(manifest=manifest,data_root=root,source_ids=SourceIds(UUID(int=1),UUID(int=2),UUID(int=3)),
                         versions=filing_adapter_v3.VERSIONS,outputs=outputs,report_id=UUID(int=4))
    with patch('psycopg.connect',side_effect=AssertionError('Database access forbidden')), \
            patch('socket.create_connection',side_effect=AssertionError('Network access forbidden')):
        plan=filing_adapter_v3.get_dry_run_plan()
        outputs['facts']=plan.stages.facts(context)
        outputs['tables']=plan.stages.tables(context)
        source=root/next(item['local_path'] for item in manifest['files'] if item['role']=='main_inline_xbrl_filing')
        tables=transform_tables(outputs['tables'].items,source.read_bytes(),outputs['facts'].items)
        cells=[record for table in tables for record,_,_ in v4_relational_cells(UUID(int=5),table)]
    assert sum(cell.resolution_status=='review_required' for cell in cells)==expected_count
