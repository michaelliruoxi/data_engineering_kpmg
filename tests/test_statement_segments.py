"""Versioned physical statement segments, source grouping and replay boundaries."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

from lxml import etree, html

from sec_pipeline.database import FactInput
from sec_pipeline.fact_extraction import extract_facts
from sec_pipeline.tables import (
    TableExtractionError, extract_tables, header_period, select_statement_segments,
    validate_statement_segments, xml_evidence,
)


GAAP = 'http://fasb.org/us-gaap/2025'
SCOPE = {'filing_id': UUID(int=1), 'html_document_id': UUID(int=2), 'xml_document_id': UUID(int=3)}
ROOT = Path(__file__).resolve().parents[1]
CASH = ['NetCashProvidedByUsedInOperatingActivities', 'NetCashProvidedByUsedInInvestingActivities',
        'NetCashProvidedByUsedInFinancingActivities', 'CashAndCashEquivalentsAtCarryingValue']


def fixture(*, complete_duplicate=False, intervening=''):
    definitions = [
        ('income', 'Consolidated Statements of Income', ['Revenues', 'OperatingIncomeLoss', 'NetIncomeLoss']),
        ('balance', 'Consolidated Balance Sheets', ['Assets', 'Liabilities', 'StockholdersEquity']),
        ('cash1', 'Consolidated Statements of Cash Flows', CASH if complete_duplicate else CASH[:2]),
        ('cash2', 'Consolidated Statements of Cash Flows', CASH if complete_duplicate else CASH[2:]),
    ]
    statements, facts = [], {}
    for key, title, concepts in definitions:
        header = 'December 31, 2025' if key == 'balance' else 'Years Ended December 31, 2025'
        rows = []
        for index, concept in enumerate(concepts):
            ident = f'{key}-{index}'
            instant = key == 'balance' or concept == CASH[-1]
            period = {'instant': '2025-12-31'} if instant else {'startDate': '2025-01-01', 'endDate': '2025-12-31'}
            label = 'Cash and cash equivalents, end of period' if concept == CASH[-1] else concept
            context = 'end' if instant else 'year'
            rows.append(f'<tr><td>{label}</td><td><ix:nonfraction id="{ident}" name="g:{concept}" '
                        f'contextref="{context}" unitref="usd">1</ix:nonfraction></td></tr>')
            facts[ident] = {'xml_id': ident, 'concept': '{' + GAAP + '}' + concept,
                            'context_ref': context, 'unit_ref': 'usd',
                            'unit': {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []},
                            'period': period, 'dimensions': [], 'raw_value': '1', 'is_nil': False,
                            'entity': {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'}}
        if key == 'cash2':
            statements.append(intervening)
        statements.append(f'<p>{title}</p><table id="{key}"><tr><td></td><td>{header}</td></tr>{"".join(rows)}</table>')
    source = (f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
              + ''.join(statements) + '</body></html>')
    return html.document_fromstring(source).getroottree(), facts


def input_facts(facts):
    return [FactInput(
        filing_id=SCOPE['filing_id'], source_document_id=SCOPE['xml_document_id'], extraction_version='facts-v1',
        occurrence_key=key, concept_namespace=record['concept'][1:].split('}')[0],
        concept_name=record['concept'].split('}')[1], numeric_value=Decimal(record['raw_value']), raw_value=record['raw_value'],
        period_kind='instant' if 'instant' in record['period'] else 'duration', unit=record['unit'],
        instant_date=date.fromisoformat(record['period']['instant']) if 'instant' in record['period'] else None,
        period_start=date.fromisoformat(record['period']['startDate']) if 'startDate' in record['period'] else None,
        period_end=date.fromisoformat(record['period']['endDate']) if 'endDate' in record['period'] else None,
        dimensions=record['dimensions'], source_reference={'element_id': key, 'context_ref': record['context_ref'],
            'unit_ref': record['unit_ref'], 'entity': record['entity']},
    ) for key, record in facts.items()]


class StatementSegmentTests(unittest.TestCase):
    def test_complementary_segments_preserve_original_nodes_and_order(self):
        tree, facts = fixture()
        segments = select_statement_segments(tree, facts)
        self.assertEqual([segment.table_key for segment in segments],
                         ['income_statement', 'balance_sheet', 'cash_flow.segment_1', 'cash_flow.segment_2'])
        for index, segment in enumerate(segments[2:], 1):
            self.assertIs(segment.element, tree.xpath(f'//table[@id="cash{index}"]')[0])
            self.assertEqual(segment.statement_type, 'cash_flow')
            self.assertEqual(segment.group['segment_index'], index)
            self.assertEqual(segment.group['segment_count'], 2)
            self.assertEqual(segment.group['segment_keys'], ['cash_flow.segment_1', 'cash_flow.segment_2'])
            self.assertEqual(set(segment.group['combined_concept_evidence'][0]['groups']), {'0', '1', '2', '3'})
        self.assertEqual([segment.group for segment in select_statement_segments(tree, input_facts(facts))],
                         [segment.group for segment in segments])

    def test_two_independently_complete_statements_are_ambiguous(self):
        tree, facts = fixture(complete_duplicate=True)
        with self.assertRaises(TableExtractionError):
            select_statement_segments(tree, facts)

    def test_a_complete_first_segment_does_not_drop_explicit_supplemental_continuation(self):
        tree, facts = fixture(complete_duplicate=True)
        table = tree.xpath('//table[@id="cash2"]')[0]
        table.getprevious().text += ' (continued)'
        for index, name in enumerate(('IncreaseDecreaseInAccountsReceivable', 'IncreaseDecreaseInAccountsPayable')):
            tree.xpath(f'//*[@id="cash2-{index}"]')[0].set('name', 'g:' + name)
            facts[f'cash2-{index}']['concept'] = '{' + GAAP + '}' + name
        for index in (2, 3):
            node = tree.xpath(f'//*[@id="cash2-{index}"]')[0]
            node.getparent().remove(node)
        segments = select_statement_segments(tree, facts)
        self.assertEqual([segment.table_key for segment in segments[-2:]], ['cash_flow.segment_1', 'cash_flow.segment_2'])

    def test_an_untagged_continuation_cannot_be_silently_dropped(self):
        tree, facts = fixture(complete_duplicate=True)
        table = tree.xpath('//table[@id="cash2"]')[0]
        table.getprevious().text += ' (continued)'
        for node in table.iter():
            if str(node.tag).endswith('nonfraction'):
                node.tag = 'span'
        with self.assertRaises(TableExtractionError):
            select_statement_segments(tree, facts)

    def test_directly_adjacent_tables_can_use_an_internal_repeated_caption(self):
        tree, facts = fixture()
        table = tree.xpath('//table[@id="cash2"]')[0]
        heading = table.getprevious()
        heading.getparent().remove(heading)
        heading.tag = 'caption'
        table.insert(0, heading)
        self.assertEqual(len(select_statement_segments(tree, facts)), 4)

    def test_incomplete_combined_statement_cannot_pass(self):
        tree, facts = fixture()
        node = tree.xpath('//*[@id="cash2-1"]')[0]
        node.getparent().remove(node)
        with self.assertRaises(TableExtractionError):
            select_statement_segments(tree, facts)

    def test_changed_second_segment_header_or_fact_period_is_rejected(self):
        for change_header in (True, False):
            tree, facts = fixture()
            if change_header:
                tree.xpath('//table[@id="cash2"]/tr[1]/td[2]')[0].text = 'Years Ended December 31, 2024'
            else:
                facts['cash2-0']['period'] = {'startDate': '2025-10-01', 'endDate': '2025-12-31'}
            with self.subTest(header=change_header), self.assertRaises(TableExtractionError):
                select_statement_segments(tree, facts)

    def test_different_entity_currency_or_parent_dimensions_cannot_be_joined(self):
        for change in ('entity', 'unit', 'dimensions'):
            tree, facts = fixture()
            for key in ('cash2-0', 'cash2-1'):
                if change == 'entity':
                    facts[key]['entity']['identifier'] = '0000000002'
                elif change == 'unit':
                    facts[key]['unit'] = {'numerator': ['{http://www.xbrl.org/2003/iso4217}EUR'], 'denominator': []}
                else:
                    facts[key]['dimensions'] = [{'kind': 'explicit', 'dimension': '{http://fasb.org/srt/2025}ConsolidationItemsAxis',
                                                 'member': '{http://fasb.org/srt/2025}ParentCompanyMember'}]
            with self.subTest(change=change), self.assertRaises(TableExtractionError):
                select_statement_segments(tree, facts)

    def test_intervening_financial_table_statement_or_note_breaks_adjacency(self):
        for middle in ('<table><tr><td><ix:nonfraction id="other" name="g:Assets">2</ix:nonfraction></td></tr></table>',
                       '<p>Consolidated Balance Sheets</p>', '<p>Note 3. Cash flow disclosures</p>'):
            tree, facts = fixture(intervening=middle)
            with self.subTest(middle=middle), self.assertRaises(TableExtractionError):
                select_statement_segments(tree, facts)

    def test_page_furniture_is_allowed_without_skipping_financial_cells(self):
        tree, facts = fixture(intervening='<table><tr><td>Example Company Form 10-Q Page 7</td></tr></table><p>Table of Contents</p>')
        self.assertEqual(len(select_statement_segments(tree, facts)), 4)

    def test_missing_or_changed_matching_heading_fails(self):
        tree, facts = fixture()
        tree.xpath('//table[@id="cash2"]')[0].getprevious().text = 'Cash Flows Statements'
        with self.assertRaises(TableExtractionError):
            select_statement_segments(tree, facts)

    def test_numeric_dates_and_exact_week_counts_are_gated_to_v2(self):
        headings = ['36 Weeks Ended', '9/5/2026']
        self.assertIsNone(header_period(headings))
        self.assertEqual(header_period(headings, advanced=True), {'startDate': '2025-12-28', 'endDate': '2026-09-05'})
        for values in (['36 Weeks Ended', '9/5/2026', '9/6/2025'], ['13 and 26 Weeks Ended 9/5/2026'],
                       ['36 Weeks Ended 2/30/2026']):
            with self.subTest(values=values):
                self.assertIsNone(header_period(values, advanced=True))


class ArchivedSegmentTests(unittest.TestCase):
    def paths(self, accession):
        directory = ROOT / 'data/raw/sec' / accession
        html_paths, xml_paths = list(directory.glob('*.htm')), list(directory.glob('*_htm.xml'))
        if len(html_paths) != 1 or len(xml_paths) != 1:
            self.skipTest('Archived segment acceptance source is unavailable')
        return html_paths[0], xml_paths[0]

    def test_stz_and_pepsi_keep_each_source_table_and_local_cell_coordinates(self):
        for accession in ('0000016918-26-000040', '0000077476-26-000048'):
            with self.subTest(accession=accession):
                html_path, xml_path = self.paths(accession)
                with self.assertRaises(TableExtractionError):
                    extract_tables(html_path, xml_path, **SCOPE)
                tables = extract_tables(html_path, xml_path, tables_version='tables-v2', **SCOPE)
                self.assertEqual(len(tables), 4)
                self.assertTrue(all(table.structure['schema_version'] == 1 for table in tables))
                self.assertTrue(all(table.structure['inventory'].get('unresolved', 0) == 0 for table in tables))
                tree = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
                facts = extract_facts(xml_path, filing_id=SCOPE['filing_id'], source_document_id=SCOPE['xml_document_id']).require_complete().facts
                self.assertTrue(validate_statement_segments(tables, tree, facts))
                for table in tables[2:]:
                    [node] = tree.xpath(table.structure['source_xpath'])
                    self.assertEqual(node.tag, 'table')
                    self.assertEqual(table.structure['rows'][0][0]['cell_id'], 'r000c000')
                    for row in table.structure['rows']:
                        for cell in row:
                            [anchor] = tree.xpath(cell['source_xpath'])
                            self.assertIn(node, anchor.iterancestors())
                self.assertNotEqual(tables[2].structure['source_xpath'], tables[3].structure['source_xpath'])

    def test_group_metadata_missing_member_and_order_tampering_fail_reselection(self):
        html_path, xml_path = self.paths('0000016918-26-000040')
        tables = extract_tables(html_path, xml_path, tables_version='tables-v2', **SCOPE)
        tree = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
        facts = xml_evidence(xml_path, include_entity=True)
        changed = deepcopy(tables[2].structure)
        changed['statement_group']['segment_index'] = 2
        malformed = [*tables[:2], replace(tables[2], structure=changed), tables[3]]
        for records in (tables[:-1], [*tables[:2], tables[3], tables[2]], malformed):
            with self.assertRaises(TableExtractionError):
                validate_statement_segments(records, tree, facts)

    def test_qualified_segmented_v2_payloads_remain_unchanged(self):
        expected = {'0000016918-26-000040': 'a54a06ab0156e0024b048a5ce3dd8f2f43077bab3c7b4506040c832d04d6a41f',
                    '0000077476-26-000048': 'ad18a529052fad124a88341777e6dbc0d6de745c7f90d554290cb5e8b29045ef',
                    '0000940944-26-000042': '0cdfd6055dca5b3d0f600a43fd01a8437084dbe69fc5a35a82f3b168ef01b0d8'}
        for accession, digest in expected.items():
            tables = extract_tables(*self.paths(accession), tables_version='tables-v2', **SCOPE)
            payload = json.dumps([asdict(table) for table in tables], default=str, sort_keys=True).encode()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)

    def test_darden_retains_middle_reconciliation_and_supplemental_continuation(self):
        html_path, xml_path = self.paths('0000940944-26-000042')
        tables = extract_tables(html_path, xml_path, tables_version='tables-v2', **SCOPE)
        self.assertEqual([table.table_key for table in tables],
                         ['income_statement', 'balance_sheet', 'cash_flow.segment_1', 'cash_flow.segment_2', 'cash_flow.segment_3'])
        self.assertTrue(all(table.structure['inventory'].get('unresolved', 0) == 0 for table in tables))
        self.assertEqual(tables[3].structure['source_xpath'], '/html/body/div[97]/table')
        self.assertEqual(tables[3].structure['statement_group']['segment_role'], 'cash_reconciliation')
        self.assertEqual(tables[3].structure['statement_group']['segment_roles'], ['statement', 'cash_reconciliation', 'statement'])
        refs = [ref for row in tables[3].structure['rows'] for cell in row for ref in cell['fact_references']]
        self.assertEqual({ref['xml_id'] for ref in refs}, {f'f-{index}' for index in range(266, 272)})
        self.assertEqual({tuple(ref['period'].items()) for ref in refs},
                         {(('instant', '2026-08-30'),), (('instant', '2025-08-24'),)})
        tree = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
        facts = extract_facts(xml_path, filing_id=SCOPE['filing_id'], source_document_id=SCOPE['xml_document_id']).require_complete().facts
        self.assertTrue(validate_statement_segments(tables, tree, facts))
        self.assertEqual(tables[3].structure['rows'][0][0]['cell_id'], 'r000c000')

    def test_darden_reconciliation_wrong_date_concept_heading_or_total_cannot_join(self):
        html_path, xml_path = self.paths('0000940944-26-000042')
        for change in ('date', 'concept', 'heading', 'total'):
            document = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
            instance = etree.parse(str(xml_path), etree.XMLParser(resolve_entities=False, no_network=True))
            reconciliation = document.xpath('/html/body/div[97]/table')[0]
            if change == 'date':
                for node in reconciliation.iter():
                    if node.text and 'August' in node.text:
                        node.text = node.text.replace('30', '29')
            elif change == 'heading':
                for node in reconciliation.iter():
                    if node.text:
                        node.text = node.text.replace('Reconciliation of cash', 'Supplemental balances of cash')
            elif change == 'concept':
                target = document.xpath('//*[@id="f-268"]')[0]
                target.set('name', 'us-gaap:AccountsPayableCurrent')
                fact = instance.xpath('//*[@id="f-268"]')[0]
                fact.tag = '{' + etree.QName(fact).namespace + '}AccountsPayableCurrent'
            else:
                target = document.xpath('//*[@id="f-270"]')[0]
                for child in list(target):
                    target.remove(child)
                target.text = '228.7'
                instance.xpath('//*[@id="f-270"]')[0].text = '228700000'
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                changed_html, changed_xml = Path(directory) / 'filing.htm', Path(directory) / 'filing.xml'
                changed_html.write_bytes(html.tostring(document, encoding='utf-8'))
                changed_xml.write_bytes(etree.tostring(instance, encoding='utf-8'))
                with self.assertRaises(TableExtractionError):
                    extract_tables(changed_html, changed_xml, tables_version='tables-v2', **SCOPE)

    def test_exact_heading_inside_text_only_layout_table_is_recognized(self):
        from sec_pipeline.tables import select_statements
        tree, facts = fixture(complete_duplicate=True)
        second = tree.xpath('//table[@id="cash2"]')[0]
        second.getparent().remove(second.getprevious())
        second.getparent().remove(second)
        income = tree.xpath('//table[@id="income"]')[0]
        original = income.getprevious()
        wrapper = html.fromstring('<div><table><tr><td>Consolidated Statements of Income</td></tr></table></div>')
        original.getparent().replace(original, wrapper)
        self.assertIs(select_statements(tree)['income_statement'], income)
        wrapper.xpath('.//td')[0].text = 'Consolidated Statements of Income 2025 123'
        with self.assertRaises(TableExtractionError):
            select_statements(tree)

    def test_casey_inherited_headers_cite_original_cells_without_inventing_display_text(self):
        html_path, xml_path = self.paths('0000726958-26-000085')
        tables = extract_tables(html_path, xml_path, tables_version='tables-v2', **SCOPE)
        self.assertEqual([table.table_key for table in tables], ['income_statement', 'balance_sheet.segment_1',
                         'balance_sheet.segment_2', 'cash_flow.segment_1', 'cash_flow.segment_2'])
        self.assertTrue(all(table.structure['inventory'].get('unresolved', 0) == 0 for table in tables))
        tree = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
        facts = extract_facts(xml_path, filing_id=SCOPE['filing_id'], source_document_id=SCOPE['xml_document_id']).require_complete().facts
        self.assertTrue(validate_statement_segments(tables, tree, facts))
        for table in (tables[2], tables[4]):
            inheritance = table.structure['statement_group']['header_inheritance']
            [source_table] = tree.xpath(inheritance['source_xpath'])
            for descriptor in inheritance['columns'].values():
                for source in descriptor['source_cells']:
                    [node] = tree.xpath(source['source_xpath'])
                    self.assertIn(source_table, node.iterancestors())
                    self.assertEqual(' '.join(''.join(node.itertext()).split()), source['display_text'])
            for row in table.structure['rows']:
                for cell in row:
                    if cell['classification'] == 'resolved':
                        self.assertEqual(cell['column_headings'], [])
                        self.assertEqual(cell['column_period'], inheritance['columns'][str(cell['column'])]['period'])
        self.assertEqual({tuple(item['period'].items()) for item in tables[4].structure['statement_group']['closure_evidence']},
                         {(('endDate', '2026-07-31'), ('startDate', '2026-05-01')),
                          (('endDate', '2025-07-31'), ('startDate', '2025-05-01'))})
        # The separate supplemental-disclosures table is not a primary segment;
        # its original fact occurrences still exist in the full extraction.
        supplemental_ids = {node.get('id') for node in tree.xpath('/html/body/div[113]/table')[0].iter()
                            if str(node.tag).endswith('nonfraction')}
        self.assertTrue(supplemental_ids <= {fact.occurrence_key for fact in facts})
        self.assertNotIn('/html/body/div[113]/table', {table.structure['source_xpath'] for table in tables})

    def test_casey_balance_inheritance_rejects_changed_evidence(self):
        html_path, xml_path = self.paths('0000726958-26-000085')
        for change in ('total', 'date', 'unit', 'width', 'intervening_table', 'header_text'):
            document = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
            instance = etree.parse(str(xml_path), etree.XMLParser(resolve_entities=False, no_network=True))
            first = document.xpath('/html/body/div[63]/table')[0]
            second = document.xpath('/html/body/div[64]/table')[0]
            if change == 'total':
                document.xpath('//*[@id="f-84"]')[0].text = '9,121,264'
                instance.xpath('//*[@id="f-84"]')[0].text = '9121264000'
            elif change in ('date', 'header_text'):
                for node in first.iter():
                    if node.text and 'July 31,' in node.text:
                        node.text = node.text.replace('July 31,', 'July 30,' if change == 'date' else '')
            elif change == 'unit':
                fact = instance.xpath('//*[@id="f-84"]')[0]
                unit = deepcopy(instance.xpath('//*[@id="' + fact.get('unitRef') + '"]')[0])
                unit.set('id', 'wrong-currency')
                unit.find('{http://www.xbrl.org/2003/instance}measure').text = 'iso4217:EUR'
                instance.getroot().append(unit)
                fact.set('unitRef', 'wrong-currency')
                document.xpath('//*[@id="f-84"]')[0].set('unitref', 'wrong-currency')
            elif change == 'width':
                second.xpath('./tr')[0].append(html.fromstring('<td></td>'))
            else:
                wrapper = html.fromstring('<div><table><tr><td>Other financial data: 123</td></tr></table></div>')
                second.getparent().addprevious(wrapper)
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                changed_html, changed_xml = Path(directory) / 'filing.htm', Path(directory) / 'filing.xml'
                changed_html.write_bytes(html.tostring(document, encoding='utf-8'))
                changed_xml.write_bytes(etree.tostring(instance, encoding='utf-8'))
                with self.assertRaises(TableExtractionError):
                    extract_tables(changed_html, changed_xml, tables_version='tables-v2', **SCOPE)

    def test_casey_cash_inheritance_rejects_period_arithmetic_and_boundary_changes(self):
        html_path, xml_path = self.paths('0000726958-26-000085')
        for change in ('closing', 'net_change', 'period', 'intervening_table', 'supplemental_title'):
            document = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
            instance = etree.parse(str(xml_path), etree.XMLParser(resolve_entities=False, no_network=True))
            if change in ('closing', 'net_change'):
                ident, lexical, raw = (('f-213', '524,060', '524060000') if change == 'closing'
                                       else ('f-209', '1,069', '1069000'))
                document.xpath(f'//*[@id="{ident}"]')[0].text = lexical
                instance.xpath(f'//*[@id="{ident}"]')[0].text = raw
            elif change == 'period':
                fact = instance.xpath('//*[@id="f-213"]')[0]
                context = deepcopy(instance.xpath('//*[@id="' + fact.get('contextRef') + '"]')[0])
                context.set('id', 'wrong-closing-date')
                context.find('.//{http://www.xbrl.org/2003/instance}instant').text = '2026-07-30'
                instance.getroot().append(context)
                fact.set('contextRef', 'wrong-closing-date')
                document.xpath('//*[@id="f-213"]')[0].set('contextref', 'wrong-closing-date')
            elif change == 'intervening_table':
                document.xpath('/html/body/div[101]')[0].addprevious(
                    html.fromstring('<div><table><tr><td>Other financial data: 123</td></tr></table></div>'))
            else:
                for node in document.xpath('/html/body/div[112]')[0].iter():
                    if node.text:
                        node.text = node.text.replace('SUPPLEMENTAL DISCLOSURES OF CASH FLOWS INFORMATION', 'OTHER DATA')
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                changed_html, changed_xml = Path(directory) / 'filing.htm', Path(directory) / 'filing.xml'
                changed_html.write_bytes(html.tostring(document, encoding='utf-8'))
                changed_xml.write_bytes(etree.tostring(instance, encoding='utf-8'))
                with self.assertRaises(TableExtractionError):
                    extract_tables(changed_html, changed_xml, tables_version='tables-v2', **SCOPE)

    def test_single_statement_v2_changes_only_extraction_version(self):
        html_path, xml_path = self.paths('0001193125-26-191507')
        original = extract_tables(html_path, xml_path, **SCOPE)
        versioned = extract_tables(html_path, xml_path, tables_version='tables-v2', **SCOPE)
        self.assertEqual(original, [replace(table, extraction_version='tables-v1') for table in versioned])
        digest = hashlib.sha256(json.dumps([asdict(table) for table in original], default=str, sort_keys=True).encode()).hexdigest()
        self.assertEqual(digest, 'b98d984cd627e19398f44f35db534086fb383c07ad533afdd8bcb85a786922b2')


if __name__ == '__main__':
    unittest.main()
