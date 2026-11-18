"""Column headers and cash-flow balances that tables-v3 accepts on corroboration."""

import inspect
import json
import unittest

from lxml import html

from sec_pipeline import filing_adapter_v3
from sec_pipeline.statement_headings import recover_headings, registrant_names, statement_kind
from sec_pipeline.tables import (
    TableExtractionError, _CORROBORATED_STATEMENT_CONCEPTS, _STATEMENT_CONCEPTS, _column_periods,
    _endpoint_balance_period, _printed_period_tokens, _segment_candidate, extract_tables, layout_grid,
    select_statement_segments, validate_statement_segments,
)
from sec_pipeline.tables_bridge import parse_tables


GAAP = 'http://fasb.org/us-gaap/2026'
CONCEPTS = ('Revenues', 'NetIncomeLoss')


def resolve(header_rows, column_periods, *, corroborate=True, extra_facts=()):
    """One value column per period; header rows are given exactly as printed.

    Returns ({column: period}, provenance). `extra_facts` adds undimensioned
    facts elsewhere in the filing, such as a reported balance.
    """
    headers = ''.join('<tr><td></td>' + ''.join(f'<td>{value}</td>' for value in row) + '</tr>' for row in header_rows)
    body = ''.join(
        f'<tr><td>{concept}</td>' + ''.join(
            f'<td><ix:nonfraction id="f{column}{index}" name="g:{concept}" contextref="c{column}" unitref="usd">1'
            f'</ix:nonfraction></td>' for column in range(1, len(column_periods) + 1)) + '</tr>'
        for index, concept in enumerate(CONCEPTS))
    tree = html.document_fromstring(
        f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body><table>{headers}{body}'
        f'</table></body></html>').getroottree()
    facts, ids = {}, {}
    for column, period in enumerate(column_periods, 1):
        for index, concept in enumerate(CONCEPTS):
            key = f'f{column}{index}'
            ids[key] = 1
            facts[key] = {'xml_id': key, 'concept': '{' + GAAP + '}' + concept, 'context_ref': f'c{column}',
                          'unit_ref': 'usd', 'period': dict(period), 'raw_value': '1', 'is_nil': False}
    for index, period in enumerate(extra_facts):
        facts[f'x{index}'] = {'xml_id': f'x{index}', 'concept': '{' + GAAP + '}Cash', 'context_ref': f'x{index}',
                              'unit_ref': 'usd', 'period': dict(period), 'raw_value': '1', 'is_nil': False}
    rows, _ = layout_grid(tree.xpath('//table')[0], tree)
    return _column_periods(rows, len(header_rows), facts, {'g': GAAP}, ids, advanced=True, corroborate=corroborate)


def duration(start, end):
    return {'startDate': start, 'endDate': end}


class PrintedDurationTests(unittest.TestCase):
    HALF_2025 = duration('2025-01-01', '2025-06-30')

    def test_duration_and_year_without_a_date_take_the_tagged_period(self):
        periods, provenance = resolve([['Six Months'], ['2025']], [self.HALF_2025])
        self.assertEqual(periods[1], self.HALF_2025)
        proof = provenance['1']
        self.assertEqual((proof['method'], proof['anchor']), ('corroborated_printed_header', 'printed_duration'))
        self.assertEqual(proof['xml_ids'], ['f10', 'f11'])

    def test_the_same_header_is_unresolved_without_the_option(self):
        periods, provenance = resolve([['Six Months'], ['2025']], [self.HALF_2025], corroborate=False)
        self.assertIsNone(periods[1])
        self.assertEqual(provenance, {})

    def test_hyphenated_periods_quarters_halves_and_fiscal_weeks(self):
        cases = (
            (['Three-Month Period Ended', 'March 28, 2025'], duration('2025-01-01', '2025-03-28')),
            (['Second Quarter', '2025'], duration('2025-03-30', '2025-06-28')),
            (['First Two Quarters of', '2025'], duration('2024-12-29', '2025-06-28')),
            (['For the periods ended June 30,', '2025', 'First Half'], self.HALF_2025),
            (['Fiscal Year', '2025', '(52 weeks)'], duration('2024-12-29', '2025-12-27')),
            (['Quarter Ended', 'June 2025'], duration('2025-04-01', '2025-06-30')),
        )
        for header, period in cases:
            with self.subTest(header=header):
                periods, provenance = resolve([[value] for value in header], [period])
                self.assertEqual(periods[1], period)
                self.assertEqual(provenance['1']['anchor'], 'printed_duration')

    def test_a_printed_length_year_or_date_that_differs_is_never_overridden(self):
        cases = (
            (['Six Months', '2025'], duration('2025-04-01', '2025-06-30')),                 # three months tagged
            (['Six Months', '2024'], self.HALF_2025),                                       # another year
            (['Three-Month Period Ended', 'March 28, 2025'], duration('2025-01-01', '2025-03-29')),  # another day
            (['Quarter Ended', 'June 2025'], duration('2025-05-01', '2025-07-31')),         # another month
            (['Fiscal Year', '2025', '(53 weeks)'], duration('2024-12-29', '2025-12-27')),  # 52 weeks tagged
            (['Three and Six Months', '2025'], self.HALF_2025),                             # two durations printed
            (['First Two Quarters of', '2025'], duration('2024-12-29', '2025-06-14')),      # 24 weeks is not six months
            (['Six Months'], self.HALF_2025),                                               # no year printed
        )
        for header, period in cases:
            with self.subTest(header=header):
                periods, provenance = resolve([[value] for value in header], [period])
                self.assertNotEqual(periods[1], period)
                self.assertNotIn('1', provenance)


