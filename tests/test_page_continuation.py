"""Statements that run on to an untitled next page, and note tables under a statement's title (tables-v3)."""

import json
import unittest

from lxml import html

from sec_pipeline.balance_segments import whole_balance_sheet
from sec_pipeline.page_continuation import (
    VERIFICATION_VERSION, in_note, statement_before_supplements, with_own_scope, without_note_tables,
)
from sec_pipeline.tables import TableExtractionError, _bare_titles, select_statement_segments


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

    def test_the_same_table_outside_a_note_still_makes_the_statement_ambiguous(self):
        for options in ({'note': False}, {'note': False, 'lines': BALANCE}):
            with self.subTest(options=options), self.assertRaises(TableExtractionError):
                select(*self.parts(**options))

    def test_the_last_attempt_looks_for_the_statements_outside_the_notes_only(self):
        # A table inside a tagged text block is a note whatever it holds: a statement total,
        # a whole second balance sheet, or columns no header dates.
        unreadable = table('detail', [('Total assets', 'Assets', '100', DATE)], title=TITLES['balance'], header='Real Estate', note=True)
        for parts in (self.parts(lines=[('Total assets', 'Assets', '100', DATE)]), self.parts(lines=BALANCE),
                      (*self.parts()[:4], unreadable)):
            with self.subTest(detail=parts[4]['lines']):
                selected = select(*parts)
                self.assertEqual([part.table_key for part in selected], self.KEYS)
                self.assertEqual(selected[1].element.get('id'), 'balance')
                self.assertIsNone(selected[1].group)
        with self.assertRaisesRegex(TableExtractionError, 'unresolved column period'):
            select(*self.parts()[:4], {**unreadable, 'note': False})

    def test_only_tables_inside_a_tagged_text_block_count_as_note_tables(self):
        tree, _ = document(*self.parts())
        inside, outside = tree.xpath('//table[@id="detail"]')[0], tree.xpath('//table[@id="balance"]')[0]
        self.assertTrue(in_note(inside))
        self.assertFalse(in_note(outside))
        empty, total = {'coverage': {'a': set()}, 'continued': False, 'table': inside}, {'coverage': {'a': {0}}, 'continued': False, 'table': inside}
        continued = {'coverage': {'a': set()}, 'continued': True, 'table': inside}
        printed = {'coverage': {'a': set()}, 'continued': False, 'table': outside}
        self.assertEqual(without_note_tables([empty, total, continued, printed]), [total, continued, printed])


class OtherScopeTests(unittest.TestCase):
    """A second statement under the same title, every number of which carries a dimension."""

    KEYS = ['income_statement', 'balance_sheet', 'cash_flow']
    GROUP = {'kind': 'explicit', 'dimension': '{' + GAAP + '}ConsolidationItemsAxis', 'member': '{http://example.com}ManufacturingGroupMember'}
    PARTNERSHIP = {'kind': 'explicit', 'dimension': '{http://xbrl.sec.gov/dei/2026}LegalEntityAxis',
                   'member': '{http://example.com}OperatingPartnershipMember'}

    def parts(self, *, dimension=GROUP, title=TITLES['cash'] + ' (Continued)', header=DURATION_HEADER, before=PAGE, lines=None,
              first=None):
        lines = FLOWS + BALANCES if lines is None else lines
        page = [(*line, dimension) if dimension else line for line in lines]
        return (whole('income'), whole('balance'), first or whole('cash'),
                table('other', page, title=title, header=header, before=before))

    def test_the_table_with_the_registrants_own_numbers_is_the_statement(self):
        with self.assertRaises(TableExtractionError):
            select(*self.parts(), corroborated_layouts=False)
        cases = ({},                                                        # the statement again for a business group
                 {'dimension': self.PARTNERSHIP, 'title': TITLES['cash']},  # the statement of a second registrant
                 {'header': 'Three Months Ended June 30, 2025'})
        for options in cases:
            with self.subTest(options=options):
                selected = select(*self.parts(**options))
                self.assertEqual([part.table_key for part in selected], self.KEYS)
                self.assertEqual(selected[2].element.get('id'), 'cash')
                self.assertIsNone(selected[2].group)

    def test_a_second_table_of_the_whole_entity_or_an_incomplete_first_table_is_refused(self):
        incomplete = table('cash', FLOWS + BALANCES[:1], title=TITLES['cash'], header=DURATION_HEADER)
        for options in ({'dimension': None}, {'dimension': None, 'title': TITLES['cash']}, {'first': incomplete}):
            with self.subTest(options=options), self.assertRaises(TableExtractionError):
                select(*self.parts(**options))

    def test_only_tables_without_a_number_of_the_registrants_own_scope_are_left_out(self):
        own = {'scopes': {json.dumps({'entity': 'e', 'unit': 'u', 'dimensions': []})}}
        detail = {'scopes': {json.dumps({'entity': 'e', 'unit': 'u', 'dimensions': [self.GROUP]})}}
        mixed = {'scopes': own['scopes'] | detail['scopes']}
        empty = {'scopes': set()}
        self.assertEqual(with_own_scope([own, detail, mixed, empty]), [own, mixed, empty])
        # Nothing is left out when no table holds a number without a dimension.
        self.assertEqual(with_own_scope([detail, empty]), [detail, empty])


