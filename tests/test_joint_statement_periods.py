"""Bounded YTD dates and namespace-qualified primary-statement aliases."""

from copy import deepcopy
from pathlib import Path
import unittest

from lxml import html

from sec_pipeline.joint_statement_periods import (
    GAAP_CASH_BALANCE_ALIASES, GAAP_REVENUE_ALIASES, additional_gaap_group,
    cash_alias_endpoint_period, resolve_ytd_period,
)
from sec_pipeline.tables import column_headers, layout_grid, local, verify_inline, xml_evidence


ROOT = Path(__file__).resolve().parents[1]
PERIOD = {'startDate': '2026-01-01', 'endDate': '2026-06-30'}
CASH = '{http://fasb.org/us-gaap/2026}' + next(iter(GAAP_CASH_BALANCE_ALIASES))


class JointPeriodTests(unittest.TestCase):
    def test_ytd_requires_the_actual_source_calendar_duration(self):
        for headers in (['Year-to-date endedJune 30,', '2026'], ['Year to Date June 30', '2026'],
                        ['EVERGY, INC.', 'Consolidated Statements of Cash Flows', '(Unaudited)',
                         'Year to Date June 30', '2026', '(millions)']):
            before = deepcopy(headers), dict(PERIOD)
            with self.subTest(headers=headers):
                self.assertEqual(resolve_ytd_period(headers, PERIOD), PERIOD)
                self.assertEqual((headers, PERIOD), before)
                self.assertIsNone(resolve_ytd_period(headers, {'startDate': '2026-04-01', 'endDate': '2026-06-30'}))

    def test_missing_ambiguous_or_contradictory_headers_cannot_supply_a_period(self):
        for headers in (['2026'], ['Year-to-date', '2026'], ['Year to date June 30'],
                        ['Year to date June 30', '2025'], ['Year to date June 30', '2026', '2025'],
                        ['Quarter and year-to-date ended June 30', '2026'],
                        ['Three months ended', 'Year-to-date ended June 30', '2026'],
                        ['Year to Date June 30 and July 31', '2026'],
                        ['Year-to-date June 31', '2026'], ['Fiscal year-to-date June 30', '2026'],
                        ['Year-to-date June 30, 2026', '7/31/2026'], ['Year-to-date June 30, 2026', '2026-07-31']):
            with self.subTest(headers=headers):
                self.assertIsNone(resolve_ytd_period(headers, PERIOD))
        for period in (None, {'instant': '2026-06-30'}, {'startDate': '2025-07-01', 'endDate': '2026-06-30'},
                       {'startDate': '2026-01-01', 'endDate': '2026-07-01'},
                       {'startDate': '2026-07-01', 'endDate': '2026-06-30'},
                       {'startDate': 'invalid', 'endDate': '2026-06-30'}):
            with self.subTest(period=period):
                self.assertIsNone(resolve_ytd_period(['Year to date June 30', '2026'], period))

    def test_aliases_require_an_exact_standard_namespace_and_name(self):
        revenue = next(iter(GAAP_REVENUE_ALIASES))
        self.assertEqual(additional_gaap_group('{http://fasb.org/us-gaap/2025}' + revenue), ('income_statement', 0))
        self.assertEqual(additional_gaap_group(CASH), ('cash_flow', 3))
        for concept in ('us-gaap:' + revenue, '{https://issuer.example/2026}' + revenue,
                        '{http://fasb.org.example/us-gaap/2026}' + revenue,
                        '{http://fasb.org/us-gaap/2026}' + revenue.lower(),
                        '{http://fasb.org/us-gaap/2026}PaymentsToAcquirePropertyPlantAndEquipment'):
            with self.subTest(concept=concept):
                self.assertIsNone(additional_gaap_group(concept))

    def test_cash_alias_dates_require_an_explicit_cash_endpoint_label(self):
        opening = {'instant': '2025-12-31'}
        closing = {'instant': '2026-06-30'}
        for heading, wanted in (
                ('CASH AND CASH EQUIVALENTS AND RESTRICTED CASH, BEGINNING OF PERIOD', opening),
                ('CASH AND CASH EQUIVALENTS AND RESTRICTED CASH, END OF PERIOD', closing),
                ('Cash, cash equivalents and restricted cash, January 1', opening),
                ('Cash, cash equivalents and restricted cash, June 30', closing),
                ('Cash, cash equivalents and restricted cash, January 1, 2026', opening)):
            with self.subTest(heading=heading):
                self.assertEqual(cash_alias_endpoint_period(CASH, heading, PERIOD), wanted)
        for heading in ('Cash, cash equivalents and restricted cash, July 31',
                        'Cash, cash equivalents and restricted cash, January 1, 2025',
                        'Cash, cash equivalents and restricted cash, beginning and end of period',
                        'Other assets at end of period', 'Cash, cash equivalents and restricted cash',
                        'Total cash, cash equivalents and restricted cash on the balance sheet'):
            with self.subTest(heading=heading):
                self.assertIsNone(cash_alias_endpoint_period(CASH, heading, PERIOD))
        self.assertIsNone(cash_alias_endpoint_period(CASH.replace('http://fasb.org', 'http://issuer.example'),
                                                    'Cash and cash equivalents and restricted cash, end of period', PERIOD))


