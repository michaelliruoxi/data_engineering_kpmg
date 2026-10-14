"""Omitted cash-flow headers need explicit continuation and balance evidence."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import unittest
from uuid import UUID

from lxml import html

from sec_pipeline.tables import (
    TableExtractionError, extract_tables, select_statement_segments,
    validate_statement_segments, xml_evidence,
)


GAAP = 'http://fasb.org/us-gaap/2026'
ROOT = Path(__file__).resolve().parents[1]
SCOPE = {'filing_id': UUID(int=1), 'html_document_id': UUID(int=2), 'xml_document_id': UUID(int=3)}
CASH = 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
PERIOD = {'startDate': '2025-01-01', 'endDate': '2025-12-31'}


def fixture():
    definitions = [
        ('income', 'Consolidated Statements of Income', ['Revenues', 'OperatingIncomeLoss', 'NetIncomeLoss'], ['100', '10', '8']),
        ('balance', 'Consolidated Balance Sheets', ['Assets', 'Liabilities', 'StockholdersEquity'], ['100', '40', '60']),
        ('cash1', 'Consolidated Statements of Cash Flows',
         ['NetCashProvidedByUsedInOperatingActivities', 'NetCashProvidedByUsedInInvestingActivities'], ['10', '-3']),
        ('cash2', 'Consolidated Statements of Cash Flows',
         ['NetCashProvidedByUsedInFinancingActivities', 'EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
          CASH + 'PeriodIncreaseDecreaseIncludingExchangeRateEffect', CASH, CASH], ['-2', '1', '6', '20', '26']),
    ]
    tables, facts = [], {}
    for key, heading, concepts, values in definitions:
        if key == 'cash2':
            tables.append('<table id="footer"><tr><td>Example Fiscal 2025 Form 10-K12</td></tr></table><p>Table of Contents</p>')
        header = '' if key == 'cash2' else 'December 31, 2025' if key == 'balance' else 'Year Ended December 31, 2025'
        rows = [f'<tr><td></td><td>{header}</td></tr>']
        for index, (concept, value) in enumerate(zip(concepts, values)):
            identifier = f'{key}-{index}'
            label, period = concept, PERIOD
            if key == 'balance':
                period = {'instant': '2025-12-31'}
            elif key == 'cash2' and index in (3, 4):
                label = 'Cash, cash equivalents and restricted cash at ' + ('beginning' if index == 3 else 'end') + ' of period'
                period = {'instant': '2024-12-31' if index == 3 else '2025-12-31'}
            rows.append(f'<tr><td>{label}</td><td><ix:nonfraction id="{identifier}" name="g:{concept}" '
                        f'contextref="{identifier}" unitref="usd">{value}</ix:nonfraction></td></tr>')
            facts[identifier] = {'xml_id': identifier, 'concept': '{' + GAAP + '}' + concept,
                                 'context_ref': identifier, 'unit_ref': 'usd', 'period': dict(period),
                                 'raw_value': value, 'is_nil': False, 'dimensions': [],
                                 'unit': {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []},
                                 'entity': {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'}}
        tables.append(f'<p>{heading}</p><table id="{key}">{"".join(rows)}</table>')
    tree = html.document_fromstring(f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
                                   + ''.join(tables) + '</body></html>').getroottree()
    return tree, facts


class CashHeaderContinuationTests(unittest.TestCase):
    def test_empty_headers_inherit_cited_physical_columns_and_exact_balances(self):
        tree, facts = fixture()
        selected = select_statement_segments(tree, facts)
        self.assertEqual([part.table_key for part in selected],
                         ['income_statement', 'balance_sheet', 'cash_flow.segment_1', 'cash_flow.segment_2'])
        second = selected[-1]
        inherited = second.group['header_inheritance']
        self.assertEqual(inherited['columns']['1']['period'], PERIOD)
        self.assertEqual(inherited['source_table_key'], 'cash_flow.segment_1')
        self.assertEqual(tree.xpath(inherited['columns']['1']['source_cells'][0]['source_xpath'])[0].text,
                         'Year Ended December 31, 2025')
        self.assertEqual(second.element.xpath('./tr[1]/td[2]')[0].text, None)
        proof = second.group['closure_evidence'][0]
        self.assertEqual(proof['activity_xml_ids'], ['cash1-0', 'cash1-1', 'cash2-0'])
        self.assertEqual(proof['effect_xml_id'], 'cash2-1')
        self.assertEqual(proof['opening_xml_id'], 'cash2-3')
        self.assertEqual(proof['closing_xml_id'], 'cash2-4')

    def test_wrong_scope_period_currency_dimensions_or_balance_is_rejected(self):
        for change in ('entity', 'unit', 'dimensions', 'duration', 'opening_date', 'closing_date', 'balance', 'activity', 'effect'):
            tree, facts = fixture()
            if change in ('entity', 'unit', 'dimensions'):
                for key in (key for key in facts if key.startswith('cash2')):
                    if change == 'entity':
                        facts[key]['entity']['identifier'] = '0000000002'
                    elif change == 'unit':
                        facts[key]['unit']['numerator'] = ['{http://www.xbrl.org/2003/iso4217}EUR']
                    else:
                        facts[key]['dimensions'] = [{'kind': 'explicit', 'dimension': '{example}Axis', 'member': '{example}ParentCompanyMember'}]
            elif change == 'duration':
                facts['cash2-0']['period']['startDate'] = '2025-02-01'
            elif change == 'opening_date':
                facts['cash2-3']['period']['instant'] = '2025-01-01'
            elif change == 'closing_date':
                facts['cash2-4']['period']['instant'] = '2025-12-30'
            else:
                key = {'balance': 'cash2-4', 'activity': 'cash1-0', 'effect': 'cash2-1'}[change]
                facts[key]['raw_value'] = '99'
                tree.xpath(f'//*[@id="{key}"]')[0].text = '99'
            with self.subTest(change=change), self.assertRaises(TableExtractionError):
                select_statement_segments(tree, facts)

    def test_headers_width_titles_and_intervening_data_are_not_inferred(self):
        for change in ('header', 'width', 'title', 'footer', 'data', 'prose', 'note', 'second_footer'):
            tree, facts = fixture()
            second = tree.xpath('//table[@id="cash2"]')[0]
            footer = tree.xpath('//table[@id="footer"]')[0]
            if change == 'header':
                second.xpath('./tr[1]/td[2]')[0].text = '2024'
            elif change == 'width':
                second.xpath('./tr[1]/td[2]')[0].set('colspan', '2')
            elif change == 'title':
                second.getprevious().text = 'Statements of Other Cash Information'
            elif change == 'footer':
                footer.xpath('./tr/td')[0].text = 'Other financial statement 10 20 30'
            elif change == 'second_footer':
                footer.addnext(deepcopy(footer))
            else:
                markup = {'data': '<table><tr><td>Other amounts</td><td>123</td></tr></table>',
                          'prose': '<p>Other financial information 123</p>',
                          'note': '<p>Note 4. Debt</p>'}[change]
                footer.addnext(html.fragment_fromstring(markup))
            with self.subTest(change=change), self.assertRaises(TableExtractionError):
                select_statement_segments(tree, facts)


class ArchivedCashHeaderContinuationTests(unittest.TestCase):
    def sources(self):
        folder = ROOT / 'data/raw/sec/0000896878-26-000037'
        html_paths, xml_paths = list(folder.glob('*.htm')), list(folder.glob('*_htm.xml'))
        if len(html_paths) != 1 or len(xml_paths) != 1:
            self.skipTest('Archived continuation source unavailable')
        return html_paths[0], xml_paths[0]

    def test_intuit_preserves_both_tables_and_empty_continuation_headers(self):
        html_path, xml_path = self.sources()
        tables = extract_tables(html_path, xml_path, tables_version='tables-v2', **SCOPE)
        self.assertEqual([table.table_key for table in tables[-2:]], ['cash_flow.segment_1', 'cash_flow.segment_2'])
        self.assertEqual([table.structure['source_xpath'] for table in tables[-2:]],
                         ['/html/body/div[865]/table', '/html/body/div[868]/table'])
        self.assertTrue(all(table.structure['inventory'].get('unresolved', 0) == 0 for table in tables))
        second = tables[-1]
        inherited_columns = set(second.structure['statement_group']['header_inheritance']['columns'])
        self.assertTrue(all(cell['column_headings'] == [] for row in second.structure['rows'] for cell in row
                            if str(cell['column']) in inherited_columns))
        self.assertEqual(len(second.structure['statement_group']['closure_evidence']), 3)
        self.assertEqual(len(second.structure['cash_reconciliation_evidence']), 1)
        tree = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8'))
        facts = xml_evidence(xml_path, include_entity=True)
        self.assertTrue(validate_statement_segments(tables, tree, facts, xml_path=xml_path))
        structure = deepcopy(second.structure)
        structure['statement_group']['header_inheritance']['columns']['3']['source_cells'][0]['display_text'] = 'invented'
        changed = [*tables[:-1], replace(second, structure=structure)]
        with self.assertRaises(TableExtractionError):
            validate_statement_segments(changed, tree, facts, xml_path=xml_path)


if __name__ == '__main__':
    unittest.main()