class SupplementPagesTests(unittest.TestCase):
    """A complete cash flow statement, then pages under its title that only supplement it."""

    KEYS = ['income_statement', 'balance_sheet', 'cash_flow']
    INTEREST = ('Cash paid for interest', 'InterestPaidNet', '3', HALF)
    RESTRICTED = ('Restricted cash', 'RestrictedCash', '5', DATE)

    def parts(self, *, lines=(RESTRICTED,), title=TITLES['cash'] + ' (continued)', header=DATE_HEADER, before=PAGE, first=None,
              note=False):
        return (whole('income'), whole('balance'), first or whole('cash'),
                table('supplement', list(lines), title=title, header=header, before=before, note=note))

    def test_the_complete_table_is_the_statement(self):
        with self.assertRaises(TableExtractionError):
            select(*self.parts(), corroborated_layouts=False)
        cases = ({},                                                                                # a continuation dated differently
                 {'lines': (self.INTEREST,), 'title': TITLES['cash'], 'header': DURATION_HEADER},   # the title repeated, unmarked
                 {'lines': (self.RESTRICTED, BALANCES[1])})                                         # the closing balance once more
        for options in cases:
            with self.subTest(options=options):
                selected = select(*self.parts(**options))
                self.assertEqual([part.table_key for part in selected], self.KEYS)
                self.assertEqual(selected[2].element.get('id'), 'cash')
                self.assertIsNone(selected[2].group)

    def test_a_page_with_an_activity_total_or_an_incomplete_statement_is_refused(self):
        incomplete = table('cash', FLOWS + BALANCES[:1], title=TITLES['cash'], header=DURATION_HEADER)
        quarter = (FLOWS[2][0], FLOWS[2][1], '-1', {'startDate': '2025-04-01', 'endDate': '2025-06-30'})
        refused = ({'lines': (quarter,), 'header': 'Three Months Ended June 30, 2025'},   # financing activities of another period
                   {'first': incomplete},                                  # the first table lacks the closing balance
                   {'before': '<p>Note 2. Summary of significant accounting policies</p>'},
                   {'before': PAGE + '<table><tr><td>Other</td><td><ix:nonfraction name="g:OtherAssets">5</ix:nonfraction></td></tr></table>'})
        for options in refused:
            with self.subTest(options=options), self.assertRaises(TableExtractionError):
                select(*self.parts(**options))

    def test_the_rule_reads_only_cash_flow_candidates(self):
        tree, _ = document(*self.parts())
        main = {'continued': False, 'axes': ['a'], 'headings': {'t'}, 'coverage': {'a': {0, 1, 2, 3}}, 'scopes': set(),
                'table': tree.xpath('//table[@id="cash"]')[0]}
        other = {'continued': True, 'axes': [], 'headings': {'t'}, 'coverage': {}, 'scopes': set(),
                 'table': tree.xpath('//table[@id="supplement"]')[0]}
        self.assertIs(statement_before_supplements(tree, 'cash_flow', [main, other], {0, 1, 2, 3}), main)
        self.assertIsNone(statement_before_supplements(tree, 'balance_sheet', [main, other], {0, 1, 2, 3}))
        self.assertIsNone(statement_before_supplements(tree, 'cash_flow', [main], {0, 1, 2, 3}))
        self.assertIsNone(statement_before_supplements(tree, 'cash_flow', [main, {**other, 'headings': {'another'}}], {0, 1, 2, 3}))


