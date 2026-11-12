"""Flagged V8 profile: cash flow followers and recorded company tagging errors."""

from copy import deepcopy
from dataclasses import replace
from uuid import UUID

from lxml import html
import pytest

from sec_pipeline.database import DatabaseError
from sec_pipeline.extraction_profiles import AUDITED_PROFILE, FLAGGED_PROFILE, versions_for
from sec_pipeline.parser_policy import policy_metadata
from sec_pipeline.source_set_storage import SOURCE_CHECKS, TaggingDefectInput, V4ProcessingSetInput, store_v4_processing_set
from sec_pipeline.table_cells import TableCellError
from sec_pipeline.tables import _column_periods, layout_grid, select_statement_segments
from sec_pipeline.tagging_defects import RULE_VERSION, TaggingDefectError, defect_key, validate_records
from sec_pipeline.v4_table_cells import v4_relational_cells
from tests.test_v4_table_cells import conflict_fixture


GAAP = 'http://fasb.org/us-gaap/2025'
YEAR = {'startDate': '2025-01-01', 'endDate': '2025-12-31'}
END = {'instant': '2025-12-31'}
USD = {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []}
ENTITY = {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'}


def _tag(facts, ident, concept, period, value):
    context = {json_key(YEAR): 'year', json_key(END): 'end'}.get(json_key(period), 'other-' + ident)
    facts[ident] = {'xml_id': ident, 'concept': '{' + GAAP + '}' + concept, 'context_ref': context, 'unit_ref': 'usd',
                    'unit': USD, 'period': period, 'dimensions': [], 'raw_value': value, 'is_nil': False, 'entity': ENTITY}
    return f'<ix:nonfraction id="{ident}" name="g:{concept}" contextref="{context}" unitref="usd">{value}</ix:nonfraction>'


def json_key(period):
    return tuple(sorted(period.items()))


def _table(title, header, rows, key):
    body = ''.join(f'<tr><td>{label}</td><td>{cell}</td></tr>' for label, cell in rows)
    return f'<p>{title}</p><table id="{key}"><tr><td></td><td>{header}</td></tr>{body}</table>'


def follower_fixture(*, reconciliation_total='300', supplement_period=YEAR, with_reconciliation=True):
    facts = {}
    income = [(name, _tag(facts, f'income-{index}', name, YEAR, '1'))
              for index, name in enumerate(['Revenues', 'OperatingIncomeLoss', 'NetIncomeLoss'])]
    balance = [(name, _tag(facts, f'balance-{index}', name, END, '1'))
               for index, name in enumerate(['Assets', 'Liabilities', 'StockholdersEquity'])]
    cash = [(name, _tag(facts, f'cash-{index}', name, YEAR, '1')) for index, name in enumerate(
        ['NetCashProvidedByUsedInOperatingActivities', 'NetCashProvidedByUsedInInvestingActivities',
         'NetCashProvidedByUsedInFinancingActivities'])]
    cash.append(('Cash, cash equivalents and restricted cash, end of period',
                 _tag(facts, 'cash-3', 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents', END, '300')))
    parts = [_table('Consolidated Statements of Income', 'Year Ended December 31, 2025', income, 'income'),
             _table('Consolidated Balance Sheets', 'December 31, 2025', balance, 'balance'),
             _table('Consolidated Statements of Cash Flows', 'Year Ended December 31, 2025', cash, 'cash')]
    if with_reconciliation:
        parts.append(_table('Reconciliation of cash, cash equivalents and restricted cash', 'Year Ended December 31, 2025', [
            ('Cash and cash equivalents', _tag(facts, 'recon-0', 'CashAndCashEquivalentsAtCarryingValue', END, '200')),
            ('Restricted cash', _tag(facts, 'recon-1', 'RestrictedCashNoncurrent', END, '100')),
            ('Total cash, cash equivalents and restricted cash',
             _tag(facts, 'recon-2', 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents', END, reconciliation_total)),
        ], 'recon'))
    parts.append(_table('Supplemental disclosures of cash flow information', 'Year Ended December 31, 2025', [
        ('Interest paid', _tag(facts, 'supp-0', 'InterestPaidNet', supplement_period, '5')),
        ('Income taxes paid', _tag(facts, 'supp-1', 'IncomeTaxesPaidNet', supplement_period, '7')),
    ], 'supp'))
    source = (f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
              + ''.join(parts) + '</body></html>')
    return html.document_fromstring(source).getroottree(), facts


def cash_segments(tree, facts, **options):
    return [segment for segment in select_statement_segments(tree, facts, **options) if segment.statement_type == 'cash_flow']


def test_cash_followers_are_selected_only_by_v8_and_keep_their_roles():
    tree, facts = follower_fixture()
    assert [segment.table_key for segment in cash_segments(tree, facts)] == ['cash_flow']
    flagged = cash_segments(tree, facts, cash_followers=True)
    assert [segment.table_key for segment in flagged] == ['cash_flow.segment_1', 'cash_flow.segment_2', 'cash_flow.segment_3']
    assert [segment.group['segment_role'] for segment in flagged] == [
        'statement', 'closing_cash_reconciliation', 'supplemental_disclosures']
    assert [segment.element.get('id') for segment in flagged] == ['cash', 'recon', 'supp']
    assert all(segment.group['segment_roles'] == ['statement', 'closing_cash_reconciliation', 'supplemental_disclosures']
               and segment.group['column_periods'] == [YEAR] for segment in flagged)


def test_a_follower_that_does_not_match_the_statement_stays_outside():
    # The reconciliation does not add up to the closing balance: nothing follows.
    tree, facts = follower_fixture(reconciliation_total='301')
    assert [segment.table_key for segment in cash_segments(tree, facts, cash_followers=True)] == ['cash_flow']
    # Supplemental amounts for another period: only the reconciliation follows.
    tree, facts = follower_fixture(supplement_period={'startDate': '2024-01-01', 'endDate': '2024-12-31'})
    assert [segment.element.get('id') for segment in cash_segments(tree, facts, cash_followers=True)] == ['cash', 'recon']
    # A supplement directly after the statement follows on its own.
    tree, facts = follower_fixture(with_reconciliation=False)
    flagged = cash_segments(tree, facts, cash_followers=True)
    assert [segment.group['segment_role'] for segment in flagged] == ['statement', 'supplemental_disclosures']


QUARTER = {'startDate': '2022-10-29', 'endDate': '2023-01-27'}
NEXT = {'startDate': '2023-10-28', 'endDate': '2024-01-26'}


def column_fixture(periods, header=('Three months ended', 'January 27, 2023'), balance_dates=('2022-10-28', '2023-01-27')):
    facts, rows = {}, []
    for index, period in enumerate(periods):
        rows.append((f'Row {index}', _tag(facts, f'row-{index}', f'Concept{index}', period, '1')))
    for index, day in enumerate(balance_dates):
        _tag(facts, f'assets-{index}', 'Assets', {'instant': day}, '1')
    source = (f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body><table>'
              f'<tr><td></td><td>{header[0]}</td></tr><tr><td></td><td>{header[1]}</td></tr>'
              + ''.join(f'<tr><td>{label}</td><td>{cell}</td></tr>' for label, cell in rows) + '</table></body></html>')
    tree = html.document_fromstring(source).getroottree()
    grid_rows, _ = layout_grid(tree.xpath('//table')[0], tree)
    html_ids = {key: 1 for key in facts}
    return lambda **options: _column_periods(grid_rows, 2, facts, {}, html_ids, advanced=True, **options)[0].get(1)


def test_one_outlier_tag_does_not_hide_the_printed_column_period_under_v8():
    periods = column_fixture([QUARTER] * 5 + [NEXT])
    assert periods() is None
    assert periods(tagging_defects=True) == QUARTER
    # Outliers must stay under a quarter of the agreeing tags; an even split never resolves.
    assert column_fixture([QUARTER] * 3 + [NEXT])(tagging_defects=True) is None
    assert column_fixture([QUARTER, QUARTER, NEXT, NEXT])(tagging_defects=True) is None


def test_a_column_tagged_from_a_balance_date_takes_the_printed_fiscal_period_under_v8():
    tagged = {'startDate': '2024-04-26', 'endDate': '2024-10-25'}
    periods = column_fixture([tagged, tagged, tagged], header=('Six Months Ended', 'October 25, 2024'),
                             balance_dates=('2024-04-26', '2024-07-26', '2024-10-25'))
    assert periods() is None
    assert periods(tagging_defects=True) == {'startDate': '2024-04-27', 'endDate': '2024-10-25'}
    # A start that is not a balance-sheet date is not corrected.
    periods = column_fixture([tagged, tagged], header=('Six Months Ended', 'October 25, 2024'),
                             balance_dates=('2024-07-26', '2024-10-25'))
    assert periods(tagging_defects=True) is None


def flagged_cell_fixture(*, version='tables-v8'):
    table, fact, _ = conflict_fixture()
    structure = deepcopy(table.structure)
    cell = structure['rows'][0][1]
    issue, ref = cell['quality_issues'][0], cell['source_fact_references'][0]
    key = defect_key('tables-v8', {'cell_xpath': cell['source_xpath'], 'inline_id': 'F1'})
    record = {'defect_key': key, 'rule_version': RULE_VERSION, 'code': issue['code'], 'inline_id': 'F1',
              'inline_document_id': ref['inline_document_id'], 'table_key': table.table_key, 'cell_key': cell['cell_id'],
              'expected': issue['expected'], 'observed': issue['observed'],
              'evidence': issue['evidence'] | {'inline_xpath': ref['inline_xpath'],
                                               'cell_tags': [{'inline_id': 'F1', 'inline_xpath': ref['inline_xpath']}]}}
    for name in ('source_fact_references', 'quality_issues', 'quality_issue_keys'):
        cell.pop(name)
    cell.update(classification='source_defect', tagging_defect={
        'defect_key': key, 'code': issue['code'], 'rule_version': RULE_VERSION, 'inline_id': 'F1',
        'inline_xpath': ref['inline_xpath'], 'cell_tags': deepcopy(record['evidence']['cell_tags'])})
    structure['inventory'] = {'label': 1, 'source_defect': 1}
    return replace(table, extraction_version=version, structure=structure), record


def test_flagged_cells_are_stored_unlinked_with_their_own_status():
    table, record = flagged_cell_fixture()
    cell, accepted, descriptor = v4_relational_cells(UUID(int=10), table)[1]
    assert (cell.kind, cell.resolution_status, cell.unresolved_reason, accepted) == ('data', 'source_defect', None, [])
    assert descriptor['tagging_defect']['defect_key'] == record['defect_key']
    assert validate_records((record,), (table,)) is True


@pytest.mark.parametrize('change', ['version', 'code', 'key', 'linked', 'tags'])
def test_malformed_or_foreign_flagged_cells_are_rejected(change):
    table, _ = flagged_cell_fixture(version='tables-v7' if change == 'version' else 'tables-v8')
    cell = table.structure['rows'][0][1]
    if change == 'code':
        cell['tagging_defect']['code'] = 'printed_number_outside_tag'
    elif change == 'key':
        cell['tagging_defect']['defect_key'] = 'tables-v7:tagging-defect:' + '0' * 64
    elif change == 'linked':
        cell['fact_references'] = [{'xml_id': 'F1'}]
    elif change == 'tags':
        cell['tagging_defect']['cell_tags'] = []
    with pytest.raises(TableCellError):
        v4_relational_cells(UUID(int=10), table)


def test_defect_records_must_match_their_cells_and_codes():
    table, record = flagged_cell_fixture()
    with pytest.raises(TaggingDefectError):
        validate_records((), (table,))
    with pytest.raises(TaggingDefectError):
        validate_records((record | {'code': 'fact_period_differs_from_column'},), (table,))
    fact_level = record | {'table_key': None, 'cell_key': None}
    with pytest.raises(TaggingDefectError):
        validate_records((record, fact_level | {'defect_key': record['defect_key'] + 'x'}), (table,))
    boundary = fact_level | {'code': 'printed_number_outside_tag', 'defect_key': record['defect_key'][:-1] + 'f'}
    assert validate_records((record, boundary), (table,)) is True


def test_defect_inputs_and_seals_keep_v8_boundaries():
    ids = {name: UUID(int=index + 1) for index, name in enumerate(
        ('filing_id', 'document_set_id', 'fact_id', 'occurrence_id', 'inline_document_id', 'table_id', 'cell_id'))}
    base = dict(ids, defect_key='tables-v8:tagging-defect:' + '0' * 64, inline_id='F1', code='fact_period_differs_from_column',
                expected={'period': YEAR}, observed={'period': END}, evidence={'inline_xpath': '/x'})
    assert TaggingDefectInput(**base).rule_version == RULE_VERSION
    for change in ({'cell_id': None, 'table_id': None}, {'code': 'printed_number_outside_tag'},
                   {'observed': {'period': YEAR}}, {'rule_version': 'cash-period-review-v1'}):
        with pytest.raises(DatabaseError):
            TaggingDefectInput(**(base | change))
    assert TaggingDefectInput(**(base | {'cell_id': None, 'table_id': None, 'code': 'printed_number_outside_tag',
                                         'expected': {'printed_number': '16.7'},
                                         'observed': {'tagged_number': '16'}})).cell_id is None
    selection = V4ProcessingSetInput(filing_id=ids['filing_id'], set_key='audited', document_set_id=ids['document_set_id'],
                                     source_checks={key: True for key in SOURCE_CHECKS}, semantic_checks={'period_header_consistency': True},
                                     coverage={'facts': 'complete', 'tables': 'partial', 'reports': 'complete', 'chunks': 'complete'},
                                     configuration={}, versions=versions_for(AUDITED_PROFILE))
    with pytest.raises(DatabaseError, match='Only tables-v8'):
        store_v4_processing_set(None, selection, table_ids=[ids['table_id']], reports=[], defect_ids=[UUID(int=99)])


def test_v8_parser_revisions_name_both_new_policies():
    revisions = policy_metadata(versions_for(FLAGGED_PROFILE))['parser_revisions']
    assert revisions == {'facts': 'numeric-tag-boundary-defects-v1', 'reports': 'audited-section-metadata-v1',
                         'tables': 'cash-statement-followers-tagging-defects-v1', 'chunks': 'token-boundary-overlap-v2'}
    assert policy_metadata(versions_for(AUDITED_PROFILE))['parser_revisions']['facts'] == 'numeric-tag-boundaries-v3'