class ComparativeColumnTests(unittest.TestCase):
    CURRENT = duration('2025-01-01', '2025-03-31')
    PRIOR = duration('2024-01-01', '2024-03-31')

    def test_a_dated_column_beside_an_established_one_takes_the_same_length_a_year_earlier(self):
        periods, provenance = resolve([['Three Months Ended March 31, 2025', 'March 31, 2024']], [self.CURRENT, self.PRIOR])
        self.assertEqual((periods[1], periods[2]), (self.CURRENT, self.PRIOR))
        self.assertNotIn('1', provenance)
        proof = provenance['2']
        self.assertEqual(proof['anchor'], 'comparative_column')
        self.assertEqual((proof['comparative_column'], proof['comparative_period']), (1, self.CURRENT))

    def test_fiscal_quarters_a_few_days_apart_still_compare(self):
        current, prior = duration('2024-12-29', '2025-03-29'), duration('2023-12-31', '2024-03-30')
        periods, _ = resolve([['13 Weeks Ended March 29, 2025', 'March 30, 2024']], [current, prior])
        self.assertEqual(periods[2], prior)

    def test_another_length_or_distance_is_not_a_comparative(self):
        for prior in (duration('2023-10-01', '2024-03-31'),      # six months
                      duration('2023-01-01', '2023-03-31')):     # two years earlier, date would not match anyway
            with self.subTest(prior=prior):
                periods, provenance = resolve([['Three Months Ended March 31, 2025', 'March 31, 2024']], [self.CURRENT, prior])
                self.assertNotEqual(periods[2], prior)
                self.assertNotIn('2', provenance)

    def test_bare_years_with_no_established_column_stay_unresolved(self):
        periods, provenance = resolve([['2025', '2024']], [self.CURRENT, self.PRIOR])
        self.assertEqual((periods[1], periods[2]), (None, None))
        self.assertEqual(provenance, {})

    def test_a_chain_reaches_the_third_year(self):
        years = [duration('2024-12-29', '2025-12-27'), duration('2023-12-31', '2024-12-28'), duration('2023-01-01', '2023-12-30')]
        periods, provenance = resolve([['Fiscal Year Ended December 27, 2025', 'December 28, 2024', 'December 30, 2023']], years)
        self.assertEqual([periods[column] for column in (1, 2, 3)], years)
        self.assertEqual(provenance['3']['comparative_column'], 2)


