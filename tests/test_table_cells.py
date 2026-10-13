"""Contract regressions for ambiguous cells and occurrence-level references."""

import copy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock
from uuid import UUID

from lxml import html

from sec_pipeline.database import FactInput, FinancialTableInput
from sec_pipeline.table_cells import TableCellError, relational_cells
from sec_pipeline.tables import FactScope, TableExtractionError, extract_tables, load_tables, select_statements


UNIT = {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []}


def cell_fixture(filing=UUID(int=1), html=UUID(int=2), xml=UUID(int=3), *, nil=False):
    """Synthetic two-tag cell plus a verified repeat, with no live source claims."""
    facts = [FactInput(
        filing_id=filing, source_document_id=xml, extraction_version='facts-fixture-v1',
        occurrence_key=key, concept_namespace='urn:fixture', concept_name=concept,
        numeric_value=None if nil and i == 1 else Decimal(value),
        raw_value=None if nil and i == 1 else value, is_nil=nil and i == 1,
        unit=UNIT, period_kind='duration', period_start=date(2026, 1, 1), period_end=date(2026, 3, 31),
        reported_decimals='0', source_reference={'element_id': key, 'context_ref': 'c1', 'unit_ref': 'u1'},
    ) for i, (key, concept, value) in enumerate((('F1', 'Revenue', '100'), ('F2', 'Profit', '20')))]
    scope = {'filing_id': str(filing), 'xml_document_id': str(xml), 'facts_version': 'facts-fixture-v1'}
    refs = [{'xml_id': f.occurrence_key, 'concept': '{urn:fixture}' + f.concept_name,
             'context_ref': 'c1', 'unit_ref': 'u1', 'unit': UNIT,
             'raw_value': '' if f.is_nil else f.raw_value, 'is_nil': f.is_nil,
             'period': {'startDate': '2026-01-01', 'endDate': '2026-03-31'}, 'dimensions': [],
             'scope': scope, 'method': 'inline_id', 'inline_xpath': f'/html/body/table/tr[2]/td[2]/ix[{i + 1}]',
             'scale': '0', 'inline_sign': None, 'format': 'ixt:num-dot-decimal'}
            for i, f in enumerate(facts)]

    def cell(key, row, col, value, classification, references=(), span=1):
        return {'cell_id': key, 'row': row, 'column': col, 'rowspan': 1, 'colspan': span,
                'display_text': value, 'source_xpath': f'/html/body/table/tr[{row + 1}]/td[{col + 1}]',
                'source_html_id': None, 'row_heading': 'Revenue / profit' if row else '',
                'column_headings': ['Quarter ended March 31, 2026'] if row and col else [],
                'column_period': {'startDate': '2026-01-01', 'endDate': '2026-03-31'} if row and col else None,
                'classification': classification, 'fact_references': list(references),
                'unresolved_reason': None, 'is_total': False}

    repeated = {**refs[0], 'method': 'same_row_semantic_repeat', 'repeated_from_inline_xpath': refs[0]['inline_xpath']}
    rows = [[cell('r0c0', 0, 0, 'Statement fixture', 'header', span=4)],
            [cell('r1c0', 1, 0, 'Revenue / profit', 'label'),
             cell('r1c1', 1, 1, '100 / nil' if nil else '100 / 20', 'resolved', refs, span=2),
             cell('r1c3', 1, 3, '100', 'resolved', [repeated])]]
    table = FinancialTableInput(filing_id=filing, source_document_id=html, extraction_version='tables-fixture-v1',
        table_key='income', statement_type='income_statement', readable_text='Statement fixture\n100 / 20\t100',
        structure={'schema_version': 1, 'fact_scope': scope, 'rows': rows,
                   'inventory': {'header': 1, 'label': 1, 'resolved': 2}})
    return table, facts


class TableCellContractTests(unittest.TestCase):
    def test_distinct_verified_tags_in_one_data_cell_are_preserved(self):
        root = Path(__file__).resolve().parents[1]
        source = root / 'data/raw/sec/0001193125-26-191507/msft-20260331.htm'
        tree = html.parse(str(source))
        table = select_statements(tree)['income_statement']
        tags = [e for e in table.iter() if e.tag == 'ix:nonfraction']
        first = tags[0]
        second = next(e for e in tags[1:] if e.get('contextref') == first.get('contextref'))
        cell = first
        while cell.tag not in ('td', 'th'):
            cell = cell.getparent()
        cell.append(second)  # Move, do not duplicate, the other original XML/inline ID.
        with tempfile.TemporaryDirectory() as temporary:
            changed = Path(temporary) / 'two-tags.htm'
            changed.write_bytes(html.tostring(tree))
            tables = extract_tables(changed, source.with_name('msft-20260331_htm.xml'),
                filing_id=UUID(int=1), html_document_id=UUID(int=2), xml_document_id=UUID(int=3))
        combined = next(c for row in tables[0].structure['rows'] for c in row
                        if any(ref['xml_id'] == first.get('id') for ref in c['fact_references']))
        self.assertEqual(combined['classification'], 'resolved')
        self.assertEqual([r['xml_id'] for r in combined['fact_references']], [first.get('id'), second.get('id')])

    def test_ambiguous_or_unresolved_references_cannot_be_promoted(self):
        table, _ = cell_fixture()
        bad = copy.deepcopy(table.structure)
        value = bad['rows'][1][1]
        value['classification'] = 'unresolved'
        value['unresolved_reason'] = 'two possible matches, neither verified'
        # The stale inventory still says zero unresolved; actual cells win.
        conn = MagicMock(autocommit=False)
        with self.assertRaises(TableExtractionError):
            load_tables(conn, [replace(table, structure=bad)], FactScope(
                UUID(int=1), UUID(int=3), 'facts-fixture-v1', {'F1': UUID(int=4), 'F2': UUID(int=5)}))
        conn.execute.assert_not_called()

    def test_duplicate_occurrence_and_span_overlap_fail_before_any_write(self):
        table, _ = cell_fixture()
        for defect in ('repeat', 'overlap'):
            with self.subTest(defect=defect):
                bad = copy.deepcopy(table.structure)
                cell = bad['rows'][1][1]
                if defect == 'repeat':
                    cell['fact_references'].append(copy.deepcopy(cell['fact_references'][0]))
                else:
                    cell['colspan'] = 3
                with self.assertRaises(TableCellError):
                    relational_cells(UUID(int=9), replace(table, structure=bad))

    def test_repeated_reference_still_checks_each_cells_value_evidence(self):
        table, facts = cell_fixture()
        bad = copy.deepcopy(table.structure)
        bad['rows'][1][2]['fact_references'][0]['raw_value'] = '999'
        identifiers = {f.occurrence_key: UUID(int=10 + i) for i, f in enumerate(facts)}
        records = {identifiers[f.occurrence_key]: (
            f.filing_id, f.source_document_id, f.extraction_version, f.concept_namespace, f.concept_name,
            f.raw_value, f.unit, f.period_kind, f.period_start, f.period_end, f.instant_date,
            f.dimensions, f.is_nil, f.numeric_value, f.occurrence_key, f.source_reference) for f in facts}
        conn = MagicMock(autocommit=False)
        conn.execute.side_effect = lambda query, args: type('Cursor', (), {'fetchone': lambda self: records[args[0]]})()
        with self.assertRaisesRegex(TableExtractionError, 'differs'):
            load_tables(conn, [replace(table, structure=bad)], FactScope(
                UUID(int=1), UUID(int=3), 'facts-fixture-v1', identifiers))
        self.assertEqual(conn.execute.call_count, 3)


if __name__ == '__main__':
    unittest.main()
