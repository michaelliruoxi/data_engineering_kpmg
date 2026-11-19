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


def test_period_conflicts_convert_to_flagged_cells_unless_the_number_is_partially_tagged():
    from sec_pipeline.tagging_defects import flag_table_cells
    table, fact, source = conflict_fixture()
    flagged, records = flag_table_cells([table], source, [fact], version='tables-v8')
    cell = flagged[0].structure['rows'][0][1]
    assert cell['classification'] == 'source_defect' and cell['fact_references'] == []
    assert [record['code'] for record in records] == ['instant_context_under_duration_header']
    assert records[0]['observed']['period'] == {'instant': '2026-06-30'} and records[0]['table_key'] == 'cash_flow'
    assert not {'source_fact_references', 'quality_issues', 'quality_issue_keys'} & set(cell)
    with pytest.raises(TaggingDefectError, match='partially tagged'):
        flag_table_cells([table], source, [fact], version='tables-v8', boundary_records=[{'inline_id': 'F1'}])


def test_a_balance_tag_on_the_companys_own_concept_becomes_a_recorded_defect():
    from sec_pipeline.source_cells_v4 import transform_tables
    from sec_pipeline.tagging_defects import flag_table_cells
    from tests.test_source_cells_v4 import OWN, fixture
    table, facts, source = fixture(own=OWN)
    reviewed = transform_tables([table], source, facts, tagging_defects=True)
    flagged, records = flag_table_cells(reviewed, source, facts, version='tables-v8')
    cell = flagged[0].structure['rows'][4][1]
    assert cell['classification'] == 'source_defect' and cell['fact_references'] == []
    assert cell['display_text'] == '24' and cell['tagging_defect']['inline_id'] == 'review'
    [record] = records
    assert record['code'] == 'instant_context_under_duration_header'
    assert record['observed']['concept'] == '{' + OWN + '}ChangeInAccruedPlantAndEquipmentPurchases'
    assert record['observed']['period'] == {'instant': '2026-03-31'}
    assert record['expected'] == {'period': {'startDate': '2026-01-01', 'endDate': '2026-03-31'}, 'row_period_kind': 'duration'}
    assert validate_records(records, flagged) is True
    # Without the page's own schema reference the cell stays unresolved and the filing is rejected.
    table, facts, source = fixture(own=OWN, schemas=())
    reviewed = transform_tables([table], source, facts, tagging_defects=True)
    flagged, records = flag_table_cells(reviewed, source, facts, version='tables-v8')
    assert flagged[0].structure['rows'][4][1]['classification'] == 'unresolved' and records == ()


def _readings(read):
    return [call.kwargs.get('corroborated_layouts', False) for call in read.call_args_list]


def test_a_filing_the_established_reading_rejects_is_read_once_more_with_the_corroborated_layouts():
    from unittest.mock import patch
    from sec_pipeline import filing_parser_v4 as parser
    accepted = object()
    with patch.object(parser, '_read', side_effect=[parser.FilingSetParseError('first reading'), accepted]) as read:
        assert parser._build('manifest', 'root', profile=FLAGGED_PROFILE) is accepted
    assert _readings(read) == [False, True]
    # A filing the established reading accepts is never read with them, so its output cannot change.
    with patch.object(parser, '_read', return_value=accepted) as read:
        assert parser._build('manifest', 'root', profile=FLAGGED_PROFILE) is accepted
    assert _readings(read) == [False]
    # Both readings reject the filing: the first rejection is the one reported.
    with patch.object(parser, '_read', side_effect=[parser.FilingSetParseError('first reading'),
                                                    ValueError('second reading')]) as read:
        with pytest.raises(parser.FilingSetParseError, match='first reading'):
            parser._build('manifest', 'root', profile=FLAGGED_PROFILE)
    assert _readings(read) == [False, True]