class YearToDateAndBalanceTests(unittest.TestCase):
    def test_year_to_date_needs_the_balance_reported_the_day_before(self):
        period = duration('2025-01-04', '2025-06-27')
        header = [['Year to Date'], ['2025']]
        periods, provenance = resolve(header, [period], extra_facts=[{'instant': '2025-01-03'}])
        self.assertEqual(periods[1], period)
        self.assertEqual(provenance['1']['anchor'], 'year_to_date_from_reported_balance')
        periods, provenance = resolve(header, [period], extra_facts=[{'instant': '2024-12-31'}])
        self.assertIsNone(periods[1])
        self.assertEqual(provenance, {})

    def test_a_balance_column_takes_its_tagged_date_only_when_it_is_printed_in_full(self):
        balance = {'instant': '2025-12-28'}
        periods, provenance = resolve([['Fiscal Year Ended'], ['December 28, 2025']], [balance])
        self.assertEqual(periods[1], balance)
        self.assertEqual(provenance['1']['anchor'], 'printed_balance_date')
        periods, provenance = resolve([['As of'], ['Fourth Quarter of'], ['2025']], [balance])
        self.assertNotEqual(periods[1], balance)
        self.assertEqual(provenance, {})


class PrintedTokenTests(unittest.TestCase):
    def test_tokens_hold_only_what_is_printed(self):
        tokens = _printed_period_tokens(['Three MonthsEnded March', '2025'])
        self.assertEqual((tokens['years'], tokens['months'], tokens['month_only'], tokens['month_days']),
                         ({2025}, 3, {3}, set()))
        tokens = _printed_period_tokens(['Year to Date', '6/30/2025'])
        self.assertTrue(tokens['year_to_date'])
        self.assertIsNone(tokens['months'])
        self.assertEqual({value.isoformat() for value in tokens['full_dates']}, {'2025-06-30'})
        self.assertIsNone(_printed_period_tokens(['39 Weeks', '40 Weeks', '2025']))


class EndpointBalanceTests(unittest.TestCase):
    HEADER = duration('2025-01-01', '2025-06-30')

    def reference(self, instant, concept='RestrictedCash'):
        return {'period': {'instant': instant}, 'concept': '{' + GAAP + '}' + concept}

    def test_a_balance_on_the_last_day_or_the_day_before_the_first_is_accepted(self):
        self.assertEqual(_endpoint_balance_period(self.reference('2025-06-30'), 'Restricted cash', self.HEADER),
                         {'instant': '2025-06-30'})
        self.assertEqual(_endpoint_balance_period(self.reference('2024-12-31'), 'Cash and equivalents', self.HEADER),
                         {'instant': '2024-12-31'})
        self.assertEqual(_endpoint_balance_period(self.reference('2025-06-30'), '', self.HEADER), {'instant': '2025-06-30'})

    def test_a_label_naming_the_other_endpoint_any_other_date_and_balance_columns_are_refused(self):
        refused = (
            (self.reference('2025-06-30'), 'Cash at beginning of period', self.HEADER),
            (self.reference('2025-06-30'), 'Opening balance', self.HEADER),
            (self.reference('2024-12-31'), 'Cash at end of period', self.HEADER),
            (self.reference('2024-12-31'), 'Ending balance', self.HEADER),
            (self.reference('2025-01-01'), 'Restricted cash', self.HEADER),       # first day, not the day before
            (self.reference('2025-03-31'), 'Restricted cash', self.HEADER),
            (self.reference('2025-06-30'), 'Restricted cash', {'instant': '2025-06-30'}),
            ({'period': dict(self.HEADER), 'concept': '{' + GAAP + '}RestrictedCash'}, 'Restricted cash', self.HEADER),
            # A liability on a row that reports a flow is a tagging error, not a cash balance.
            (self.reference('2025-06-30', 'DividendsPayableCurrentAndNoncurrent'),
             'Issuance of common shares for dividend reinvestment', self.HEADER),
            (self.reference('2025-06-30', 'NoncashOrPartNoncashAcquisitionValueOfAssetsAcquired1'), 'Assets acquired', self.HEADER),
        )
        for reference, label, header in refused:
            with self.subTest(label=label, reference=reference, header=header):
                self.assertIsNone(_endpoint_balance_period(reference, label, header))


HALF = duration('2025-01-01', '2025-06-30')
DISPOSAL_CASH = 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations'


