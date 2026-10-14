"""Short printed cash labels preserve exact native concepts and endpoint dates."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from lxml import html
import pytest

from sec_pipeline import filing_adapter_v3
from sec_pipeline.filing_sources import verify_filing
from sec_pipeline.processing import DryRunContext,SourceIds
from sec_pipeline.statement_aliases import _ordinary_cash_endpoint_period,select_alias_statement_segments
from sec_pipeline.tables import xml_evidence


CASH='{http://fasb.org/us-gaap/2026}CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations'
PERIOD={'startDate':'2026-01-01','endDate':'2026-06-30'}
ROOT=Path(__file__).resolve().parents[1]
FOLDER=ROOT/'data/raw/sec/0001193125-26-339405'


@pytest.mark.parametrize('label,date',[
    ('Cash and cash equivalents, beginning of period','2025-12-31'),
    ('Cash and cash equivalents, end of period','2026-06-30'),
])
def test_literal_short_label_has_one_supported_source_endpoint(label,date):
    assert _ordinary_cash_endpoint_period(CASH,label,PERIOD)=={'instant':date}


@pytest.mark.parametrize('label',['Cash and cash equivalents','Other cash and cash equivalents, end of period',
    'Cash and cash equivalents, net change for period','Cash and cash equivalents, beginning and end of period',
    'Cash and cash equivalents, beginning of next period','Cash and cash equivalents, June 30'])
def test_ambiguous_or_nonendpoint_short_labels_remain_unsupported(label):
    assert _ordinary_cash_endpoint_period(CASH,label,PERIOD) is None


def test_short_label_never_promotes_a_foreign_qname_or_invalid_duration():
    label='Cash and cash equivalents, end of period'
    assert _ordinary_cash_endpoint_period(CASH.replace('http://fasb.org','https://issuer.invalid'),label,PERIOD) is None
    for period in ({'instant':'2026-06-30'},{'startDate':'2026-07-01','endDate':'2026-06-30'},
                   {'startDate':'bad','endDate':'2026-06-30'}):
        assert _ordinary_cash_endpoint_period(CASH,label,period) is None


def source():
    path=FOLDER/'pkg-20260630.htm'
    if not path.exists():pytest.skip('Optional archived Packaging Corporation source unavailable.')
    return html.parse(str(path),parser=html.HTMLParser(encoding='utf-8')),xml_evidence(FOLDER/'pkg-20260630_htm.xml',include_entity=True)


def test_archived_short_labels_pass_full_v3_validation_without_relabeling():
    path=FOLDER/'source_manifest.json'
    if not path.exists():pytest.skip('Optional archived source unavailable.')
    outputs={}
    context=DryRunContext(manifest=verify_filing(path,ROOT),data_root=ROOT,
        source_ids=SourceIds(UUID(int=1),UUID(int=2),UUID(int=3)),versions=filing_adapter_v3.VERSIONS,outputs=outputs,report_id=UUID(int=4))
    with patch('psycopg.connect',side_effect=AssertionError('Database access forbidden')), \
            patch('socket.create_connection',side_effect=AssertionError('Network access forbidden')):
        plan=filing_adapter_v3.get_dry_run_plan()
        for stage in ('facts','tables','report','chunks'):outputs[stage]=getattr(plan.stages,stage)(context)
        checked=plan.stages.validate(context)
    assert all(value is True for value in checked.checks.values()),checked.checks
    cash=next(table for table in outputs['tables'].items if table.statement_type=='cash_flow')
    proof=cash.structure['statement_group']['standard_cash_alias']
    assert len(proof['endpoint_periods'])==4
    assert {entry['row_heading'] for entry in proof['endpoint_sources'].values()}=={
        'Cash and cash equivalents, beginning of period','Cash and cash equivalents, end of period'}


@pytest.mark.parametrize('change',['date','entity','currency','dimension','missing_close'])
def test_archived_alias_still_requires_exact_scope_dates_and_unique_closing(change):
    tree,facts=source()
    selected=select_alias_statement_segments(tree,facts)
    assert selected is not None
    proof=selected[-1].group['standard_cash_alias']
    identifier=next(iter(proof['endpoint_periods']))
    fact=facts[identifier]
    if change=='date':fact['period']={'instant':'2026-01-01'}
    elif change=='entity':fact['entity']=deepcopy(fact['entity'])|{'identifier':'0000000001'}
    elif change=='currency':fact['unit']={'numerator':['{http://www.xbrl.org/2003/iso4217}EUR'],'denominator':[]}
    elif change=='dimension':fact['dimensions']=[{'kind':'explicit','dimension':'{example}Axis','member':'{example}Member'}]
    else:
        closing=next(key for key,period in proof['endpoint_periods'].items() if period['instant']=='2026-06-30')
        row=tree.xpath('//*[@id=$id]/ancestor::tr[1]',id=closing)[0]
        label=row.xpath('./td|./th')[0]
        label.clear();label.text='Other cash balances'
    assert select_alias_statement_segments(tree,facts) is None
