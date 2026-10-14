"""Source-backed fiscal dates, shared captions and immutable V2 replays."""

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import unittest
from uuid import UUID

from lxml import html

from sec_pipeline.tables import (
    _column_periods, column_headers, extract_tables, header_period, layout_grid,
)


ROOT = Path(__file__).resolve().parents[1]
SCOPE = {'filing_id': UUID(int=1), 'html_document_id': UUID(int=2), 'xml_document_id': UUID(int=3)}
GAAP = 'http://fasb.org/us-gaap/2026'
FISCAL_CALENDAR = (
    'We operate using a 52/53 week fiscal year ending on the first Sunday after January 30, '
    'which for fiscal 2026 was February 1, 2026 and for fiscal 2027 will be January 31, 2027. '
    'The second quarter of fiscal 2026 and 2027 ended on August 3, 2025 and August 2, 2026. '
    'Unless otherwise stated, all dates refer to our fiscal year and fiscal quarters.'
)
WEEK_CALENDAR = (
    'The 2026 and 2025 third quarters and three fiscal quarters referenced herein represent '
    'the twelve-week and thirty-six-week periods ended September 6, 2026 and September 7, 2025, respectively.'
)


def columns(headers, period, *, before='', prose='', mutate=None, advanced=True):
    header_rows = ''.join(f'<tr><td></td><td>{value}</td></tr>' for value in headers)
    body = ''.join(f'<tr><td>{concept}</td><td><ix:nonfraction id="f{index}" name="g:{concept}" '
                   f'contextref="c" unitref="usd">1</ix:nonfraction></td></tr>'
                   for index, concept in enumerate(('Revenues', 'NetIncomeLoss')))
    tree = html.document_fromstring(
        f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
        f'{before}<div><table>{header_rows}{body}</table></div><p>{prose}</p></body></html>').getroottree()
    table = tree.xpath('//table')[-1]
    facts = {f'f{index}': {'xml_id': f'f{index}', 'concept': '{' + GAAP + '}' + concept,
                           'context_ref': 'c', 'unit_ref': 'usd', 'period': dict(period),
                           'raw_value': '1', 'is_nil': False}
             for index, concept in enumerate(('Revenues', 'NetIncomeLoss'))}
    if mutate:
        mutate(tree, facts)
    rows, _ = layout_grid(table, tree)
    periods, provenance = _column_periods(rows, len(headers), facts, {'g': GAAP},
                                          {'f0': 1, 'f1': 1}, advanced=advanced)
    return periods.get(1), provenance, tree, rows


