"""Statements that run on to an untitled next page, and note tables under a statement's title (tables-v3)."""

import unittest

from lxml import html

from sec_pipeline.page_continuation import VERIFICATION_VERSION, in_note, without_note_tables
from sec_pipeline.tables import TableExtractionError, select_statement_segments


GAAP = 'http://fasb.org/us-gaap/2026'
HALF = {'startDate': '2025-01-01', 'endDate': '2025-06-30'}
DATE = {'instant': '2025-06-30'}
OPENING = {'instant': '2024-12-31'}
CASH = 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
BEFORE_TAX = 'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest'
PAGE = '<p>See accompanying notes.</p><p>86</p><p>Table of Contents</p>'
DURATION_HEADER, DATE_HEADER = 'Six Months Ended June 30, 2025', 'June 30, 2025'

INCOME = [('Revenues', 'Revenues', '100', HALF), ('Income before income taxes', BEFORE_TAX, '10', HALF),
          ('Income tax expense', 'IncomeTaxExpenseBenefit', '2', HALF), ('Net income', 'NetIncomeLoss', '8', HALF)]
BALANCE = [('Total assets', 'Assets', '100', DATE), ('Total liabilities', 'Liabilities', '40', DATE),
           ('Total equity', 'StockholdersEquity', '60', DATE),
           ('Total liabilities and equity', 'LiabilitiesAndStockholdersEquity', '100', DATE)]
FLOWS = [('Net cash provided by operating activities', 'NetCashProvidedByUsedInOperatingActivities', '10', HALF),
         ('Net cash used in investing activities', 'NetCashProvidedByUsedInInvestingActivities', '-3', HALF),
         ('Net cash used in financing activities', 'NetCashProvidedByUsedInFinancingActivities', '-2', HALF)]
BALANCES = [('Cash at beginning of period', CASH, '20', OPENING), ('Cash at end of period', CASH, '25', DATE)]
TITLES = {'income': 'Consolidated Statements of Income', 'balance': 'Consolidated Balance Sheets',
          'cash': 'Consolidated Statements of Cash Flows'}


def table(key, lines, *, title=None, header=None, before='', note=False):
    """One table: `title` printed above it, `header` over its value column, `before` any HTML ahead of both."""
    return {'key': key, 'lines': lines, 'title': title, 'header': header, 'before': before, 'note': note}


def whole(kind):
    lines = {'income': INCOME, 'balance': BALANCE, 'cash': FLOWS + BALANCES}[kind]
    return table(kind, lines, title=TITLES[kind], header=DATE_HEADER if kind == 'balance' else DURATION_HEADER)


