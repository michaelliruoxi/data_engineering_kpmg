"""Explicit continuation and bank-cash component proofs reject mismatched scopes."""

from collections import Counter
from copy import deepcopy
import json

from lxml import html
import pytest

from sec_pipeline.balance_segments import verified_balance_segments
from sec_pipeline.bank_cash_components import bind_bank_cash_components
from sec_pipeline.tables import TableExtractionError, _segment_candidate, layout_grid, local


GAAP='http://fasb.org/us-gaap/2026'
UNIT={'numerator':['{http://www.xbrl.org/2003/iso4217}USD'],'denominator':[]}
ENTITY={'scheme':'http://www.sec.gov/CIK','identifier':'0000831001'}


def make_sources(bank=False):
    facts={}
    def row(label,name,values,stem,dimensions=None):
        cells=[]
        for suffix,period,value in zip(('26','25'),('2026-06-30','2025-12-31'),values):
            identifier=stem+suffix
            facts[identifier]={'xml_id':identifier,'concept':'{'+GAAP+'}'+name,'context_ref':suffix,'unit_ref':'usd','unit':deepcopy(UNIT),
                'raw_value':str(value),'is_nil':False,'period':{'instant':period},'dimensions':dimensions or [],'entity':deepcopy(ENTITY)}
            cells.append(f'<td><ix:nonFraction id="{identifier}" name="g:{name}" contextRef="{suffix}" unitRef="usd">{value}</ix:nonFraction></td>')
        return '<tr><td>'+label+'</td>'+''.join(cells)+'</tr>'
    header='<tr><td></td><td>June 30, 2026</td><td>December 31, 2025</td></tr>'
    if bank:
        contents='<table>'+row('Cash, due from banks and deposits with banks at end of period',
            'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',(100,80),'close')
        contents+=row('Cash and due from banks (including segregated cash and other deposits)','CashAndDueFromBanks',(30,20),'cash')
        contents+=row('Deposits with banks, net of allowance','InterestBearingDepositsInBanks',(70,60),'deposits')
        contents+=row('Cash, due from banks and deposits with banks at end of period',
            'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',(100,80),'total')+'</table>'
    else:
        contents='<h2>Consolidated Balance Sheet</h2><table>'+header+'<tr><td>Assets</td><td></td><td></td></tr>'
        contents+=row('Loans','LoansAndLeasesReceivableNetReportedAmount',(30,20),'detail',
            [{'kind':'explicit','dimension':'{urn:fixture}PortfolioAxis','member':'{urn:fixture}CommercialMember'}])
        contents+=row('Total assets','Assets',(100,80),'assets')+'</table>'
        contents+='<p>Statement continues on the next page.</p><p>94</p><h2>Consolidated Balance Sheet</h2><p>(Continued)</p>'
        contents+='<table>'+header+'<tr><td>Liabilities</td><td></td><td></td></tr>'
        contents+=row('Total liabilities','Liabilities',(60,50),'liabilities')
        contents+=row('Total equity','StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest',(40,30),'equity')
        contents+=row('Total liabilities and equity','LiabilitiesAndStockholdersEquity',(100,80),'total')+'</table>'
    tree=html.document_fromstring('<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:g="'+GAAP+'"><body>'+contents+'</body></html>').getroottree()
    ids=Counter(n.get('id') for n in tree.iter() if local(n)=='nonfraction')
    return tree,facts,ids


def candidates(tree,facts,ids):
    return [_segment_candidate(tree,table,'balance_sheet',facts,ids) for table in tree.xpath('//table')]


def test_explicit_assets_liabilities_pair_keeps_dimensional_details_and_exact_core():
    tree,facts,ids=make_sources()
    selected=verified_balance_segments(tree,candidates(tree,facts,ids),facts)
    assert [item.table_key for item in selected]==['balance_sheet.segment_1','balance_sheet.segment_2']
    proof=selected[0].group['balance_continuation']
    assert len(proof['totals'])==2 and len(proof['detail_scopes'])==2
    assert selected[0].element is tree.xpath('//table')[0]
    assert proof['totals'][0]['facts']['Assets']=='assets26'


@pytest.mark.parametrize('problem',['no_forward','no_continued','note_between','wrong_total','foreign_detail','core_dimension','currency'])
def test_balance_continuation_rejects_missing_or_conflicting_source_proof(problem):
    tree,facts,ids=make_sources()
    if problem=='no_forward':tree.xpath('//p')[0].text='Some other paragraph.'
    if problem=='no_continued':tree.xpath('//p')[2].text='Additional information.'
    if problem=='note_between':tree.xpath('//p')[1].text='Note 2. Other disclosures'
    chosen=candidates(tree,facts,ids)
    if problem=='wrong_total':
        for ref in chosen[1]['references'][chosen[1]['axes'][0]]:
            if ref['xml_id']=='total26':ref['raw_value']='101'
    if problem=='foreign_detail':
        next(ref for ref in chosen[0]['references'][chosen[0]['axes'][0]] if ref['xml_id']=='detail26')['entity']={'scheme':'http://www.sec.gov/CIK','identifier':'0000000001'}
    if problem=='core_dimension':
        next(ref for ref in chosen[0]['references'][chosen[0]['axes'][0]] if ref['xml_id']=='assets26')['dimensions']=[{'member':'parent'}]
    if problem=='currency':
        next(ref for ref in chosen[1]['references'][chosen[1]['axes'][0]] if ref['xml_id']=='equity26')['unit']={'numerator':['{http://www.xbrl.org/2003/iso4217}EUR'],'denominator':[]}
    assert verified_balance_segments(tree,chosen,facts) is None


def bank_args(tree,facts,ids):
    rows,_=layout_grid(tree.xpath('//table')[0],tree)
    periods={1:{'startDate':'2026-01-01','endDate':'2026-06-30'},2:{'startDate':'2025-01-01','endDate':'2025-12-31'}}
    return rows,periods,facts,{},ids


def test_bank_components_keep_original_bindings_and_prove_both_closing_totals():
    tree,facts,ids=make_sources(bank=True)
    bound,groups=bind_bank_cash_components(*bank_args(tree,facts,ids))
    assert len(bound)==6 and len(groups)==1
    assert groups[0]['verification_version']=='bank-cash-components-v1'
    assert groups[0]['periods'][0]['component_xml_ids']==['cash26','deposits26']
    assert bound['r001c001']['period']=={'instant':'2026-06-30'}


@pytest.mark.parametrize('problem',['wrong_total','foreign_namespace','foreign_entity','dimension','wrong_date','interrupted'])
def test_bank_components_never_match_by_amount_alone(problem):
    tree,facts,ids=make_sources(bank=True)
    if problem=='wrong_total':
        facts['total26']['raw_value']='101'
        tree.xpath('//*[@id="total26"]')[0].text='101'
    elif problem=='foreign_namespace':facts['deposits26']['concept']='{urn:foreign}InterestBearingDepositsInBanks'
    elif problem=='foreign_entity':facts['deposits26']['entity']={'scheme':'http://www.sec.gov/CIK','identifier':'9'}
    elif problem=='dimension':facts['deposits26']['dimensions']=[{'member':'parent'}]
    elif problem=='wrong_date':facts['deposits26']['period']={'instant':'2026-06-29'}
    else:tree.xpath('//table/tr[3]/td[1]')[0].text='Other financial activity'
    with pytest.raises(TableExtractionError):
        bind_bank_cash_components(*bank_args(tree,facts,ids))