class HeaderPeriodV2Tests(unittest.TestCase):
    def test_year_only_calendar_fiscal_year_needs_exact_corroborated_annual_dates(self):
        for period in ({'startDate': '2025-07-01', 'endDate': '2026-06-30'},
                       {'startDate': '2023-07-01', 'endDate': '2024-06-30'}):
            with self.subTest(period=period):
                headers = [period['endDate'][:4]]
                resolved, provenance, _, _ = columns(headers, period)
                self.assertEqual(resolved, period)
                self.assertEqual(provenance['1']['method'], 'corroborated_column_contexts')
                self.assertIsNone(header_period(headers, context_period=period, advanced=False))
        period = {'startDate': '2025-07-01', 'endDate': '2026-06-30'}
        for headers, candidate in [(['2025'], period), (['2026', '2025'], period),
                (['Six Months', '2026'], period), (['2026', 'December 31'], period),
                (['2026'], {'startDate': '2026-01-01', 'endDate': '2026-06-30'}),
                (['2026'], {'startDate': '2025-08-01', 'endDate': '2026-06-30'}),
                (['2026'], {'startDate': '2025-07-03', 'endDate': '2026-06-30'})]:
            with self.subTest(headers=headers, candidate=candidate):
                self.assertNotEqual(columns(headers, candidate)[0], candidate)
        self.assertIsNone(columns(['2026'], period, mutate=lambda tree, facts: facts.pop('f1'))[0])

    def test_plural_and_ordinal_quarters_are_distinct(self):
        cases = [(['Three Fiscal Quarters Ended', 'August 2,2026'],
                  {'startDate': '2025-11-03', 'endDate': '2026-08-02'}),
                 (['Second Quarter Ended', 'August 2,2026'],
                  {'startDate': '2026-05-04', 'endDate': '2026-08-02'}),
                 (['Two Quarters Ended', 'August 2,2026'],
                  {'startDate': '2026-02-02', 'endDate': '2026-08-02'})]
        for headers, period in cases:
            with self.subTest(headers=headers):
                self.assertEqual(header_period(headers, context_period=period, advanced=True), period)
        self.assertIsNone(header_period(cases[0][0], context_period=cases[0][1]))
        self.assertIsNone(header_period(['Two and Three Fiscal Quarters Ended August 2,2026'],
                                        context_period=cases[0][1], advanced=True))

    def test_spelled_week_counts_and_numeric_dates(self):
        self.assertEqual(header_period(['Thirteen Weeks Ended', '8/1/2026'], advanced=True),
                         {'startDate': '2026-05-03', 'endDate': '2026-08-01'})
        self.assertEqual(header_period(['Twenty-six Weeks Ended', 'August 1, 2026'], advanced=True),
                         {'startDate': '2026-02-01', 'endDate': '2026-08-01'})
        for headers in (['Thirteen and Twenty-six Weeks Ended', '8/1/2026'],
                        ['Thirteen Weeks Ended', '2/30/2026'],
                        ['13 Weeks Ended', '8/1/2026', '8/2/2025']):
            with self.subTest(headers=headers):
                self.assertIsNone(header_period(headers, advanced=True))
        self.assertIsNone(header_period(['Thirteen Weeks Ended', '8/1/2026']))

    def test_nonstandard_fiscal_quarters_require_matching_calendar_prose(self):
        period = {'startDate': '2026-06-15', 'endDate': '2026-09-06'}
        headers = ['Fiscal Quarter Ended', 'September 6,', '2026']
        self.assertIsNone(columns(headers, period)[0])
        resolved, provenance, tree, _ = columns(headers, period, prose=WEEK_CALENDAR)
        self.assertEqual(resolved, period)
        self.assertEqual(provenance['1']['xml_ids'], ['f0', 'f1'])
        for source in provenance['1']['calendar_sources']:
            self.assertEqual(''.join(tree.xpath(source['source_xpath'])[0].itertext()), source['display_text'])
        for prose in (WEEK_CALENDAR.replace('twelve-week', 'thirteen-week'),
                      WEEK_CALENDAR.replace('September 6, 2026', 'September 5, 2026'),
                      WEEK_CALENDAR.replace('September 6, 2026', 'September 5, 2026') + ' A subsequent event occurred September 6, 2026.',
                      'Our awards represent twelve-week performance measures for the fiscal quarter ended September 6, 2026.', ''):
            with self.subTest(prose=prose):
                self.assertIsNone(columns(headers, period, prose=prose)[0])
        self.assertIsNone(columns(headers, period, prose=WEEK_CALENDAR, advanced=False)[0])
        for contradictory in ([*headers, 'September 7, 2026'], [*headers, 'September 7,'],
                              [*headers, '9/7/2026'], [*headers, 'Thirteen Weeks Ended'],
                              [*headers, '2025'], [*headers, 'Three Months Ended']):
            with self.subTest(headers=contradictory):
                self.assertNotEqual(columns(contradictory, period, prose=WEEK_CALENDAR)[0], period)
        conflicting = WEEK_CALENDAR + '</p><p>' + WEEK_CALENDAR.replace('twelve-week', 'thirteen-week')
        self.assertIsNone(columns(headers, period, prose=conflicting)[0])

    def test_fiscal_year_labels_use_explicit_comparative_date_mapping(self):
        cases = [(['Second Quarter of Fiscal', '2027'],
                  {'startDate': '2026-05-04', 'endDate': '2026-08-02'}),
                 (['First Two Quarters of Fiscal', '2026'],
                  {'startDate': '2025-02-03', 'endDate': '2025-08-03'}),
                 (['At the End of', 'Second Quarter of Fiscal 2027'], {'instant': '2026-08-02'})]
        for headers, period in cases:
            with self.subTest(headers=headers):
                self.assertIsNone(header_period(headers, context_period=period, advanced=True))
                self.assertEqual(columns(headers, period, prose=FISCAL_CALENDAR)[0], period)
        headers, period = cases[0]
        for changed in (['Second Quarter of Fiscal', '2028'], ['Third Quarter of Fiscal', '2027'],
                        ['Second Quarter of Fiscal', '2027', 'August 4, 2026'],
                        ['Second Quarter of Fiscal', '2027', 'August 4,']):
            with self.subTest(headers=changed):
                self.assertNotEqual(columns(changed, period, prose=FISCAL_CALENDAR)[0], period)
        self.assertIsNone(columns(headers, period, prose=FISCAL_CALENDAR.replace('August 2, 2026', 'August 9, 2026'))[0])
        conflicting = FISCAL_CALENDAR + '</p><p>' + FISCAL_CALENDAR.replace('August 2, 2026', 'August 9, 2026')
        self.assertIsNone(columns(headers, period, prose=conflicting)[0])
        self.assertIsNone(columns(headers, period, prose=conflicting.replace('</p><p>', ' '))[0])

    def test_corroboration_rejects_one_concept_conflicts_and_changed_inline_values(self):
        headers = ['Fiscal Quarter Ended', 'September 6,', '2026']
        period = {'startDate': '2026-06-15', 'endDate': '2026-09-06'}

        def same_concept(tree, facts):
            facts['f1']['concept'] = facts['f0']['concept']
            tree.xpath('//*[@id="f1"]')[0].set('name', 'g:Revenues')

        def conflicting_period(tree, facts):
            facts['f1']['period']['startDate'] = '2026-06-14'

        def changed_inline(tree, facts):
            tree.xpath('//*[@id="f1"]')[0].text = '2'

        for mutation in (same_concept, conflicting_period, changed_inline):
            with self.subTest(mutation=mutation.__name__):
                self.assertIsNone(columns(headers, period, prose=WEEK_CALENDAR, mutate=mutation)[0])

    def test_outside_caption_is_cited_without_fabricating_displayed_header(self):
        headers = ['Three Months', '2026']
        period = {'startDate': '2026-05-01', 'endDate': '2026-07-31'}
        caption = '<div>Periods Ended July31,</div><div>(In millions, unaudited)</div>'
        resolved, provenance, tree, rows = columns(headers, period, before=caption)
        self.assertEqual(resolved, period)
        self.assertEqual(column_headers(rows, 2, 1), headers)
        self.assertEqual(provenance['1']['header_sources'][0]['display_text'], 'Periods Ended July31,')
        self.assertIsNone(columns(headers, period, before=caption + '<table><tr><td>Other data</td></tr></table>')[0])
        self.assertIsNone(columns(headers, period, before=caption.replace('July31', 'July30'))[0])

    def test_shared_two_date_caption_needs_specific_compatible_child_headers(self):
        caption = 'For the Three and Nine Months Ended August 2, 2026 and July 27, 2025'
        headers = [caption, 'Nine Months Ended', '2025']
        period = {'startDate': '2024-10-28', 'endDate': '2025-07-27'}
        resolved, provenance, _, rows = columns(headers, period)
        self.assertEqual(resolved, period)
        self.assertEqual(column_headers(rows, 3, 1), headers)
        self.assertEqual(provenance['1']['header_sources'][0]['display_text'], caption)
        for changed in ([caption, '2025'], [caption, 'Six Months Ended', '2025'],
                        [caption, 'Nine Months Ended', '2024'],
                        [caption.replace('August 2, 2026', 'August 2, 2025'), 'Nine Months Ended', '2025']):
            with self.subTest(headers=changed):
                self.assertIsNone(columns(changed, period)[0])


