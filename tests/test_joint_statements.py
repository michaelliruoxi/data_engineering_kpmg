"""Reporting-parent selection cannot borrow subsidiary or note statements."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest
from uuid import UUID

from lxml import html

from sec_pipeline import tables as t
from sec_pipeline.fact_extraction import extract_facts
from sec_pipeline.joint_statements import select_joint_statement_segments


ROOT = Path(__file__).resolve().parents[1]
GAAP = 'http://fasb.org/us-gaap/2026'
DEI = 'http://xbrl.sec.gov/dei/2026'
ENTITY = {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'}
UNIT = {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []}
PERIOD = {'startDate': '2026-01-01', 'endDate': '2026-03-31'}


def fixture():
    facts, dei = {}, []
    for owner, name in ((1, 'Primary Corporation'), (2, 'Affiliate Corporation')):
        context = 'primary' if owner == 1 else 'affiliate'
        dimensions = [] if owner == 1 else [{'kind': 'explicit', 'dimension': '{' + DEI + '}LegalEntityAxis',
                                             'member': '{https://issuer.test/2026}AffiliateMember'}]
        fields = {'EntityCentralIndexKey': str(owner), 'EntityRegistrantName': name}
        if owner == 1:
            fields.update(DocumentType='10-Q', DocumentPeriodEndDate='2026-03-31')
        for field, value in fields.items():
            identifier = f'dei-{owner}-{field}'
            facts[identifier] = {'xml_id': identifier, 'concept': '{' + DEI + '}' + field, 'context_ref': context,
                'unit_ref': None, 'unit': None, 'raw_value': value, 'is_nil': False, 'dimensions': dimensions,
                'entity': ENTITY, 'period': PERIOD}
            dei.append(f'<ix:nonNumeric id="{identifier}" name="dei:{field}" contextRef="{context}">{value}</ix:nonNumeric>')
    contexts = '<xbrli:context id="primary"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">1</xbrli:identifier></xbrli:entity></xbrli:context>'
    contexts += '<xbrli:context id="affiliate"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">1</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="dei:LegalEntityAxis">ex:AffiliateMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity></xbrli:context>'
    definitions = {
        'income_statement': ('Consolidated Statements of Income', [('Revenue', 'Revenues', '100'), ('Operating income', 'OperatingIncomeLoss', '30'), ('Net income', 'NetIncomeLoss', '20')]),
        'balance_sheet': ('Consolidated Balance Sheets', [('Assets', 'Assets', '200'), ('Liabilities and equity', 'LiabilitiesAndStockholdersEquity', '200'), ('Equity', 'StockholdersEquity', '100')]),
        'cash_flow': ('Consolidated Statements of Cash Flows', [('Operating cash', 'NetCashProvidedByUsedInOperatingActivities', '30'), ('Investing cash', 'NetCashProvidedByUsedInInvestingActivities', '-10'), ('Financing cash', 'NetCashProvidedByUsedInFinancingActivities', '-5'), ('Cash, end of period', 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents', '25')]),
    }
    tables = []
    for owner, name in ((1, 'Primary Corporation'), (2, 'Affiliate Corporation')):
        dims = facts[f'dei-{owner}-EntityCentralIndexKey']['dimensions']
        for kind, (title, rows) in definitions.items():
            label = 'March 31, 2026' if kind == 'balance_sheet' else 'Three months ended March 31, 2026'
            parts = [f'<table id="{owner}-{kind}"><tr><td colspan="2">{name}</td></tr><tr><td colspan="2">{title}</td></tr><tr><td></td><td>{label}</td></tr>']
            for index, (row_label, concept, value) in enumerate(rows):
                identifier = f'f-{owner}-{kind}-{index}'
                period = {'instant': '2026-03-31'} if kind == 'balance_sheet' or 'end of period' in row_label else PERIOD
                facts[identifier] = {'xml_id': identifier, 'concept': '{' + GAAP + '}' + concept,
                    'context_ref': f'{owner}-{kind}-{index}', 'unit_ref': 'usd', 'unit': UNIT,
                    'raw_value': value, 'is_nil': False, 'dimensions': dims, 'entity': ENTITY, 'period': period}
                parts.append(f'<tr><td>{row_label}</td><td><ix:nonFraction id="{identifier}" name="g:{concept}" contextRef="{owner}-{kind}-{index}" unitRef="usd">{value}</ix:nonFraction></td></tr>')
            tables.append(''.join(parts) + '</table>')
    source = f'<html xmlns:g="{GAAP}" xmlns:dei="{DEI}" xmlns:ex="https://issuer.test/2026" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"><body><ix:header><ix:resources>{contexts}</ix:resources></ix:header>' + ''.join(dei + tables) + '</body></html>'
    return html.document_fromstring(source).getroottree(), deepcopy(facts)


class JointStatementTests(unittest.TestCase):
    def test_selection_keeps_original_parent_nodes_and_fact_scopes(self):
        tree, facts = fixture()
        before = deepcopy(facts)
        selected = select_joint_statement_segments(tree, facts)
        self.assertEqual([segment.element.get('id') for segment in selected],
                         ['1-income_statement', '1-balance_sheet', '1-cash_flow'])
        self.assertTrue(all(segment.group['joint_statement']['registrant_cik'] == '0000000001' for segment in selected))
        self.assertEqual(facts, before)
        self.assertEqual(len(tree.xpath('//table')), 6)

    def test_primary_dei_inline_name_context_and_cik_must_be_proven(self):
        for change in ('cik', 'name', 'entity'):
            tree, facts = fixture()
            if change == 'cik':
                facts['dei-1-EntityCentralIndexKey']['raw_value'] = '99'
            elif change == 'name':
                tree.xpath('//*[@id="dei-1-EntityRegistrantName"]')[0].text = 'Other Corporation'
            else:
                facts['dei-1-EntityCentralIndexKey']['entity'] = ENTITY | {'identifier': '0000000099'}
            with self.subTest(change=change), self.assertRaises(ValueError):
                select_joint_statement_segments(tree, facts)

    def test_subsidiary_facts_cannot_replace_missing_parent_statement(self):
        tree, facts = fixture()
        parent = tree.xpath('//*[@id="1-balance_sheet"]')[0]
        parent.getparent().remove(parent)
        child = tree.xpath('//*[@id="2-balance_sheet"]')[0]
        child.xpath('./tr[1]/td')[0].text = 'Primary Corporation'
        with self.assertRaises(t.TableExtractionError):
            select_joint_statement_segments(tree, facts)

    def test_currency_entity_or_competing_source_statement_is_rejected(self):
        for change in ('currency', 'entity', 'duplicate'):
            tree, facts = fixture()
            if change == 'currency':
                facts['f-1-income_statement-0']['unit'] = {'numerator': ['{http://www.xbrl.org/2003/iso4217}EUR'], 'denominator': []}
            elif change == 'entity':
                facts['f-1-income_statement-0']['entity'] = ENTITY | {'identifier': '0000000002'}
            else:
                node = tree.xpath('//*[@id="1-income_statement"]')[0]
                node.addnext(deepcopy(node))
            with self.subTest(change=change), self.assertRaises(t.TableExtractionError):
                select_joint_statement_segments(tree, facts)

    def test_parent_only_note_and_unconsolidated_headers_are_not_primary(self):
        for qualifier in ('Parent company only', 'Unconsolidated', 'Selected financial information',
                          'Note 12. Parent company financial information', 'Parent-Company Only',
                          'Parent Only', 'ParentCompanyOnly'):
            tree, facts = fixture()
            node = tree.xpath('//*[@id="1-balance_sheet"]/tr[3]')[0]
            node.addprevious(html.fragment_fromstring('<tr><td colspan="2">' + qualifier + '</td></tr>'))
            with self.subTest(qualifier=qualifier), self.assertRaises(t.TableExtractionError):
                select_joint_statement_segments(tree, facts)


class ArchivedJointStatementTests(unittest.TestCase):
    CASES = {
        '0000922224-26-000044': ['div[291]/table', 'div[320]/table', 'div[330]/table', 'div[310]/table'],
        '0001032208-26-000045': ['div[76]/table', 'div[86]/table', 'div[91]/table', 'div[97]/table', 'div[101]/table'],
        '0001047862-26-000142': ['div[98]/table', 'div[118]/table', 'div[126]/table', 'div[112]/table'],
        '0001104659-26-093360': ['div[13]/div[2]/table', 'div[11]/div[2]/table', 'div[15]/div[2]/table'],
        '0001070750-26-000126': ['div[70]/table', 'div[61]/table', 'div[87]/table', 'div[97]/table'],
        '0001711269-26-000100': ['div[83]/table', 'div[74]/table', 'div[78]/table', 'div[88]/table'],
        '0001868275-26-000104': ['div[66]/table', 'div[80]/table', 'div[73]/table'],
    }

    def sources(self, accession):
        path = ROOT / 'data/raw/sec' / accession / 'source_manifest.json'
        if not path.is_file():
            self.skipTest('Archived combined filing is unavailable')
        manifest = json.loads(path.read_text())
        sources = {record['role']: ROOT / record['local_path'] for record in manifest['files']}
        return sources['main_inline_xbrl_filing'], sources['extracted_xbrl_instance']

    def test_seven_archives_select_parent_physical_tables_in_original_coordinates(self):
        for accession, paths in self.CASES.items():
            with self.subTest(accession=accession):
                html_path, xml_path = self.sources(accession)
                tree = html.parse(str(html_path), html.HTMLParser(encoding='utf-8'))
                selected = t.select_statement_segments(tree, t.xml_evidence(xml_path, include_entity=True))
                self.assertEqual([tree.getpath(segment.element) for segment in selected], ['/html/body/' + path for path in paths])
                materialized = extract_facts(xml_path, filing_id=UUID(int=1), source_document_id=UUID(int=3)).require_complete().facts
                repeated = t.select_statement_segments(tree, materialized)
                self.assertEqual([segment.group for segment in selected], [segment.group for segment in repeated])

    def test_stored_joint_column_date_and_own_occurrence_are_rechecked(self):
        html_path, xml_path = self.sources('0001104659-26-093360')
        tree = html.parse(str(html_path), html.HTMLParser(encoding='utf-8'))
        facts = t.xml_evidence(xml_path, include_entity=True)
        tables = t.extract_tables(html_path, xml_path, filing_id=UUID(int=1), html_document_id=UUID(int=2),
                                  xml_document_id=UUID(int=3), tables_version='tables-v2')
        self.assertTrue(t.validate_statement_segments(tables, tree, facts, xml_path=xml_path))
        for change in ('date', 'fact', 'classification', 'row_label'):
            structure = deepcopy(tables[0].structure)
            cell = next(cell for row in structure['rows'] for cell in row if cell.get('fact_references'))
            if change == 'date':
                cell['column_period']['endDate'] = '2026-06-29'
            elif change == 'fact':
                cell['fact_references'][0]['xml_id'] = 'other'
            elif change == 'classification':
                cell['classification'] = 'formatting'
                cell['fact_references'] = []
            else:
                cell['row_heading'] = 'Different economic meaning'
            changed = [replace(tables[0], structure=structure), *tables[1:]]
            with self.subTest(change=change), self.assertRaises(t.TableExtractionError):
                t.validate_statement_segments(changed, tree, facts, xml_path=xml_path)


if __name__ == '__main__':
    unittest.main()
