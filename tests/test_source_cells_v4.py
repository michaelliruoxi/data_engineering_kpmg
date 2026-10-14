"""Source-exact period review cannot repair mismatches or missing core evidence."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from uuid import UUID

from lxml import html
import pytest

from sec_pipeline.database import FactInput, FinancialTableInput
from sec_pipeline.source_cells_v4 import transform_tables, validate_transformation
from sec_pipeline.source_cells_v3 import SourceCellError, fact_reference
from sec_pipeline.source_locators import inline_source_xpath, source_xpath
from sec_pipeline.tables import layout_grid


ROOT = Path(__file__).resolve().parents[1]
GAAP = 'http://fasb.org/us-gaap/2026'
PERIOD = {'startDate': '2026-01-01', 'endDate': '2026-03-31'}
SCOPE = {'filing_id': str(UUID(int=1)), 'xml_document_id': str(UUID(int=3)), 'facts_version': 'facts-v1'}
UNIT = {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []}
ENTITY = {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'}


def fixture(*, opening=False, shown='24', value='24'):
    definitions = [
        ('operating', 'Operating activities', 'NetCashProvidedByUsedInOperatingActivities', '10', 'duration'),
        ('investing', 'Investing activities', 'NetCashProvidedByUsedInInvestingActivities', '5', 'duration'),
        ('financing', 'Financing activities', 'NetCashProvidedByUsedInFinancingActivities', '3', 'duration'),
        ('review', 'Beginning of period' if opening else 'Issuance of common shares for dividend reinvestment',
         'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents' if opening else 'DividendsPayableCurrentAndNoncurrent', value, 'instant'),
        ('closing', 'Cash, end of period', 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents', '30', 'instant'),
    ]
    rows, facts = ['<tr><td></td><td>Three months ended March 31, 2026</td></tr>'], []
    for identifier, label, concept, raw, period in definitions:
        display = shown if identifier == 'review' else raw
        rows.append(f'<tr><td>{label}</td><td><ix:nonFraction id="{identifier}" name="g:{concept}" contextRef="{identifier}-context" unitRef="usd">{display}</ix:nonFraction></td></tr>')
        facts.append(FactInput(filing_id=UUID(int=1), source_document_id=UUID(int=3), extraction_version='facts-v1',
            occurrence_key=identifier, concept_namespace=GAAP, concept_name=concept, numeric_value=Decimal(raw), raw_value=raw,
            period_kind=period, unit=deepcopy(UNIT),
            period_start=date(2026, 1, 1) if period == 'duration' else None,
            period_end=date(2026, 3, 31) if period == 'duration' else None,
            instant_date=(date(2026, 1, 1) if opening and identifier == 'review' else date(2026, 3, 31)) if period == 'instant' else None,
            source_reference={'element_id':identifier,'context_ref':identifier+'-context','unit_ref':'usd','entity':deepcopy(ENTITY)}))
    source = (f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body><table>'+''.join(rows)+'</table></body></html>').encode()
    tree = html.document_fromstring(source).getroottree()
    cells, grid = layout_grid(tree.xpath('//table')[0], tree)
    for index, row in enumerate(cells):
        for cell in row:
            cell.pop('_element')
            cell.update(row_heading=definitions[index-1][1] if index else '', column_headings=['Three months ended March 31, 2026'],
                        column_period=deepcopy(PERIOD), fact_references=[], unresolved_reason=None, is_total=False)
            if index == 0:
                cell['classification'] = 'header'
            elif cell['column'] == 0:
                cell['classification'] = 'label'
            elif definitions[index-1][0] == 'review':
                cell['classification'] = 'unresolved'
                cell['unresolved_reason'] = 'Fact period differs from column heading' if opening else 'Unexpected instant in cash-flow row'
            else:
                fact = facts[index-1]
                node = tree.xpath('//*[@id="'+fact.occurrence_key+'"]')[0]
                cell['classification'] = 'resolved'
                cell['fact_references'] = [{**fact_reference(fact),'xml_id':fact.occurrence_key,'method':'inline_id',
                    'inline_xpath':inline_source_xpath(tree,node),'scope':deepcopy(SCOPE)}]
    structure = {'schema_version':2,'source_value_policy':'source-marker-v1',
        'html_sha256':hashlib.sha256(source).hexdigest(),'xml_sha256':'b'*64,
        'source_xpath':source_xpath(tree,tree.xpath('//table')[0]), 'fact_scope':deepcopy(SCOPE),'rows':cells,'grid':grid,
        'inventory':dict(Counter(cell['classification'] for row in cells for cell in row))}
    return FinancialTableInput(filing_id=UUID(int=1),source_document_id=UUID(int=2),extraction_version='tables-v3',
        table_key='cash_flow',statement_type='cash_flow',structure=structure,readable_text='source cash statement'), facts, source


@pytest.mark.parametrize('opening', [False, True])
def test_exact_source_values_and_dates_are_preserved_with_explicit_review(opening):
    table, facts, source = fixture(opening=opening)
    original_table, original_facts = deepcopy(table), deepcopy(facts)
    [result] = transform_tables([table], source, facts)
    cell = result.structure['rows'][4][1]
    assert cell['classification'] == 'source_conflict'
    assert cell['fact_references'] == []
    ref = cell['source_fact_references'][0]
    assert ref['concept'] == '{'+GAAP+'}'+facts[3].concept_name
    assert ref['period'] == fact_reference(facts[3])['period']
    assert ref['raw_value'] == '24' and ref['inline_document_id'] == str(UUID(int=2))
    issue = cell['quality_issues'][0]
    assert issue['code'] == ('opening_balance_date_not_prior_day' if opening else 'instant_context_under_duration_header')
    assert issue['expected']['period'] == PERIOD
    assert issue['observed']['period'] == ref['period']
    assert cell['column_period'] == PERIOD
    assert cell['quality_issue_keys'] == [issue['issue_key']]
    assert table == original_table and facts == original_facts
    assert transform_tables([table], source, facts) == [result]
    assert validate_transformation([result], [table], source, facts)


@pytest.mark.parametrize('change', ['amount','date','unit','entity','dimension','nil','no_core','wrong_statement'])
def test_source_mismatch_or_incomplete_scope_cannot_be_downgraded_to_review(change):
    table, facts, source = fixture(shown='25' if change == 'amount' else '24')
    if change == 'date':
        facts[3] = replace(facts[3], instant_date=date(2026,3,30))
    elif change == 'unit':
        facts[3] = replace(facts[3], unit={'numerator':['{http://www.xbrl.org/2003/iso4217}EUR'],'denominator':[]})
    elif change == 'entity':
        facts[3] = replace(facts[3], source_reference={**facts[3].source_reference,'entity':{**ENTITY,'identifier':'0000000002'}})
    elif change == 'dimension':
        facts[3] = replace(facts[3], dimensions=[{'kind':'explicit','dimension':'{test}Axis','member':'{test}Member'}])
    elif change == 'nil':
        facts[3] = replace(facts[3], is_nil=True, numeric_value=None, raw_value=None)
    elif change == 'no_core':
        table.structure['rows'][1][1].update(classification='unresolved',fact_references=[],unresolved_reason='missing core')
    elif change == 'wrong_statement':
        table = replace(table,statement_type='income_statement')
    [result] = transform_tables([table],source,facts)
    assert result.structure['rows'][4][1]['classification'] == 'unresolved'
    assert not result.structure['rows'][4][1].get('quality_issues')


@pytest.mark.parametrize('change', ['table_xpath','xml_as_html','row_label','header','period','coordinates','span'])
def test_claimed_provenance_must_match_original_table_and_headers(change):
    table,facts,source = fixture()
    cell = table.structure['rows'][4][1]
    if change == 'table_xpath': table.structure['source_xpath'] = '/html/body/table[99]'
    elif change == 'xml_as_html': table = replace(table,source_document_id=UUID(int=3))
    elif change == 'row_label': cell['row_heading'] = 'Invented source meaning'
    elif change == 'header': cell['column_headings'] = ['Invented duration caption']
    elif change == 'period': cell['column_period']['startDate'] = '2025-01-01'
    elif change == 'coordinates': cell['row'] += 1
    else: cell['colspan'] += 1
    [result] = transform_tables([table],source,facts)
    assert result.structure['rows'][4][1]['classification'] == 'unresolved'
    assert not result.structure['rows'][4][1].get('quality_issues')


@pytest.mark.parametrize('change', ['observed_period','expected_period','binding','missing_issue','hidden_cell'])
def test_review_evidence_tampering_fails_fresh_reconstruction(change):
    original,facts,source = fixture()
    [result] = transform_tables([original],source,facts)
    cell = result.structure['rows'][4][1]
    if change == 'observed_period': cell['quality_issues'][0]['observed']['period']['instant'] = '2026-03-30'
    elif change == 'expected_period': cell['quality_issues'][0]['expected']['period'] = {'instant':'2026-03-31'}
    elif change == 'binding': cell['source_fact_references'][0]['raw_value'] = '0'
    elif change == 'missing_issue': cell['quality_issues'] = []
    else:
        cell.update(classification='formatting', source_fact_references=[], quality_issues=[],quality_issue_keys=[])
        result.structure['inventory'] = dict(Counter(c['classification'] for row in result.structure['rows'] for c in row))
    with pytest.raises(SourceCellError,match='reconstruction'):
        validate_transformation([result],[original],source,facts)


@pytest.mark.parametrize('accession,expected', [('0001047862-26-000142',2),('0001193125-26-380207',1)])
def test_archived_source_period_issues_are_retained_without_changing_facts(accession,expected):
    from sec_pipeline import filing_adapter_v3 as adapter
    from sec_pipeline.processing import DryRunContext,SourceIds
    from sec_pipeline.filing_sources import verify_filing
    path = ROOT/'data/raw/sec'/accession/'source_manifest.json'
    if not path.exists(): pytest.skip('Optional archived source is unavailable.')
    manifest = verify_filing(path,ROOT)
    ctx = DryRunContext(manifest=manifest,data_root=ROOT,source_ids=SourceIds(UUID(int=1),UUID(int=2),UUID(int=3)),
        versions=adapter.VERSIONS,outputs={},report_id=UUID(int=4))
    plan = adapter.get_dry_run_plan()
    ctx.outputs['facts'] = plan.stages.facts(ctx)
    baseline = plan.stages.tables(ctx).items
    source = (ROOT/next(r['local_path'] for r in manifest['files'] if r['role']=='main_inline_xbrl_filing')).read_bytes()
    result = transform_tables(baseline,source,ctx.outputs['facts'].items)
    counts = Counter(c['classification'] for t in result for row in t.structure['rows'] for c in row)
    assert counts['source_conflict'] == expected and counts['unresolved'] == 0
    assert validate_transformation(result,baseline,source,ctx.outputs['facts'].items)
