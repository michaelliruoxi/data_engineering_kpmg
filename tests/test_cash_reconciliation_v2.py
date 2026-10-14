"""Bounded cash-component disclosures preserve explicit source occurrences."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

from lxml import html

from sec_pipeline.cash_reconciliation import bind_cash_reconciliations, validate_cash_reconciliations
from sec_pipeline.tables import (
    TableExtractionError, extract_tables, layout_grid, validate_statement_segments, xml_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
SCOPE = {'filing_id': UUID(int=1), 'html_document_id': UUID(int=2), 'xml_document_id': UUID(int=3)}
GAAP = 'http://fasb.org/us-gaap/2026'
TOTAL = 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
PERIOD = {'startDate': '2026-06-01', 'endDate': '2026-08-31'}


def fixture():
    definitions = [('closing', 'Cash, cash equivalents and restricted cash, end of period', TOTAL, '100'),
                   ('cash', 'Cash and cash equivalents', 'CashAndCashEquivalentsAtCarryingValue', '80'),
                   ('restricted', 'Restricted cash', 'RestrictedCash', '20'),
                   ('total', 'Total cash, cash equivalents and restricted cash', TOTAL, '100')]
    rows = ['<tr><td></td><td>Three Months Ended August 31, 2026</td></tr>']
    facts = {}
    for key, label, concept, value in definitions:
        if key == 'cash':
            rows.append('<tr><td>Reconciliation of cash, cash equivalents, and restricted cash</td><td></td></tr>')
        rows.append(f'<tr id="row-{key}"><td>{label}</td><td><ix:nonfraction id="{key}" name="g:{concept}" '
                    f'contextref="end" unitref="usd">{value}</ix:nonfraction></td></tr>')
        facts[key] = {'xml_id': key, 'concept': '{' + GAAP + '}' + concept, 'context_ref': 'end', 'unit_ref': 'usd',
                      'period': {'instant': PERIOD['endDate']}, 'raw_value': value, 'is_nil': False, 'dimensions': [],
                      'entity': {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'},
                      'unit': {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []}}
    rows.append('<tr><td>Supplemental disclosures of cash flow information</td><td></td></tr>')
    source = (f'<html xmlns:g="{GAAP}" xmlns:c="https://example.test/2026" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
              '<body><table>' + ''.join(rows) + '</table></body></html>')
    tree = html.document_fromstring(source).getroottree()
    return tree, facts


def bind(tree, facts, xml_path=Path('missing.xml')):
    rows, _ = layout_grid(tree.xpath('//table')[0], tree)
    return bind_cash_reconciliations(rows, {1: PERIOD}, facts, {'g': GAAP},
                                    {key: 1 for key in facts}, xml_path)


class CashReconciliationTests(unittest.TestCase):
    def test_complete_components_retain_ids_dates_and_main_duration(self):
        tree, facts = fixture()
        original = deepcopy(facts)
        bindings, groups = bind(tree, facts)
        self.assertEqual(len(bindings), 3)
        self.assertEqual(facts, original)
        proof = groups[0]['periods'][0]
        self.assertEqual(proof['column_period'], PERIOD)
        self.assertEqual(proof['period'], {'instant': PERIOD['endDate']})
        self.assertEqual(proof['component_xml_ids'], ['cash', 'restricted'])
        self.assertEqual(proof['closing_xml_id'], 'closing')
        self.assertEqual(proof['total_xml_id'], 'total')
        self.assertEqual(groups[0]['last_data_row'], 5)
        self.assertTrue(all(binding['period'] == {'instant': PERIOD['endDate']} for binding in bindings.values()))

    def test_wrong_date_scope_dimension_unit_nil_or_value_cannot_bind(self):
        for field in ('date', 'entity', 'dimension', 'unit', 'nil', 'amount', 'closing', 'concept', 'inline'):
            tree, facts = fixture()
            fact = facts['restricted']
            if field == 'date':
                fact['period']['instant'] = '2026-08-30'
            elif field == 'entity':
                fact['entity']['identifier'] = '0000000002'
            elif field == 'dimension':
                fact['dimensions'] = [{'kind': 'explicit', 'dimension': '{test}Axis', 'member': '{test}ParentCompanyMember'}]
            elif field == 'unit':
                fact['unit']['numerator'] = ['{http://www.xbrl.org/2003/iso4217}EUR']
            elif field == 'nil':
                fact['is_nil'] = True
            elif field == 'amount':
                fact['raw_value'] = '21'
                tree.xpath('//*[@id="restricted"]')[0].text = '21'
            elif field == 'closing':
                facts['closing']['raw_value'] = '101'
                tree.xpath('//*[@id="closing"]')[0].text = '101'
            elif field == 'concept':
                fact['concept'] = '{' + GAAP + '}Assets'
                tree.xpath('//*[@id="restricted"]')[0].set('name', 'g:Assets')
            elif field == 'inline':
                tree.xpath('//*[@id="restricted"]')[0].text = '21'
            with self.subTest(field=field), self.assertRaises(TableExtractionError):
                bind(tree, facts)

    def test_no_heading_no_closing_or_an_intervening_row_cannot_supply_a_binding(self):
        tree, facts = fixture()
        tree.xpath('//tr[3]/td[1]')[0].text = 'Other assets'
        self.assertEqual(bind(tree, facts), ({}, []))
        tree, facts = fixture()
        tree.xpath('//*[@id="row-closing"]/td[1]')[0].text = 'Other cash information'
        self.assertEqual(bind(tree, facts), ({}, []))
        tree, facts = fixture()
        row = html.fragment_fromstring('<tr><td>Accounts receivable</td><td>5</td></tr>')
        tree.xpath('//*[@id="row-restricted"]')[0].addprevious(row)
        with self.assertRaises(TableExtractionError):
            bind(tree, facts)

    def test_missing_total_untagged_zero_or_duplicate_component_is_rejected(self):
        for change in ('total', 'untagged', 'duplicate'):
            tree, facts = fixture()
            if change == 'total':
                node = tree.xpath('//*[@id="row-total"]')[0]
                node.getparent().remove(node)
            elif change == 'untagged':
                node = tree.xpath('//*[@id="restricted"]')[0]
                node.tag = 'span'
                node.text = '—'
            else:
                node = deepcopy(tree.xpath('//*[@id="row-restricted"]')[0])
                node.set('id', 'duplicate-row')
                tagged = node.xpath('.//*[name()="ix:nonfraction"]')[0]
                tagged.set('id', 'duplicate')
                facts['duplicate'] = deepcopy(facts['restricted']) | {'xml_id': 'duplicate'}
                tree.xpath('//*[@id="row-restricted"]')[0].addnext(node)
            with self.subTest(change=change), self.assertRaises(TableExtractionError):
                bind(tree, facts)

    def test_a_tagged_zero_is_allowed_without_inference(self):
        tree, facts = fixture()
        for key, value in (('cash', '100'), ('restricted', '0')):
            facts[key]['raw_value'] = value
            tree.xpath(f'//*[@id="{key}"]')[0].text = value
        bindings, _ = bind(tree, facts)
        self.assertEqual(len(bindings), 3)

    def test_exponent_separation_cannot_round_away_a_cash_component(self):
        for cash, restricted in (('1E30', '1'), ('100', '1E-40'), ('1E4097', '1')):
            tree, facts = fixture()
            for key, value in (('cash', cash), ('restricted', restricted), ('total', cash), ('closing', cash)):
                facts[key]['raw_value'] = value
                tree.xpath(f'//*[@id="{key}"]')[0].text = value
            with self.subTest(cash=cash, restricted=restricted), self.assertRaises(TableExtractionError):
                bind(tree, facts)

    def test_custom_cash_component_requires_archived_instant_monetary_declaration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'filing.xml'
            path.write_text('<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:link="http://www.xbrl.org/2003/linkbase" '
                            'xmlns:xlink="http://www.w3.org/1999/xlink"><link:schemaRef xlink:href="issuer.xsd"/></xbrl>')
            element = '<xs:element name="RestrictedCashIncludedInOtherAssets" id="issuer_cash" type="xbrli:monetaryItemType" '
            element += 'substitutionGroup="xbrli:item" xbrli:periodType="instant" xbrli:balance="debit" abstract="false"/>'
            schema = ('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xbrli="http://www.xbrl.org/2003/instance" '
                      'targetNamespace="https://example.test/2026">' + element + '</xs:schema>')
            tree, facts = fixture()
            facts['restricted']['concept'] = '{https://example.test/2026}RestrictedCashIncludedInOtherAssets'
            tree.xpath('//*[@id="restricted"]')[0].set('name', 'c:RestrictedCashIncludedInOtherAssets')
            schema_path = Path(directory) / 'issuer.xsd'
            schema_path.write_text(schema)
            _, groups = bind(tree, facts, path)
            self.assertEqual(groups[0]['extension_concepts'][0]['element_id'], 'issuer_cash')
            for changed in (schema.replace('monetaryItemType', 'stringItemType'),
                            schema.replace('periodType="instant"', 'periodType="duration"'),
                            schema.replace('balance="debit"', 'balance="credit"'),
                            schema.replace('abstract="false"', 'abstract="true"'),
                            schema.replace('</xs:schema>', element + '</xs:schema>')):
                schema_path.write_text(changed)
                with self.subTest(schema=changed), self.assertRaises(TableExtractionError):
                    bind(tree, facts, path)


class ArchivedCashReconciliationTests(unittest.TestCase):
    def sources(self, accession):
        folder = ROOT / 'data/raw/sec' / accession
        html_paths, xml_paths = list(folder.glob('*.htm')), list(folder.glob('*_htm.xml'))
        if len(html_paths) != 1 or len(xml_paths) != 1:
            self.skipTest('Archived reconciliation source unavailable')
        return html_paths[0], xml_paths[0]

    def test_five_actual_sources_have_complete_and_independently_verified_blocks(self):
        for accession, count in (('0000723531-26-000006', 8), ('0001327567-26-000023', 12),
                                 ('0001628280-26-061160', 6), ('0001474432-26-000088', 6),
                                 ('0001104659-26-102213', 6)):
            with self.subTest(accession=accession):
                html_path, xml_path = self.sources(accession)
                tables = extract_tables(html_path, xml_path, tables_version='tables-v2', **SCOPE)
                self.assertTrue(all(table.structure['inventory'].get('unresolved', 0) == 0 for table in tables))
                cash = next(table for table in tables if table.statement_type == 'cash_flow')
                bound = [cell for row in cash.structure['rows'] for cell in row if 'period_binding' in cell]
                self.assertEqual(len(bound), count)
                self.assertTrue(all(set(cell['column_period']) == {'startDate', 'endDate'} for cell in bound))
                self.assertTrue(all(cell['fact_references'][0]['period'] == cell['period_binding']['period'] for cell in bound))
                tree = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8'))
                self.assertTrue(validate_cash_reconciliations(tables, tree, xml_path))
                self.assertTrue(validate_statement_segments(tables, tree, xml_evidence(xml_path, include_entity=True), xml_path=xml_path))

    def test_metadata_binding_dates_and_source_occurrences_cannot_be_substituted(self):
        html_path, xml_path = self.sources('0000723531-26-000006')
        tables = extract_tables(html_path, xml_path, tables_version='tables-v2', **SCOPE)
        tree = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8'))
        for change in ('metadata', 'binding', 'date', 'source', 'removed'):
            altered = list(tables)
            index = next(index for index, table in enumerate(tables) if table.statement_type == 'cash_flow')
            structure = deepcopy(tables[index].structure)
            cell = next(cell for row in structure['rows'] for cell in row if 'period_binding' in cell)
            if change == 'metadata':
                structure['cash_reconciliation_evidence'][0]['periods'][0]['total_xml_id'] = 'other'
            elif change == 'binding':
                cell['period_binding']['period']['instant'] = '2026-08-30'
            elif change == 'date':
                cell['column_period'] = dict(cell['period_binding']['period'])
            elif change == 'source':
                cell['fact_references'][0]['xml_id'] = 'other'
            else:
                structure.pop('cash_reconciliation_evidence')
                for row in structure['rows']:
                    for item in row:
                        item.pop('period_binding', None)
            altered[index] = replace(tables[index], structure=structure)
            with self.subTest(change=change), self.assertRaises(TableExtractionError):
                validate_cash_reconciliations(altered, tree, xml_path)


if __name__ == '__main__':
    unittest.main()