@pytest.mark.parametrize('profile', ['document-set-v4', 'corrected-layout-v5', 'reviewed-layout-v6', AUDITED_PROFILE])
def test_only_the_flagged_profile_reads_a_filing_a_second_time(profile):
    from unittest.mock import patch
    from sec_pipeline import filing_parser_v4 as parser
    with patch.object(parser, '_read', side_effect=[parser.FilingSetParseError('first reading'), object()]) as read:
        with pytest.raises(parser.FilingSetParseError, match='first reading'):
            parser._build('manifest', 'root', profile=profile)
    assert _readings(read) == [False]


@pytest.mark.parametrize('layouts', [False, True])
def test_the_reading_of_a_set_reaches_every_check_and_is_named_on_its_tables(tmp_path, layouts):
    from unittest.mock import patch
    from sec_pipeline import filing_parser_v4 as parser
    from tests.test_source_cells_v4 import fixture
    table, facts, source = fixture()
    root = tmp_path.resolve()
    (root / 'page.htm').write_bytes(source)
    (root / 'page.xml').write_bytes(b'<xbrl/>')
    page = {'source_url': 'https://example.test/page.htm', 'local_path': 'page.htm'}
    instance = {'source_url': 'https://example.test/page.xml', 'local_path': 'page.xml'}
    sources = {page['source_url']: UUID(int=2), instance['source_url']: UUID(int=3)}
    reviewed = replace(table, extraction_version='tables-v4', structure=table.structure | {'schema_version': 3})
    seen = {}

    def step(name, result):
        def call(*arguments, **options):
            seen[name] = options.get('corroborated_layouts')
            return result
        return call

    with patch.object(parser, 'extract_tables', step('extraction', [table])), \
            patch.object(parser, 'validate_statement_segments', step('selection', True)), \
            patch.object(parser, 'transform_v3', lambda *arguments, **options: [table]), \
            patch.object(parser, 'validate_representations', lambda *arguments, **options: None), \
            patch.object(parser.source_adapter, '_table_checks', lambda *arguments, **options: (True, True)), \
            patch.object(parser, 'transform_v4', step('period review', [reviewed])), \
            patch.object(parser, 'validate_transformation', step('review validation', True)), \
            patch('sec_pipeline.tagging_defects.flag_table_cells', step('flagging', ((reviewed,), ()))), \
            patch.object(parser, 'v4_relational_cells', lambda *arguments, **options: []):
        tables, defects = parser._checked_tables(
            {'files': []}, root, UUID(int=1), instance, page, sources, facts, (), cash_followers=True,
            tagging_defects=True, tables_version='tables-v8', corroborated_layouts=layouts)
    assert seen == dict.fromkeys(('extraction', 'selection', 'period review', 'review validation', 'flagging'), layouts)
    # Only a table of the second reading says so; a table of the established reading is stored as it always was.
    assert [stored.structure.get(parser.LAYOUT_READING) for stored in tables] == ([True] if layouts else [None])
    assert tables == ((replace(reviewed, structure=reviewed.structure | {'corroborated_layouts': True}),) if layouts
                      else (reviewed,))
    assert defects == ()


def test_flagging_checks_a_table_with_the_reading_that_produced_it(monkeypatch):
    from sec_pipeline import source_cells_v4
    from sec_pipeline.tagging_defects import flag_table_cells
    from tests.test_source_cells_v4 import fixture
    table, facts, source = fixture(heading='First Quarter 2026')
    reviewed = source_cells_v4.transform_tables([table], source, facts, tagging_defects=True, corroborated_layouts=True)
    seen = []

    class Recorded(source_cells_v4.ReviewSource):
        def __init__(self, *arguments, **options):
            seen.append(options)
            super().__init__(*arguments, **options)

    monkeypatch.setattr(source_cells_v4, 'ReviewSource', Recorded)
    flagged, records = flag_table_cells(reviewed, source, facts, version='tables-v8', corroborated_layouts=True)
    assert seen == [{'tagging_defects': True, 'corroborated_layouts': True}]
    assert flagged[0].structure['rows'][4][1]['classification'] == 'source_defect'
    assert [record['code'] for record in records] == ['instant_context_under_duration_header']
    assert validate_records(records, flagged) is True