def document(*tables):
    parts, facts = [], {}
    for item in tables:
        rows = [f'<tr><td></td><td>{item["header"]}</td></tr>'] if item['header'] else []
        for index, (label, concept, value, period, *dimensions) in enumerate(item['lines']):
            identifier = f'{item["key"]}-{index}'
            rows.append(f'<tr><td>{label}</td><td><ix:nonfraction id="{identifier}" name="g:{concept}" '
                        f'contextref="{identifier}" unitref="usd">{value}</ix:nonfraction></td></tr>')
            facts[identifier] = {'xml_id': identifier, 'concept': '{' + GAAP + '}' + concept, 'context_ref': identifier,
                                 'unit_ref': 'usd', 'period': dict(period), 'raw_value': value, 'is_nil': False,
                                 'dimensions': [dict(dimension) for dimension in dimensions],
                                 'unit': {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []},
                                 'entity': {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'}}
        printed = (f'<p>{item["title"]}</p>' if item['title'] else '') + f'<table id="{item["key"]}">{"".join(rows)}</table>'
        if item['note']:
            printed = f'<ix:nonnumeric name="g:NoteTextBlock" contextref="note"><p>Note 4</p>{printed}</ix:nonnumeric>'
        parts.append(item['before'] + printed)
    tree = html.document_fromstring(f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
                                   + ''.join(parts) + '</body></html>').getroottree()
    return tree, facts


def select(*tables, corroborated_layouts=True):
    tree, facts = document(*tables)
    return select_statement_segments(tree, facts, corroborated_layouts=corroborated_layouts)


def keys(*tables, **options):
    return [part.table_key for part in select(*tables, **options)]


class CashFlowPageTests(unittest.TestCase):
    KEYS = ['income_statement', 'balance_sheet', 'cash_flow.segment_1', 'cash_flow.segment_2']

    def parts(self, *, balances=BALANCES, header=DURATION_HEADER, before=PAGE, title=None, note=False, flows=FLOWS, extra=()):
        return (whole('income'), whole('balance'), table('cash', flows, title=TITLES['cash'], header=DURATION_HEADER),
                table('page', [*extra, *balances], title=title, header=header, before=before, note=note))

    def test_an_untitled_table_on_the_next_page_closes_the_statement_when_cash_reconciles(self):
        with self.assertRaisesRegex(TableExtractionError, 'cash_flow: ambiguous or incomplete'):
            select(*self.parts(), corroborated_layouts=False)
        selected = select(*self.parts())
        self.assertEqual([part.table_key for part in selected], self.KEYS)
        first, second = selected[2], selected[3]
        self.assertEqual([first.element.get('id'), second.element.get('id')], ['cash', 'page'])
        proof = first.group['page_continuation']
        self.assertEqual(proof['verification_version'], VERIFICATION_VERSION)
        self.assertEqual(proof['ties'], [{
            'period': HALF,
            'facts': {'operating': 'cash-0', 'investing': 'cash-1', 'financing': 'cash-2', 'opening': 'page-0', 'closing': 'page-1'},
            'raw_values': {'operating': '10', 'investing': '-3', 'financing': '-2', 'opening': '20', 'closing': '25'}}])
        self.assertEqual(first.group['heading'], 'consolidated statements of cash flows')
        self.assertEqual((first.group['segment_index'], second.group['segment_index']), (1, 2))
        self.assertNotIn('header_inheritance', second.group)

    def test_a_table_without_column_headers_takes_the_periods_of_the_first(self):
        selected = select(*self.parts(header=None))
        self.assertEqual([part.table_key for part in selected], self.KEYS)
        inheritance = selected[3].group['header_inheritance']
        self.assertEqual(inheritance['source_table_key'], 'cash_flow.segment_1')
        self.assertEqual({column: value['period'] for column, value in inheritance['columns'].items()}, {'1': HALF})
        self.assertNotIn('header_inheritance', selected[2].group)

    def test_the_exchange_rate_effect_and_a_printed_net_change_are_part_of_the_reconciliation(self):
        effect = ('Effect of exchange rates', 'EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
                  '1', HALF)
        change = ('Net increase in cash', CASH + 'PeriodIncreaseDecreaseIncludingExchangeRateEffect', '6', HALF)
        closing = [BALANCES[0], ('Cash at end of period', CASH, '26', DATE)]
        selected = select(*self.parts(extra=(effect, change), balances=closing))
        self.assertEqual(selected[2].group['page_continuation']['ties'][0]['facts'], {
            'operating': 'cash-0', 'investing': 'cash-1', 'financing': 'cash-2', 'exchange_rate_effect': 'page-0',
            'opening': 'page-2', 'closing': 'page-3', 'net_change': 'page-1'})
        excluding = ('Net increase in cash', CASH + 'PeriodIncreaseDecreaseExcludingExchangeRateEffect', '5', HALF)
        self.assertEqual(keys(*self.parts(extra=(effect, excluding), balances=closing)), self.KEYS)
        for wrong in (('Net increase in cash', change[1], '7', HALF), ('Net increase in cash', excluding[1], '6', HALF)):
            with self.subTest(wrong=wrong), self.assertRaises(TableExtractionError):
                select(*self.parts(extra=(effect, wrong), balances=closing))

    def test_cash_that_does_not_reconcile_a_title_a_note_or_anything_else_between_is_refused(self):
        numbers = '<table><tr><td>Other</td><td><ix:nonfraction name="g:OtherAssets">5</ix:nonfraction></td></tr></table>'
        refused = (
            {'balances': [BALANCES[0], ('Cash at end of period', CASH, '26', DATE)]},
            {'balances': [('Cash at beginning of period', CASH, '20', {'instant': '2025-01-01'}), BALANCES[1]]},
            {'balances': BALANCES[1:]},
            {'title': 'Consolidated Statements of Comprehensive Income'}, {'title': 'Consolidated Balance Sheets'},
            {'note': True}, {'before': PAGE + numbers}, {'before': '<p>Note 2. Summary of significant accounting policies</p>'},
            {'before': '<p>Consolidated Balance Sheets</p>'}, {'before': '<p>' + 'Narrative. ' * 300 + '</p>'},
            {'header': 'Three Months Ended June 30, 2025'},
        )
        for options in refused:
            with self.subTest(options=options), self.assertRaises(TableExtractionError):
                select(*self.parts(**options))


class BalanceSheetPageTests(unittest.TestCase):
    KEYS = ['income_statement', 'balance_sheet.segment_1', 'balance_sheet.segment_2', 'cash_flow']

    def parts(self, *, total='100', header=DATE_HEADER, before=PAGE):
        liabilities = [*BALANCE[1:3], ('Total liabilities and equity', 'LiabilitiesAndStockholdersEquity', total, DATE)]
        return (whole('income'), table('assets', BALANCE[:1], title=TITLES['balance'], header=DATE_HEADER),
                table('liabilities', liabilities, header=header, before=before), whole('cash'))

    def test_untitled_liabilities_and_equity_on_the_next_page_tie_to_total_assets(self):
        with self.assertRaisesRegex(TableExtractionError, 'balance_sheet: ambiguous or incomplete'):
            select(*self.parts(), corroborated_layouts=False)
        selected = select(*self.parts())
        self.assertEqual([part.table_key for part in selected], self.KEYS)
        group = selected[2].group
        self.assertEqual(group['page_continuation']['verification_version'], VERIFICATION_VERSION)
        self.assertEqual(group['balance_continuation']['totals'][0]['raw_values'],
                         {'Assets': '100', 'LiabilitiesAndStockholdersEquity': '100'})
        self.assertEqual([part.element.get('id') for part in selected[1:3]], ['assets', 'liabilities'])

    def test_headerless_columns_take_the_date_and_other_totals_are_refused(self):
        selected = select(*self.parts(header=None))
        self.assertEqual({column: value['period'] for column, value in selected[2].group['header_inheritance']['columns'].items()},
                         {'1': DATE})
        for options in ({'total': '101'}, {'header': 'December 31, 2024'}, {'before': '<p>Note 3. Inventories</p>'}):
            with self.subTest(options=options), self.assertRaises(TableExtractionError):
                select(*self.parts(**options))


class IncomeStatementPageTests(unittest.TestCase):
    KEYS = ['income_statement.segment_1', 'income_statement.segment_2', 'balance_sheet', 'cash_flow']

    def parts(self, *, net='8', concept='NetIncomeLoss', first=INCOME[:3]):
        return (table('income', first, title=TITLES['income'], header=DURATION_HEADER),
                table('page', [('Net income', concept, net, HALF)], header=DURATION_HEADER, before=PAGE),
                whole('balance'), whole('cash'))

    def test_net_income_on_the_next_page_equals_income_before_taxes_less_the_tax(self):
        with self.assertRaisesRegex(TableExtractionError, 'income_statement: ambiguous or incomplete'):
            select(*self.parts(), corroborated_layouts=False)
        selected = select(*self.parts())
        self.assertEqual([part.table_key for part in selected], self.KEYS)
        self.assertEqual(selected[0].group['page_continuation']['ties'], [{
            'period': HALF, 'facts': {'before_income_taxes': 'income-1', 'income_tax': 'income-2', 'net_income': 'page-0'},
            'raw_values': {'before_income_taxes': '10', 'income_tax': '2', 'net_income': '8'}}])
        self.assertEqual(keys(*self.parts(concept='ProfitLoss')), self.KEYS)

    def test_another_amount_or_a_first_page_without_the_tax_line_is_refused(self):
        for options in ({'net': '9'}, {'first': INCOME[:2]}, {'first': [INCOME[0], INCOME[2]]}):
            with self.subTest(options=options), self.assertRaises(TableExtractionError):
                select(*self.parts(**options))


class ContinuedPageDetailTests(unittest.TestCase):
    """A page marked "(continued)" that adds rows of dimensional detail to the cash flow statement."""
    KEYS = ['income_statement', 'balance_sheet', 'cash_flow.segment_1', 'cash_flow.segment_2']
    SEGMENT = {'dimension': '{' + GAAP + '}StatementBusinessSegmentsAxis', 'member': '{http://example.com/2025}HomebuildingMember'}
    OTHER_ENTITY = {'dimension': '{http://xbrl.sec.gov/dei/2026}LegalEntityAxis', 'member': '{http://example.com/2025}SubsidiaryMember'}

    def parts(self, *, detail, second=FLOWS[2:] + BALANCES):
        return (whole('income'), whole('balance'), table('cash', FLOWS[:2], title=TITLES['cash'], header=DURATION_HEADER),
                table('cash-continued', [*second, *detail], title=TITLES['cash'] + ' (continued)', header=DURATION_HEADER,
                      before='<p>See notes.</p>'))

    def test_detail_with_dimensions_on_a_continued_page_no_longer_separates_it(self):
        detail = [('Homebuilding cash', 'CashAndCashEquivalentsAtCarryingValue', '15', DATE, self.SEGMENT)]
        self.assertEqual(keys(*self.parts(detail=[])), self.KEYS)
        with self.assertRaisesRegex(TableExtractionError, 'scope'):
            select(*self.parts(detail=detail), corroborated_layouts=False)
        selected = select(*self.parts(detail=detail))
        self.assertEqual([part.table_key for part in selected], self.KEYS)
        [scope] = selected[2].group['detail_scopes']
        self.assertIn('HomebuildingMember', scope)
        self.assertEqual(selected[2].group['scope']['dimensions'], [])
        self.assertNotIn('detail_scopes', select(*self.parts(detail=[]))[2].group)

    def test_another_legal_entity_or_a_total_reported_only_with_a_dimension_is_refused(self):
        other_entity = [('Subsidiary cash', 'CashAndCashEquivalentsAtCarryingValue', '15', DATE, self.OTHER_ENTITY)]
        financing = ('Net cash used in financing activities', 'NetCashProvidedByUsedInFinancingActivities', '-2', HALF, self.SEGMENT)
        plain = ('Other financing', 'ProceedsFromPaymentsForOtherFinancingActivities', '-1', HALF)
        for options in ({'detail': other_entity}, {'detail': [], 'second': [financing, plain, *BALANCES]}):
            with self.subTest(options=options), self.assertRaises(TableExtractionError):
                select(*self.parts(**options))


class NoteTableTests(unittest.TestCase):
    KEYS = ['income_statement', 'balance_sheet', 'cash_flow.segment_1', 'cash_flow.segment_2']
    DETAIL = [('Deferred revenue', 'ContractWithCustomerLiabilityCurrent', '5', DATE)]

    def parts(self, *, lines=DETAIL, note=True, title=TITLES['balance']):
        # A cash flow statement marked "(continued)" sends every kind through
        # the selection of physical segments, where each titled table is a candidate.
        return (whole('income'), whole('balance'),
                table('cash', FLOWS[:2], title=TITLES['cash'], header=DURATION_HEADER),
                table('cash-continued', FLOWS[2:] + BALANCES, title=TITLES['cash'] + ' (continued)', header=DURATION_HEADER,
                      before='<p>See notes.</p>'),
                table('detail', lines, title=title, header=DATE_HEADER, note=note))

    def test_a_note_table_under_the_statements_title_that_holds_none_of_its_totals_is_left_out(self):
        with self.assertRaisesRegex(TableExtractionError, 'balance_sheet: ambiguous or incomplete'):
            select(*self.parts(), corroborated_layouts=False)
        selected = select(*self.parts())
        self.assertEqual([part.table_key for part in selected], self.KEYS)
        self.assertEqual(selected[1].element.get('id'), 'balance')
        self.assertIsNone(selected[1].group)

    def test_the_same_table_outside_a_note_or_one_that_holds_a_total_still_makes_the_statement_ambiguous(self):
        for options in ({'note': False}, {'lines': [('Total assets', 'Assets', '100', DATE)]}, {'lines': BALANCE}):
            with self.subTest(options=options), self.assertRaises(TableExtractionError):
                select(*self.parts(**options))

    def test_only_tables_inside_a_tagged_text_block_count_as_note_tables(self):
        tree, _ = document(*self.parts())
        inside, outside = tree.xpath('//table[@id="detail"]')[0], tree.xpath('//table[@id="balance"]')[0]
        self.assertTrue(in_note(inside))
        self.assertFalse(in_note(outside))
        empty, total = {'coverage': {'a': set()}, 'continued': False, 'table': inside}, {'coverage': {'a': {0}}, 'continued': False, 'table': inside}
        continued = {'coverage': {'a': set()}, 'continued': True, 'table': inside}
        printed = {'coverage': {'a': set()}, 'continued': False, 'table': outside}
        self.assertEqual(without_note_tables([empty, total, continued, printed]), [total, continued, printed])


if __name__ == '__main__':
    unittest.main()