class ArchivedPeriodV2Tests(unittest.TestCase):
    def source(self, accession):
        directory = ROOT / 'data/raw/sec' / accession
        html_files, xml_files = list(directory.glob('*.htm')), list(directory.glob('*_htm.xml'))
        if len(html_files) != 1 or len(xml_files) != 1:
            self.skipTest('Archived statement source unavailable')
        return html_files[0], xml_files[0]

    def test_six_real_filings_have_no_remaining_header_period_mismatches(self):
        for accession in ('0001286681-26-000040', '0001730168-26-000080', '0001104659-26-108926',
                          '0001628280-26-061160', '0001104659-26-102213', '0001474432-26-000088'):
            with self.subTest(accession=accession):
                html_path, xml_path = self.source(accession)
                tables = extract_tables(html_path, xml_path, tables_version='tables-v2', **SCOPE)
                self.assertEqual(len(tables), 3)
                tree = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8'))
                for table in tables:
                    for row in table.structure['rows']:
                        for cell in row:
                            if cell['fact_references'] and cell['column'] > 0:
                                self.assertIsNotNone(cell['column_period'])
                            self.assertNotIn(cell['unresolved_reason'],
                                             ('Unresolved column period', 'Fact period differs from column heading'))
                    for evidence in table.structure.get('column_period_evidence', {}).values():
                        for key in ('calendar_sources', 'header_sources'):
                            for source in evidence.get(key, []):
                                [node] = tree.xpath(source['source_xpath'])
                                self.assertEqual(' '.join(''.join(node.itertext()).split()), source['display_text'])
                if accession in ('0001286681-26-000040', '0001730168-26-000080'):
                    self.assertTrue(all(table.structure['inventory'].get('unresolved', 0) == 0 for table in tables))

    def test_previously_loaded_segmented_payloads_remain_byte_identical(self):
        hashes = {
            '0000016918-26-000040': '32feb947689293b51b5ec40a331bb19f6a64d95dfed25fce2755edba771f6bc2',
            '0000077476-26-000048': '1bec3d8a70e609629d1733c823cc8f4f4d0b0de3b4f92b80a8cfacc4241cb24e',
            '0000940944-26-000042': 'b2aee7dbba9bf53e137d2f262785155f16de3e86387efd51279a9caece1e08e8',
            '0000726958-26-000085': 'feb1d15756c5221c3afcfb49c0d46bbc34cc0a71969c71704d69e2a2605041a4',
        }
        for accession, expected in hashes.items():
            with self.subTest(accession=accession):
                tables = extract_tables(*self.source(accession), tables_version='tables-v2', **SCOPE)
                payload = json.dumps([asdict(table) for table in tables], sort_keys=True, default=str, separators=(',', ':')).encode()
                self.assertEqual(hashlib.sha256(payload).hexdigest(), expected)


if __name__ == '__main__':
    unittest.main()
