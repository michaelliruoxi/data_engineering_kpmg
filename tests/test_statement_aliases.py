"""Ordinary cash aliases retain native source concepts, periods and cells."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import unittest
from uuid import UUID

from lxml import html

from sec_pipeline.statement_aliases import select_alias_statement_segments, validate_alias_cells
from sec_pipeline.tables import (
    TableExtractionError, _STATEMENT_CONCEPTS, _select_statement_segments_v2,
    extract_tables, xml_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
SCOPE = {'filing_id': UUID(int=1), 'html_document_id': UUID(int=2), 'xml_document_id': UUID(int=3)}
ACCESSIONS = ('0000047217-26-000051', '0000935703-26-000109', '0001193125-26-389274', '0001571996-26-000046')


class StatementAliasTests(unittest.TestCase):
    def source(self, accession=ACCESSIONS[2]):
        folder = ROOT / 'data/raw/sec' / accession
        html_paths, xml_paths = list(folder.glob('*.htm')), list(folder.glob('*_htm.xml'))
        if len(html_paths) != 1 or len(xml_paths) != 1:
            self.skipTest('Archived standard cash-alias source unavailable')
        tree = html.parse(str(html_paths[0]), parser=html.HTMLParser(encoding='utf-8'))
        return tree, xml_evidence(xml_paths[0], include_entity=True), html_paths[0], xml_paths[0]

    def test_four_original_sources_resolve_without_mutating_legacy_concepts(self):
        catalog = deepcopy(_STATEMENT_CONCEPTS)
        for accession in ACCESSIONS:
            with self.subTest(accession=accession):
                tree, facts, html_path, xml_path = self.source(accession)
                with self.assertRaises(TableExtractionError):
                    _select_statement_segments_v2(tree, facts)
                selected = select_alias_statement_segments(tree, facts)
                self.assertEqual(len(selected), 3)
                cash = next(part for part in selected if part.statement_type == 'cash_flow')
                proof = cash.group['standard_cash_alias']
                self.assertEqual(len(proof['endpoint_periods']), 4)
                self.assertTrue(all(facts[identifier]['concept'] in proof['concepts']
                                    and facts[identifier]['period'] == period for identifier, period in proof['endpoint_periods'].items()))
                tables = extract_tables(html_path, xml_path, tables_version='tables-v2', **SCOPE)
                parsed = next(table for table in tables if table.statement_type == 'cash_flow')
                self.assertTrue(validate_alias_cells(parsed, cash, tree, facts))
        self.assertEqual(_STATEMENT_CONCEPTS, catalog)

    def test_alias_scope_context_namespace_and_labels_cannot_be_substituted(self):
        for change in ('date', 'parent', 'currency', 'entity', 'namespace', 'label', 'missing_core'):
            tree, facts, _, _ = self.source()
            selected = select_alias_statement_segments(tree, facts)
            cash = selected[-1]
            proof = cash.group['standard_cash_alias']
            identifier = next(iter(proof['endpoint_periods']))
            fact = facts[identifier]
            node = tree.xpath(f'//*[@id="{identifier}"]')[0]
            if change == 'date':
                fact['period']['instant'] = '2026-06-01'
            elif change == 'parent':
                fact['dimensions'] = [{'kind': 'explicit', 'dimension': '{http://fasb.org/srt/2026}ConsolidationItemsAxis',
                                      'member': '{http://fasb.org/srt/2026}ParentCompanyMember'}]
            elif change == 'currency':
                fact['unit'] = deepcopy(fact['unit'])
                fact['unit']['numerator'] = ['{http://www.xbrl.org/2003/iso4217}EUR']
            elif change == 'entity':
                fact['entity'] = deepcopy(fact['entity'])
                fact['entity']['identifier'] = '0000000002'
            elif change == 'namespace':
                local_name = fact['concept'].split('}', 1)[-1]
                fact['concept'] = '{https://issuer.example/2026}' + local_name
                tree.getroot().set('xmlns:other', 'https://issuer.example/2026')
                node.set('name', 'other:' + local_name)
            elif change == 'label':
                row = node.xpath('ancestor::tr[1]')[0]
                label = row.xpath('./td|./th')[0]
                label.clear(); label.text = 'Other cash balances'
            else:
                for item in facts.values():
                    if item['concept'].endswith('}NetCashProvidedByUsedInOperatingActivities'):
                        item['dimensions'] = [{'kind': 'explicit', 'dimension': '{example}Axis', 'member': '{example}Member'}]
            with self.subTest(change=change):
                self.assertIsNone(select_alias_statement_segments(tree, facts))

    def test_parent_only_and_note_header_disclaimers_are_rejected(self):
        for label in ('(Parent company only)', '(Parent-Company Only)', '(ParentOnly)',
                      '(Unconsolidated)', 'Note 12. Parent financial information'):
            tree, facts, _, _ = self.source()
            cash = select_alias_statement_segments(tree, facts)[-1].element
            row = html.fragment_fromstring('<tr><td colspan="13">' + label + '</td></tr>')
            cash.insert(0, row)
            with self.subTest(label=label):
                self.assertIsNone(select_alias_statement_segments(tree, facts))

    def test_competing_complete_alias_cash_statements_are_rejected(self):
        tree, facts, _, _ = self.source()
        selected = select_alias_statement_segments(tree, facts)
        original = selected[-1].element
        copy = deepcopy(original)
        for node in copy.iter():
            identifier = node.get('id')
            if identifier in facts:
                replacement = identifier + '-duplicate'
                node.set('id', replacement)
                facts[replacement] = deepcopy(facts[identifier]) | {'xml_id': replacement}
        wrapper = html.Element('div')
        heading = html.Element('p'); heading.text = 'Condensed Consolidated Statements of Cash Flows'
        wrapper.append(heading); wrapper.append(copy)
        tree.xpath('//body')[0].append(wrapper)
        self.assertIsNone(select_alias_statement_segments(tree, facts))

    def test_alias_endpoint_cannot_be_hidden_as_formatting_or_given_another_fact(self):
        tree, facts, html_path, xml_path = self.source()
        segment = next(part for part in select_alias_statement_segments(tree, facts) if part.statement_type == 'cash_flow')
        table = next(table for table in extract_tables(html_path, xml_path, tables_version='tables-v2', **SCOPE)
                     if table.statement_type == 'cash_flow')
        identifiers = set(segment.group['standard_cash_alias']['endpoint_periods'])
        for change in ('formatting', 'period', 'header', 'fact'):
            structure = deepcopy(table.structure)
            cell = next(cell for row in structure['rows'] for cell in row
                        if any(ref['xml_id'] in identifiers for ref in cell['fact_references']))
            if change == 'formatting':
                cell['classification'] = 'formatting'; cell['fact_references'] = []
            elif change == 'period':
                cell['column_period'] = None
            elif change == 'header':
                cell['column_headings'] = ['Invented date']
            else:
                cell['fact_references'][0]['xml_id'] = 'another-source-occurrence'
            with self.subTest(change=change), self.assertRaises(TableExtractionError):
                validate_alias_cells(replace(table, structure=structure), segment, tree, facts)


if __name__ == '__main__':
    unittest.main()