class LastAttemptTests(unittest.TestCase):
    """Readings only the last selection attempts take."""

    SPLIT = ['income_statement', 'balance_sheet', 'cash_flow.segment_1', 'cash_flow.segment_2']
    PARENT = {'kind': 'explicit', 'dimension': '{' + GAAP + '}ConsolidatedEntitiesAxis', 'member': '{' + GAAP + '}ParentCompanyMember'}

    def split_cash(self):
        # A cash flow statement marked "(continued)" sends every kind through the selection of physical segments.
        return (table('cash', FLOWS[:2], title=TITLES['cash'], header=DURATION_HEADER),
                table('cash-continued', FLOWS[2:] + BALANCES, title=TITLES['cash'] + ' (continued)', header=DURATION_HEADER,
                      before='<p>See notes.</p>'))

    def test_rows_of_the_parent_company_are_detail_of_a_consolidated_statement(self):
        loans = ('Loans held by the parent', 'LoansAndLeasesReceivableNetReportedAmount', '5', DATE, self.PARENT)
        balance = table('balance', [loans, *BALANCE], title=TITLES['balance'], header=DATE_HEADER)
        with self.assertRaisesRegex(TableExtractionError, 'balance_sheet: ambiguous or incomplete'):
            select(whole('income'), balance, *self.split_cash(), corroborated_layouts=False)
        selected = select(whole('income'), balance, *self.split_cash())
        self.assertEqual([part.table_key for part in selected], self.SPLIT)
        self.assertEqual(selected[1].element.get('id'), 'balance')
        # A table of parent-company numbers alone is the parent's own statement and no candidate.
        parent_only = table('balance', [(*line, self.PARENT) for line in BALANCE], title=TITLES['balance'], header=DATE_HEADER)
        with self.assertRaises(TableExtractionError):
            select(whole('income'), parent_only, *self.split_cash())

    def test_a_balance_sheet_whose_two_sides_agree_needs_no_standard_equity_total(self):
        def parts(total):
            lines = [BALANCE[0], BALANCE[1], ('Total common shareholders equity', 'CommonShareholdersEquityOfTheCompany', '60', DATE),
                     ('Total liabilities and equity', 'LiabilitiesAndStockholdersEquity', total, DATE)]
            return whole('income'), table('balance', lines, title=TITLES['balance'], header=DATE_HEADER), whole('cash')

        with self.assertRaisesRegex(TableExtractionError, 'balance_sheet: ambiguous or incomplete'):
            select(*parts('100'), corroborated_layouts=False)
        selected = select(*parts('100'))
        self.assertEqual([part.table_key for part in selected], ['income_statement', 'balance_sheet', 'cash_flow'])
        self.assertIsNone(selected[1].group)
        with self.assertRaisesRegex(TableExtractionError, 'balance_sheet: ambiguous or incomplete'):
            select(*parts('99'))

    def test_whole_balance_sheet_needs_one_unmarked_table_alone_under_its_title(self):
        def candidate(total='100', continued=False, headings=('consolidated balance sheets',)):
            reference = {'dimensions': [], 'is_nil': False, 'period': DATE, 'unit': {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []},
                         'entity': {'scheme': 'http://www.sec.gov/CIK', 'identifier': '1'}}
            axis = json.dumps(DATE, sort_keys=True)
            return {'continued': continued, 'axes': [axis], 'headings': set(headings), 'table': object(), 'references': {axis: [
                {**reference, 'concept': '{' + GAAP + '}Assets', 'raw_value': '100'},
                {**reference, 'concept': '{' + GAAP + '}LiabilitiesAndStockholdersEquity', 'raw_value': total}]}}

        whole_table = candidate()
        self.assertIs(whole_balance_sheet([whole_table])[0].element, whole_table['table'])
        self.assertIsNone(whole_balance_sheet([candidate(total='99')]))
        self.assertIsNone(whole_balance_sheet([candidate(continued=True)]))
        self.assertIsNone(whole_balance_sheet([whole_table, candidate()]))
        self.assertIsNone(whole_balance_sheet([whole_table, candidate(total='99')]))
        self.assertIsNotNone(whole_balance_sheet([whole_table, candidate(total='99', headings=('statement of net assets',))]))

    def test_a_title_printed_after_the_registrants_name_is_the_same_title(self):
        second = table('cash-2', FLOWS[2:] + BALANCES, title='Example Corp and Subsidiaries ' + TITLES['cash'], header=DURATION_HEADER,
                       before='<p>See notes.</p>')
        tables = (whole('income'), whole('balance'), table('cash', FLOWS[:2], title=TITLES['cash'], header=DURATION_HEADER), second)
        tree, facts = document(*tables)
        facts['name'] = {**facts['cash-0'], 'xml_id': 'name', 'concept': '{http://xbrl.sec.gov/dei/2026}EntityRegistrantName',
                         'raw_value': 'Example Corp', 'unit': None, 'unit_ref': None}
        with self.assertRaises(TableExtractionError):
            select_statement_segments(tree, facts)
        selected = select_statement_segments(tree, facts, corroborated_layouts=True)
        self.assertEqual([part.table_key for part in selected], self.SPLIT)
        self.assertEqual(selected[2].group['heading'], 'consolidated statements of cash flows')
        self.assertEqual(_bare_titles({'example corp and subsidiaries consolidated statements of cash flows', 'example corp'},
                                      ('example corp',)), {'consolidated statements of cash flows', 'example corp'})


if __name__ == '__main__':
    unittest.main()