def period_fixture(tagged, *, heading='Year Ended December 31, 2025', balances=()):
    """An income statement column as the extractor leaves it: one tagged amount per row, each in the period given.

    A period is (start, end) or a single date. A cell whose tag carries the
    column's printed period is resolved; any other is left unresolved for its
    period. ``balances`` are balance-sheet dates the filing reports elsewhere.
    """
    from collections import Counter
    from datetime import date
    from decimal import Decimal
    import hashlib
    from sec_pipeline.database import FactInput, FinancialTableInput
    from sec_pipeline.source_cells_v3 import fact_reference
    from sec_pipeline.source_locators import inline_source_xpath, source_xpath

    def fact(identifier, concept, period):
        duration = isinstance(period, tuple)
        return FactInput(
            filing_id=UUID(int=1), source_document_id=UUID(int=3), extraction_version='facts-v1', occurrence_key=identifier,
            concept_namespace=GAAP, concept_name=concept, numeric_value=Decimal('7'), raw_value='7',
            period_kind='duration' if duration else 'instant', unit=deepcopy(USD),
            period_start=date.fromisoformat(period[0]) if duration else None,
            period_end=date.fromisoformat(period[1]) if duration else None,
            instant_date=None if duration else date.fromisoformat(period),
            source_reference={'element_id': identifier, 'context_ref': identifier + '-context', 'unit_ref': 'usd',
                              'entity': deepcopy(ENTITY)})

    def inline(item):
        return (f'<ix:nonFraction id="{item.occurrence_key}" name="g:{item.concept_name}" '
                f'contextRef="{item.occurrence_key}-context" unitRef="usd">7</ix:nonFraction>')

    facts = [fact(f'row-{index}', f'Concept{index}', period) for index, period in enumerate(tagged)]
    reported = [fact(f'assets-{index}', 'Assets', day) for index, day in enumerate(balances)]
    source = (f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
              f'<body><table><tr><td></td><td>{heading}</td></tr>'
              + ''.join(f'<tr><td>Row {index}</td><td>{inline(item)}</td></tr>' for index, item in enumerate(facts))
              + '</table>' + ''.join(f'<p>{inline(item)}</p>' for item in reported) + '</body></html>').encode()
    tree = html.document_fromstring(source).getroottree()
    element = tree.xpath('//table')[0]
    cells, grid = layout_grid(element, tree)
    scope = {'filing_id': str(UUID(int=1)), 'xml_document_id': str(UUID(int=3)), 'facts_version': 'facts-v1'}
    evidence = {item.occurrence_key: {**fact_reference(item), 'xml_id': item.occurrence_key} for item in [*facts, *reported]}
    namespaces = {key[6:]: value for key, value in tree.getroot().attrib.items() if key.startswith('xmlns:')}
    periods, provenance = _column_periods(cells, 1, evidence, namespaces, dict.fromkeys(evidence, 1), advanced=True,
                                          tagging_defects=True)
    for index, row in enumerate(cells):
        for cell in row:
            cell.pop('_element')
            cell.update(row_heading=f'Row {index - 1}' if index else '', column_headings=[heading],
                        column_period=deepcopy(periods[1]), fact_references=[], unresolved_reason=None, is_total=False,
                        classification='header' if index == 0 else 'label')
            if index and cell['column']:
                item = facts[index - 1]
                if evidence[item.occurrence_key]['period'] == periods[1]:
                    node = tree.xpath(f'//*[@id="{item.occurrence_key}"]')[0]
                    cell.update(classification='resolved', fact_references=[{
                        **fact_reference(item), 'xml_id': item.occurrence_key, 'method': 'inline_id',
                        'inline_xpath': inline_source_xpath(tree, node), 'scope': deepcopy(scope)}])
                else:
                    cell.update(classification='unresolved', unresolved_reason='Fact period differs from column heading')
    structure = {'schema_version': 2, 'source_value_policy': 'source-marker-v1',
                 'html_sha256': hashlib.sha256(source).hexdigest(), 'xml_sha256': 'b' * 64,
                 'source_xpath': source_xpath(tree, element), 'fact_scope': scope, 'rows': cells, 'grid': grid,
                 'inventory': dict(Counter(cell['classification'] for row in cells for cell in row))}
    if provenance:
        structure['column_period_evidence'] = provenance
    return FinancialTableInput(filing_id=UUID(int=1), source_document_id=UUID(int=2), extraction_version='tables-v3',
                               table_key='income_statement', statement_type='income_statement', structure=structure,
                               readable_text='source income statement'), [*facts, *reported], source