def filing(*, income_header='Six Months Ended June 30, 2025', cash_concept='CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
           closing_label='Cash and cash equivalents at end of period', closing_date='2025-06-30',
           income_title='Consolidated Statements of Income', registrant=None, split_balance=None):
    """Three ordinary statements, each one table under its printed title.

    `split_balance` prints the balance sheet as an assets table and a
    liabilities-and-equity table: `between` is the HTML between them, `title`
    the second table's title and `total` its liabilities-and-equity total.
    """
    definitions = [
        ('income', income_title, income_header,
         [('Revenues', 'Revenues', '100', HALF), ('Operating income', 'OperatingIncomeLoss', '10', HALF),
          ('Net income', 'NetIncomeLoss', '8', HALF)]),
        ('balance', 'Consolidated Balance Sheets', 'June 30, 2025',
         [(name, name, value, {'instant': '2025-06-30'})
          for name, value in (('Assets', '100'), ('Liabilities', '40'), ('StockholdersEquity', '60'))]),
        ('cash', 'Consolidated Statements of Cash Flows', 'Six Months Ended June 30, 2025',
         [('Net cash provided by operating activities', 'NetCashProvidedByUsedInOperatingActivities', '10', HALF),
          ('Net cash used in investing activities', 'NetCashProvidedByUsedInInvestingActivities', '-3', HALF),
          ('Net cash used in financing activities', 'NetCashProvidedByUsedInFinancingActivities', '-2', HALF),
          ('Cash and cash equivalents at beginning of period', cash_concept, '20', {'instant': '2024-12-31'}),
          (closing_label, cash_concept, '25', {'instant': closing_date})]),
    ]
    if split_balance is not None:
        date = {'instant': '2025-06-30'}
        title = split_balance.get('title', 'Consolidated Balance Sheets')
        definitions[1:2] = [
            ('assets', 'Consolidated Balance Sheets', 'June 30, 2025', [('Total assets', 'Assets', '100', date)]),
            ('liabilities', f'{split_balance.get("between", "")}</p><p>{title}', 'June 30, 2025',
             [('Total liabilities', 'Liabilities', '40', date), ('Total equity', 'StockholdersEquity', '60', date),
              ('Total liabilities and equity', 'LiabilitiesAndStockholdersEquity', split_balance.get('total', '100'), date)]),
        ]
    tables, facts = [], {}
    for key, heading, header, lines in definitions:
        rows = [f'<tr><td></td><td>{header}</td></tr>']
        for index, (label, concept, value, period) in enumerate(lines):
            identifier = f'{key}-{index}'
            rows.append(f'<tr><td>{label}</td><td><ix:nonfraction id="{identifier}" name="g:{concept}" '
                        f'contextref="{identifier}" unitref="usd">{value}</ix:nonfraction></td></tr>')
            facts[identifier] = {'xml_id': identifier, 'concept': '{' + GAAP + '}' + concept, 'context_ref': identifier,
                                 'unit_ref': 'usd', 'period': dict(period), 'raw_value': value, 'is_nil': False,
                                 'dimensions': [],
                                 'unit': {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []},
                                 'entity': {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'}}
        tables.append(f'<p>{heading}</p><table id="{key}">{"".join(rows)}</table>')
    if registrant:
        facts['name'] = {'xml_id': 'name', 'concept': '{http://xbrl.sec.gov/dei/2026}EntityRegistrantName',
                         'context_ref': 'name', 'unit_ref': None, 'period': dict(HALF), 'raw_value': registrant,
                         'is_nil': False, 'dimensions': [], 'unit': None,
                         'entity': {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'}}
    tree = html.document_fromstring(f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
                                   + ''.join(tables) + '</body></html>').getroottree()
    return tree, facts


class WideTitleTests(unittest.TestCase):
    NAMES = registrant_names(['The Example Group', 'Example & Sons'])
    ACCEPTED = {
        'The Example Group Consolidated Statements of Income': 'income_statement',
        'Example & Sons and Subsidiaries Consolidated Balance Sheets': 'balance_sheet',
        'Consolidated Balance Sheets in thousands, except par values': 'balance_sheet',
        'Condensed Consolidated Statements of Net Income': 'income_statement',
        'Consolidated Statements of Earnings and Comprehensive Earnings': 'income_statement',
        'Interim Condensed Consolidated Statements of Operations and Comprehensive Income (Loss)': 'income_statement',
        'Condensed and Consolidated Statements of Comprehensive Income': 'income_statement',
        'Consolidated and Combined Statements of Earnings': 'income_statement',
        'Consolidated Statements of (Loss) Earnings': 'income_statement',
        'Statements of Income and Total Comprehensive Income': 'income_statement',
        'Consolidated Results of Operations': 'income_statement',
        'Consolidated Statement of Condition': 'balance_sheet',
    }

    def test_wider_titles_are_read_only_on_request(self):
        for title, kind in self.ACCEPTED.items():
            with self.subTest(title=title):
                self.assertIsNone(statement_kind(title))
                self.assertEqual(statement_kind(title, wide=True, registrants=self.NAMES), kind)

    def test_the_whole_text_must_still_be_a_title(self):
        refused = ('See the Consolidated Statements of Income', 'Notes to Consolidated Statements of Income',
                   'Another Company Consolidated Statements of Income', 'Parent Company Statements of Cash Flows',
                   'Statements of Income Data', 'Statements of Changes in Equity', 'Selected Financial Data',
                   'Results of Operations by Segment', 'The Example Group')
        for title in refused:
            with self.subTest(title=title):
                self.assertIsNone(statement_kind(title, wide=True, registrants=self.NAMES))

    def test_established_titles_mean_the_same_with_and_without_the_option(self):
        for title in ('Consolidated Statements of Income', 'Condensed Consolidated Balance Sheets',
                      'Statements of Consolidated Cash Flows', 'Consolidated Statements of Operations and Comprehensive Loss'):
            with self.subTest(title=title):
                self.assertIsNotNone(statement_kind(title))
                self.assertEqual(statement_kind(title, wide=True, registrants=self.NAMES), statement_kind(title))

    def test_a_registrant_name_between_title_and_table_is_header_metadata(self):
        tree = html.document_fromstring(
            '<html><body><p>Consolidated Statements of Net Income</p><p>The Example Group</p>'
            '<table><tr><td>Revenues</td><td>1</td></tr></table></body></html>').getroottree()
        [table] = tree.xpath('//table')
        self.assertEqual(recover_headings(table), ())
        [evidence] = recover_headings(table, wide=True, registrants=self.NAMES)
        self.assertEqual((evidence.kind, evidence.text), ('income_statement', 'Consolidated Statements of Net Income'))
        self.assertEqual(recover_headings(table, wide=True, registrants=registrant_names(['Somebody Else'])), ())

    def test_selection_reads_a_title_printed_after_the_registrant_name(self):
        options = {'income_title': 'The Example Group Consolidated Statements of Income', 'registrant': 'The Example Group'}
        tree, facts = filing(**options)
        with self.assertRaises(TableExtractionError):
            select_statement_segments(tree, facts)
        selected = select_statement_segments(tree, facts, corroborated_layouts=True)
        self.assertEqual([part.table_key for part in selected], ['income_statement', 'balance_sheet', 'cash_flow'])
        # Another company's name is not this filing's title.
        tree, facts = filing(income_title=options['income_title'], registrant='Somebody Else')
        with self.assertRaises(TableExtractionError):
            select_statement_segments(tree, facts, corroborated_layouts=True)


class HeaderLineTests(unittest.TestCase):
    REGISTRANTS = ['W. R. EXAMPLE CORP', 'AUTOEXAMPLE, INC /NY']
    BALANCE = [('balance_sheet', 'Consolidated Balance Sheets')]

    def headings(self, *blocks, header_lines=True, first_row='<td>Revenues</td><td>1</td>'):
        tree = html.document_fromstring(
            '<html><body>' + ''.join(f'<p>{block}</p>' for block in blocks)
            + f'<table><tr>{first_row}</tr><tr><td>Revenues</td><td>1</td></tr></table></body></html>').getroottree()
        [table] = tree.xpath('//table')
        # As selection does: the other spellings of the name belong to this reading only.
        found = recover_headings(table, wide=True, header_lines=header_lines,
                                 registrants=registrant_names(self.REGISTRANTS, spellings=header_lines))
        return [(item.kind, item.text) for item in found]

    def test_lines_printed_between_a_title_and_its_table_are_passed_only_on_request(self):
        lines = ('(Millions of Dollars, Except Share Data)', '(unaudited, in thousands, except per share data)',
                 '(Dollars and shares in millions, except per share amounts; unaudited)',
                 '(Unaudited; Dollars &amp; Shares in Millions Except Per Share Amounts)', '($ in millions)',
                 'Millions of dollars, except per share data', 'March 31, 2025 and December 31, 2024',
                 'FOR THE THREE- AND NINE-MONTHS ENDED SEPTEMBER 30, 2025 AND 2024',
                 'Years Ended January 31, 2026, February 1, 2025 and February 3, 2024',
                 'Three Months Ended March 31, 2025 and 2024 (in millions, except per share amounts)',
                 'W. R. Example Corporation and Subsidiaries at December 31, 2025 and 2024',
                 '____________________', 'Table of Contents', '36')
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(self.headings('Consolidated Balance Sheets', line), self.BALANCE)
                self.assertEqual(self.headings('Consolidated Balance Sheets', line, header_lines=False), [])

    def test_a_line_with_any_other_word_still_ends_the_search(self):
        lines = ('The following table presents segment results', 'Segment results for the three months ended March 31, 2025',
                 'Note 3', 'Millions of dollars, except for the acquisition described below', 'Supplemental information',
                 'Three months ended March 31, 2025 compared with 2024', 'Somebody Else and Subsidiaries at December 31, 2025')
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(self.headings('Consolidated Balance Sheets', line), [])

    def test_titles_with_missing_spaces_a_footnote_marker_or_another_spelling_of_the_registrant(self):
        titles = {
            'CONSOLIDATEDBALANCESHEETS (in thousands, except par values)': 'balance_sheet',
            'CONDENSED CONSOLIDATEDSTATEMENTS OF INCOME AND TOTAL COMPREHENSIVE INCOME': 'income_statement',
            '(UNAUDITED) CONSOLIDATED STATEMENTSOF EARNINGS AND COMPREHENSIVE EARNINGS': 'income_statement',
            'CONSOLIDATED STATEMENTS OF CASH FLOWS(1)': 'cash_flow',
            'CONSOLIDATED CASH FLOWS (Unaudited)': 'cash_flow',
            'W. R. EXAMPLE CORPORATION AND SUBSIDIARIES CONSOLIDATED BALANCE SHEETS': 'balance_sheet',
            'AutoExample, Inc. Consolidated Statements of Income': 'income_statement',
            'AutoExample Incorporated &#8211; Consolidated Statements of Earnings': 'income_statement',
        }
        for title, kind in titles.items():
            with self.subTest(title=title):
                self.assertEqual([found for found, _ in self.headings(title)], [kind])
                self.assertEqual(self.headings(title, header_lines=False), [])
        for title in ('Cash Flows', 'Somebody Else Corporation Consolidated Balance Sheets', 'CONSOLIDATEDBALANCESHEETDATA',
                      'Consolidated Statements of Cash Flows (1) continued from Note 2'):
            with self.subTest(title=title):
                self.assertEqual(self.headings(title), [])

    def test_a_title_printed_over_two_lines_is_joined_only_where_the_break_is_inside_it(self):
        first, second = 'CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS', 'AND COMPREHENSIVE INCOME (LOSS)'
        self.assertEqual(self.headings(first, second, '(Unaudited)'), [('income_statement', f'{first} {second}')])
        self.assertEqual(self.headings(first, second, '(Unaudited)', header_lines=False), [])
        self.assertEqual(self.headings('CONDENSED CONSOLIDATED STATEMENTS OF', 'COMPREHENSIVE INCOME'),
                         [('income_statement', 'CONDENSED CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME')])
        for lines in (('Consolidated Statements', 'Income'), ('Selected data of', 'Comprehensive Income'),
                      (first, 'Comprehensive income is described in Note 4')):
            with self.subTest(lines=lines):
                self.assertEqual(self.headings(*lines), [])

    def test_a_title_in_the_tables_own_header_beside_its_period(self):
        row = '<td>CONDENSED CONSOLIDATED INCOME STATEMENTS (Unaudited)</td><td>Three Months Ended</td>'
        self.assertEqual(self.headings(first_row=row), [('income_statement', 'CONDENSED CONSOLIDATED INCOME STATEMENTS (Unaudited)')])
        self.assertEqual(self.headings(first_row=row, header_lines=False), [])
        row = '<td>CONDENSED CONSOLIDATED INCOME STATEMENTS</td><td>By segment</td>'
        self.assertEqual(self.headings(first_row=row), [])

    def test_registrant_spellings_are_added_only_for_this_reading(self):
        self.assertEqual(registrant_names(['CORNING INC /NY']), ('corning inc ny',))
        self.assertEqual(set(registrant_names(['CORNING INC /NY'], spellings=True)), {'corning inc', 'corning incorporated'})
        self.assertEqual(set(registrant_names(['Kroger Co'], spellings=True)), {'kroger co', 'kroger company'})
        self.assertEqual(registrant_names(['Novo Nordisk A/S'], spellings=True), ('novo nordisk a s',))

    def test_selection_reads_header_lines_after_every_other_reading_has_declined(self):
        tree, facts = filing(income_title='CONSOLIDATEDSTATEMENTS OF INCOME</p><p>(Millions of Dollars, Except Share Data)')
        with self.assertRaises(TableExtractionError):
            select_statement_segments(tree, facts)
        selected = select_statement_segments(tree, facts, corroborated_layouts=True)
        self.assertEqual([part.table_key for part in selected], ['income_statement', 'balance_sheet', 'cash_flow'])
        tree, facts = filing(income_title='CONSOLIDATEDSTATEMENTS OF INCOME</p><p>Selected segment data in millions')
        with self.assertRaises(TableExtractionError):
            select_statement_segments(tree, facts, corroborated_layouts=True)
        self.assertIs(inspect.signature(recover_headings).parameters['header_lines'].default, False)
        self.assertIs(inspect.signature(statement_kind).parameters['header_lines'].default, False)


class TiedBalanceSheetTests(unittest.TestCase):
    KEYS = ['income_statement', 'balance_sheet.segment_1', 'balance_sheet.segment_2', 'cash_flow']
    NUMBERS = '<table><tr><td>Other assets</td><td><ix:nonfraction name="g:OtherAssets">5</ix:nonfraction></td></tr></table>'

    def select(self, **split_balance):
        tree, facts = filing(split_balance=split_balance)
        return select_statement_segments(tree, facts, corroborated_layouts=True)

    def test_two_tables_under_one_title_are_one_balance_sheet_when_the_totals_tie(self):
        tree, facts = filing(split_balance={})
        with self.assertRaisesRegex(TableExtractionError, 'balance_sheet: ambiguous or incomplete'):
            select_statement_segments(tree, facts)
        selected = self.select()
        self.assertEqual([part.table_key for part in selected], self.KEYS)
        assets, liabilities = selected[1], selected[2]
        self.assertEqual([assets.element.get('id'), liabilities.element.get('id')], ['assets', 'liabilities'])
        proof = assets.group['balance_continuation']
        self.assertEqual(proof['verification_version'], 'tied-balance-totals-v1')
        self.assertEqual(proof['totals'], [{'period': {'instant': '2025-06-30'},
                                            'facts': {'Assets': 'assets-0', 'LiabilitiesAndStockholdersEquity': 'liabilities-2'},
                                            'raw_values': {'Assets': '100', 'LiabilitiesAndStockholdersEquity': '100'}}])
        self.assertEqual((assets.group['segment_index'], liabilities.group['segment_index']), (1, 2))

    def test_footnotes_page_furniture_and_the_repeated_title_may_stand_between(self):
        for between in ('See notes to consolidated financial statements.', 'Table of Contents',
                        '<table><tr><td>86</td></tr></table>', '(1) Includes 5 of assets of consolidated entities.</p><p>86'):
            with self.subTest(between=between):
                self.assertEqual([part.table_key for part in self.select(between=between)], self.KEYS)

    def test_totals_that_differ_another_title_or_anything_else_between_are_refused(self):
        refused = ({'total': '101'}, {'title': 'Consolidated Statements of Financial Position'},
                   {'between': self.NUMBERS}, {'between': 'Note 2. Summary of significant accounting policies'},
                   {'between': 'Consolidated Statements of Cash Flows'}, {'between': 'Detail. ' * 300})
        for options in refused:
            with self.subTest(options=options), self.assertRaises(TableExtractionError):
                self.select(**options)


class SelectionTests(unittest.TestCase):
    KEYS = ['income_statement', 'balance_sheet', 'cash_flow']

    def selected(self, **options):
        tree, facts = filing(**options)
        return [part.table_key for part in select_statement_segments(tree, facts, corroborated_layouts=True)]

    def refused(self, corroborated_layouts, **options):
        tree, facts = filing(**options)
        with self.assertRaises(TableExtractionError):
            select_statement_segments(tree, facts, corroborated_layouts=corroborated_layouts)

    def test_ordinary_statements_are_selected_either_way(self):
        tree, facts = filing()
        self.assertEqual([part.table_key for part in select_statement_segments(tree, facts)], self.KEYS)
        self.assertEqual(self.selected(), self.KEYS)

    def candidate(self, table_id, kind, *, corroborate, concept_groups=None, **options):
        tree, facts = filing(**options)
        [table] = tree.xpath(f'//table[@id="{table_id}"]')
        return _segment_candidate(tree, table, kind, facts, {key: 1 for key in facts}, corroborate=corroborate,
                                  concept_groups=concept_groups)

    def test_a_statement_whose_header_needs_corroboration_is_a_candidate_only_with_the_option(self):
        with self.assertRaisesRegex(TableExtractionError, 'unresolved column period'):
            self.candidate('income', 'income_statement', corroborate=False, income_header='Six Months 2025')
        candidate = self.candidate('income', 'income_statement', corroborate=True, income_header='Six Months 2025')
        [axis] = candidate['axes']
        self.assertEqual(json.loads(axis), HALF)
        self.assertEqual(candidate['coverage'][axis], {0, 1, 2})
        # Printed wording that differs from the tags is still refused.
        for header in ('Three Months 2025', 'Six Months 2024'):
            with self.subTest(header=header), self.assertRaisesRegex(TableExtractionError, 'unresolved column period'):
                self.candidate('income', 'income_statement', corroborate=True, income_header=header)

    def test_the_closing_total_that_includes_discontinued_operations_completes_a_cash_flow_statement(self):
        self.refused(False, cash_concept=DISPOSAL_CASH)
        tree, facts = filing(cash_concept=DISPOSAL_CASH)
        selected = select_statement_segments(tree, facts, corroborated_layouts=True)
        self.assertEqual([part.table_key for part in selected], self.KEYS)
        self.assertTrue(all(part.group is None for part in selected))

    def test_the_closing_total_counts_only_when_dated_at_the_column_end(self):
        options = {'corroborate': True, 'concept_groups': _CORROBORATED_STATEMENT_CONCEPTS, 'cash_concept': DISPOSAL_CASH}
        dated = self.candidate('cash', 'cash_flow', **options)
        self.assertEqual(list(dated['coverage'].values()), [{0, 1, 2, 3}])
        misdated = self.candidate('cash', 'cash_flow', closing_date='2025-03-31', **options)
        self.assertEqual(list(misdated['coverage'].values()), [{0, 1, 2}])
        # The established catalog never counts that concept.
        established = self.candidate('cash', 'cash_flow', corroborate=True, cash_concept=DISPOSAL_CASH)
        self.assertEqual(list(established['coverage'].values()), [{0, 1, 2}])

    def test_the_default_catalog_is_unchanged_and_the_wider_one_adds_one_concept(self):
        self.assertNotIn(DISPOSAL_CASH, set().union(*_STATEMENT_CONCEPTS['cash_flow']))
        for kind, groups in _STATEMENT_CONCEPTS.items():
            wider = _CORROBORATED_STATEMENT_CONCEPTS[kind]
            self.assertEqual(len(wider), len(groups))
            added = [set(new) - set(old) for old, new in zip(groups, wider)]
            self.assertEqual(added, ([set(), set(), set(), {DISPOSAL_CASH}] if kind == 'cash_flow' else [set()] * len(groups)))


class ProfileScopeTests(unittest.TestCase):
    def test_only_the_v3_adapter_turns_the_layouts_on(self):
        for function in (extract_tables, parse_tables, validate_statement_segments, select_statement_segments):
            self.assertIs(inspect.signature(function).parameters['corroborated_layouts'].default, False)
        self.assertIs(inspect.signature(_column_periods).parameters['corroborate'].default, False)
        source = inspect.getsource(filing_adapter_v3)
        self.assertIn('parse_v2_tables(_v2_context(context), corroborated_layouts=True)', source)
        self.assertIn('corroborated_layouts=True) is True', source)


if __name__ == '__main__':
    unittest.main()