class ArchivedJointPeriodTests(unittest.TestCase):
    def source(self, accession):
        directory = ROOT / 'data/raw/sec' / accession
        html_files, xml_files = list(directory.glob('*.htm')), list(directory.glob('*_htm.xml'))
        if len(html_files) != 1 or len(xml_files) != 1:
            self.skipTest('Archived joint-statement source unavailable')
        tree = html.parse(str(html_files[0]), parser=html.HTMLParser(encoding='utf-8'))
        return tree, xml_evidence(xml_files[0], include_entity=True)

    def test_hst_and_evergy_periods_use_original_headers_and_consistent_xml_occurrences(self):
        for accession, xpath in (('0001070750-26-000126', '/html/body/div[87]/table'),
                                 ('0001711269-26-000100', '/html/body/div[88]/table')):
            with self.subTest(accession=accession):
                tree, facts = self.source(accession)
                rows, _ = layout_grid(tree.xpath(xpath)[0], tree)
                first = next(index for index, row in enumerate(rows) if any(local(node) == 'nonfraction'
                             for cell in row for node in cell['_element'].iter()))
                shared = [cell['display_text'] for row in rows[:first] for cell in row
                          if cell['column'] == 0 and cell['display_text'].lower().startswith('year to date')]
                references = {}
                for row in rows[first:]:
                    for cell in row:
                        if cell['column'] == 0:
                            continue
                        for node in cell['_element'].iter():
                            if local(node) != 'nonfraction':
                                continue
                            fact = facts[node.get('id')]
                            verify_inline(node, fact, {})
                            if 'startDate' in fact['period']:
                                for column in range(cell['column'], cell['column'] + cell['colspan']):
                                    references.setdefault(column, []).append(fact)
                for column, refs in references.items():
                    periods = {tuple(sorted(ref['period'].items())) for ref in refs}
                    self.assertEqual(len(periods), 1)
                    self.assertGreaterEqual(len({ref['concept'] for ref in refs}), 2)
                    period = dict(next(iter(periods)))
                    headers = [*shared, *column_headers(rows, first, column)]
                    self.assertEqual(resolve_ytd_period(headers, period), period)

    def test_hst_and_sempra_cash_aliases_match_the_same_occurrence_period(self):
        for accession, xpath in (('0001070750-26-000126', '/html/body/div[87]/table'),
                                 ('0001032208-26-000045', '/html/body/div[97]/table')):
            with self.subTest(accession=accession):
                tree, facts = self.source(accession)
                checked = 0
                for row in layout_grid(tree.xpath(xpath)[0], tree)[0]:
                    for cell in row:
                        for node in cell['_element'].iter():
                            if local(node) != 'nonfraction':
                                continue
                            fact = facts[node.get('id')]
                            if additional_gaap_group(fact['concept']) != ('cash_flow', 3):
                                continue
                            verify_inline(node, fact, {})
                            instant = fact['period']['instant']
                            year = int(instant[:4]) + (instant.endswith('-12-31'))
                            column_period = {'startDate': f'{year}-01-01', 'endDate': f'{year}-06-30'}
                            self.assertEqual(cash_alias_endpoint_period(fact['concept'], row[0]['display_text'], column_period), fact['period'])
                            checked += 1
                self.assertEqual(checked, 4)


if __name__ == '__main__':
    unittest.main()
