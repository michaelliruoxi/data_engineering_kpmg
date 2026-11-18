"""Column headers and cash-flow balances that tables-v3 accepts on corroboration."""

import inspect
import json
import unittest

from lxml import html

from sec_pipeline import filing_adapter_v3
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
           closing_label='Cash and cash equivalents at end of period', closing_date='2025-06-30'):
    """Three ordinary statements, each one table under its printed title."""
    definitions = [
        ('income', 'Consolidated Statements of Income', income_header,
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
    tree = html.document_fromstring(f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
                                   + ''.join(tables) + '</body></html>').getroottree()
    return tree, facts


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