def flagged_periods(table, facts, source):
    from sec_pipeline.source_cells_v4 import transform_tables
    from sec_pipeline.tagging_defects import flag_table_cells
    reviewed = transform_tables([table], source, facts, tagging_defects=True)
    flagged, records = flag_table_cells(reviewed, source, facts, version='tables-v8')
    assert validate_records(records, flagged) is True
    inventory = flagged[0].structure['inventory']
    return [record['code'] for record in records], inventory.get('source_defect', 0), inventory.get('unresolved', 0)


CALENDAR_YEAR, PRIOR_YEAR = ('2025-01-01', '2025-12-31'), ('2024-01-01', '2024-12-31')


@pytest.mark.parametrize('agreeing,contradicting,flagged', [(9, 2, 2), (5, 1, 1), (8, 2, 0), (4, 1, 0), (13, 3, 0)])
def test_a_tag_is_flagged_against_its_column_only_where_the_other_numbers_confirm_the_period(agreeing, contradicting, flagged):
    # At most two contradicting cells, and more than four times as many that carry the column's period.
    table, facts, source = period_fixture([CALENDAR_YEAR] * agreeing + [PRIOR_YEAR] * contradicting)
    assert table.structure['rows'][1][1]['column_period'] == {'startDate': '2025-01-01', 'endDate': '2025-12-31'}
    assert flagged_periods(table, facts, source) == (
        ['fact_period_differs_from_column'] * flagged, flagged, contradicting - flagged)


def test_a_header_read_as_a_date_does_not_turn_a_column_of_correct_tags_into_tagging_errors():
    # Qualcomm's 10-K for fiscal 2024 heads two income statement columns "September 24,2023" and
    # "September 25,2022" under a "Year Ended" caption that spans only the first column. Read alone, each is a
    # date, and every fiscal-year amount under it contradicts that date. Nothing confirms the reading, so none
    # of the tags is a tagging error and the cells stay unresolved.
    table, facts, source = period_fixture([CALENDAR_YEAR] * 6, heading='December 31, 2025')
    assert table.structure['rows'][1][1]['column_period'] == {'instant': '2025-12-31'}
    assert flagged_periods(table, facts, source) == ([], 0, 6)


def test_a_column_whose_every_tag_starts_one_day_early_is_still_flagged_as_a_whole():
    early = ('2024-04-26', '2024-10-25')
    table, facts, source = period_fixture([early] * 4, heading='Six Months Ended October 25, 2024',
                                          balances=('2024-04-26', '2024-07-26', '2024-10-25'))
    assert table.structure['column_period_evidence']['1']['method'] == 'fiscal_calendar_start_tagging_defects'
    assert table.structure['rows'][1][1]['column_period'] == {'startDate': '2024-04-27', 'endDate': '2024-10-25'}
    assert flagged_periods(table, facts, source) == (['fact_period_differs_from_column'] * 4, 4, 0)
    # Without that evidence the same cells are an unconfirmed column.
    del table.structure['column_period_evidence']
    assert flagged_periods(table, facts, source) == ([], 0, 4)
