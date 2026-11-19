"""Header rows that tables-v3 reads where the page displays them.

A cell styled `display:none` takes no room on the page. When a caption row
hides fewer spacer cells than the rows that carry the numbers, the physical
grid puts its captions above other columns than the page does.
"""

from pathlib import Path
import tempfile
import unittest
from unittest import mock
from uuid import UUID

from lxml import html

from sec_pipeline.tables import (
    _column_periods, _displayed_header_rows, extract_tables, layout_grid, select_statement_segments,
    validate_statement_segments,
)


GAAP = 'http://fasb.org/us-gaap/2026'
HIDDEN = '<td style="display:none" colspan="2"></td>'
Q2_2024, Q2_2025 = ({'startDate': f'{year}-04-01', 'endDate': f'{year}-06-30'} for year in (2024, 2025))
H1_2024, H1_2025 = ({'startDate': f'{year}-01-01', 'endDate': f'{year}-06-30'} for year in (2024, 2025))
PERIODS = (Q2_2024, Q2_2025, H1_2024, H1_2025)
CAPTIONS = ('<tr><td></td><td colspan="2">Three Months Ended June 30,</td>'
            '<td colspan="2">Six Months Ended June 30,</td></tr>')
YEARS = f'<tr><td></td>{HIDDEN}<td>2024</td><td>2025</td><td>2024</td><td>2025</td></tr>'
INCOME = ('Revenues', 'OperatingIncomeLoss', 'NetIncomeLoss')


def fact(identifier, concept, period, value='1'):
    return {'xml_id': identifier, 'concept': '{' + GAAP + '}' + concept, 'context_ref': identifier, 'unit_ref': 'usd',
            'period': dict(period), 'raw_value': value, 'is_nil': False, 'dimensions': [],
            'unit': {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []},
            'entity': {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'}}


def numbered(key, concepts, periods, hidden=HIDDEN):
    """Rows of tagged numbers, one column per period, behind the hidden spacer cells."""
    rows, facts = [], {}
    for index, concept in enumerate(concepts):
        cells = []
        for column, period in enumerate(periods):
            identifier = f'{key}-{index}-{column}'
            facts[identifier] = fact(identifier, concept, period)
            cells.append(f'<td><ix:nonfraction id="{identifier}" name="g:{concept}" contextref="{identifier}" '
                         f'unitref="usd">1</ix:nonfraction></td>')
        rows.append(f'<tr><td>{concept}</td>{hidden}{"".join(cells)}</tr>')
    return ''.join(rows), facts


def document(body):
    return html.document_fromstring(
        f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>{body}</body></html>').getroottree()


def resolve(*, captions=CAPTIONS, years=YEARS, periods=PERIODS, corroborate=True, hidden=HIDDEN):
    body, facts = numbered('income', INCOME, periods, hidden)
    tree = document(f'<table>{captions}{years}{body}</table>')
    rows, _ = layout_grid(tree.xpath('//table')[0], tree)
    result = _column_periods(rows, 2, facts, {'g': GAAP}, {key: 1 for key in facts}, advanced=True, corroborate=corroborate)
    return rows, result


class DisplayedCaptionTests(unittest.TestCase):
    def test_captions_are_read_above_the_columns_the_page_displays_them_over(self):
        _, (periods, provenance) = resolve()
        # The grid columns of the four number columns are 3 to 6; columns 1 and 2 are hidden.
        self.assertEqual([periods[column] for column in (3, 4, 5, 6)], list(PERIODS))
        quarter = provenance['3']
        self.assertEqual(quarter['method'], 'displayed_header_alignment')
        self.assertEqual(quarter['physical_headings'], ['Six Months Ended June 30,', '2024'])
        self.assertEqual(quarter['displayed_headings'], ['Three Months Ended June 30,', '2024'])
        self.assertEqual(provenance['6']['physical_headings'], ['2025'])
        self.assertEqual(provenance['6']['displayed_headings'], ['Six Months Ended June 30,', '2025'])

    def test_the_physical_grid_is_kept_without_the_option(self):
        _, (periods, provenance) = resolve(corroborate=False)
        self.assertEqual(periods[3], H1_2024)
        self.assertFalse(any('displayed_headings' in evidence for evidence in provenance.values()))

    def test_a_header_the_physical_grid_already_reads_is_left_alone(self):
        aligned = f'<tr><td></td>{HIDDEN}<td colspan="2">Three Months Ended June 30,</td><td colspan="2">Six Months Ended June 30,</td></tr>'
        rows, (periods, provenance) = resolve(captions=aligned)
        self.assertEqual([periods[column] for column in (3, 4, 5, 6)], list(PERIODS))
        self.assertEqual(provenance, {})
        self.assertIsNone(_displayed_header_rows(rows, 2))

    def test_a_tagged_period_the_displayed_caption_does_not_print_is_refused(self):
        # The second column is tagged with the half year under a caption displayed as three months.
        _, (periods, provenance) = resolve(periods=(Q2_2024, H1_2025, H1_2024, H1_2025))
        self.assertEqual(periods[3], H1_2024)
        self.assertFalse(any('displayed_headings' in evidence for evidence in provenance.values()))

    def test_rows_the_page_does_not_line_up_are_left_alone(self):
        cases = {
            'a caption row narrower than the numbers as displayed':
                {'captions': '<tr><td></td><td colspan="2">Three Months Ended June 30,</td><td>Six Months Ended June 30,</td></tr>'},
            'a caption spanning two rows':
                {'captions': CAPTIONS.replace('<td colspan="2">Three', '<td colspan="2" rowspan="2">Three'),
                 'years': f'<tr><td></td>{HIDDEN}<td>2024</td><td>2025</td></tr>'},
        }
        for name, options in cases.items():
            with self.subTest(name):
                rows, (periods, provenance) = resolve(**options)
                self.assertIsNone(_displayed_header_rows(rows, 2))
                self.assertFalse(any('displayed_headings' in evidence for evidence in provenance.values()))

    def test_numbered_rows_that_hide_different_cells_are_left_alone(self):
        body, facts = numbered('income', INCOME, PERIODS)
        other, more = numbered('more', ('IncomeTaxExpenseBenefit',), PERIODS,
                               hidden='<td style="display:none"></td><td></td>')
        tree = document(f'<table>{CAPTIONS}{YEARS}{body}{other}</table>')
        rows, _ = layout_grid(tree.xpath('//table')[0], tree)
        self.assertIsNone(_displayed_header_rows(rows, 2))
        periods, provenance = _column_periods(rows, 2, {**facts, **more}, {'g': GAAP}, {key: 1 for key in {**facts, **more}},
                                              advanced=True, corroborate=True)
        self.assertEqual(periods[3], H1_2024)
        self.assertFalse(any('displayed_headings' in evidence for evidence in provenance.values()))

    def test_displayed_cells_keep_their_identity_and_text(self):
        rows, _ = resolve()
        shown = _displayed_header_rows(rows, 2)
        captions = [(cell['cell_id'], cell['display_text'], cell['column'], cell['colspan']) for cell in shown[0] if cell['display_text']]
        self.assertEqual(captions, [('r000c001', 'Three Months Ended June 30,', 3, 2),
                                    ('r000c003', 'Six Months Ended June 30,', 5, 2)])
        # The year row hides what the numbered rows hide and is returned as it is.
        self.assertIs(shown[1], rows[1])


class ExtractionTests(unittest.TestCase):
    def filing(self):
        income, facts = numbered('income', INCOME, PERIODS)
        end = {'instant': '2025-06-30'}
        balance, more = numbered('balance', ('Assets', 'Liabilities', 'StockholdersEquity'), (end,), hidden='')
        facts.update(more)
        cash, more = numbered('cash', ('NetCashProvidedByUsedInOperatingActivities', 'NetCashProvidedByUsedInInvestingActivities',
                                       'NetCashProvidedByUsedInFinancingActivities'), (H1_2025,), hidden='')
        facts.update(more)
        facts['closing'] = fact('closing', 'CashAndCashEquivalentsAtCarryingValue', end)
        cash += ('<tr><td>Cash and cash equivalents at end of period</td><td><ix:nonfraction id="closing" '
                 'name="g:CashAndCashEquivalentsAtCarryingValue" contextref="closing" unitref="usd">1</ix:nonfraction></td></tr>')
        body = (f'<p>Consolidated Statements of Income</p><table id="income">{CAPTIONS}{YEARS}{income}</table>'
                f'<p>Consolidated Balance Sheets</p><table id="balance"><tr><td></td><td>June 30, 2025</td></tr>{balance}</table>'
                f'<p>Consolidated Statements of Cash Flows</p><table id="cash"><tr><td></td>'
                f'<td>Six Months Ended June 30, 2025</td></tr>{cash}</table>')
        source = f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>{body}</body></html>'
        return source, facts

    def extract(self, corroborated_layouts):
        source, facts = self.filing()
        with tempfile.TemporaryDirectory() as folder:
            html_path, xml_path = Path(folder) / 'filing.htm', Path(folder) / 'filing.xml'
            html_path.write_text(source, encoding='utf-8')
            xml_path.write_text('<xbrl/>', encoding='utf-8')
            with mock.patch('sec_pipeline.tables.xml_evidence', return_value=facts):
                tables = extract_tables(html_path, xml_path, filing_id=UUID(int=1), html_document_id=UUID(int=2),
                                        xml_document_id=UUID(int=3), tables_version='tables-v2',
                                        corroborated_layouts=corroborated_layouts)
        return document(source.split('<body>')[1].split('</body>')[0]), facts, tables

    def test_numbers_carry_the_displayed_headings_and_header_cells_their_physical_place(self):
        tree, facts, tables = self.extract(True)
        income = tables[0].structure
        cells = {cell['cell_id']: cell for row in income['rows'] for cell in row}
        first = cells['r002c003']
        self.assertEqual((first['classification'], first['column_period']), ('resolved', Q2_2024))
        self.assertEqual(first['column_headings'], ['Three Months Ended June 30,', '2024'])
        self.assertEqual(cells['r002c006']['column_headings'], ['Six Months Ended June 30,', '2025'])
        self.assertFalse([cell for cell in cells.values() if cell['classification'] == 'unresolved'])
        # The caption cell itself stays where the markup puts it.
        caption = cells['r000c003']
        self.assertEqual((caption['column'], caption['colspan'], caption['column_headings'][0]),
                         (3, 2, 'Six Months Ended June 30,'))
        self.assertEqual([column['headings'] for column in income['presentation_columns']],
                         [['Three Months Ended June 30,', '2024'], ['Three Months Ended June 30,', '2025'],
                          ['Six Months Ended June 30,', '2024'], ['Six Months Ended June 30,', '2025']])
        self.assertEqual([column['period'] for column in income['presentation_columns']], list(PERIODS))
        self.assertEqual(income['column_period_evidence']['3']['method'], 'displayed_header_alignment')
        validate_statement_segments(tables, tree, facts, corroborated_layouts=True)

    def test_without_the_option_the_contradicted_columns_stay_unresolved(self):
        _, _, tables = self.extract(False)
        cells = {cell['cell_id']: cell for row in tables[0].structure['rows'] for cell in row}
        self.assertEqual((cells['r002c003']['classification'], cells['r002c003']['unresolved_reason']),
                         ('unresolved', 'Fact period differs from column heading'))
        self.assertEqual(cells['r002c003']['column_headings'], ['Six Months Ended June 30,', '2024'])

    def test_selection_is_the_same_with_and_without_the_option(self):
        source, facts = self.filing()
        tree = document(source.split('<body>')[1].split('</body>')[0])
        keys = ['income_statement', 'balance_sheet', 'cash_flow']
        self.assertEqual([part.table_key for part in select_statement_segments(tree, facts)], keys)
        self.assertEqual([part.table_key for part in select_statement_segments(tree, facts, corroborated_layouts=True)], keys)


if __name__ == '__main__':
    unittest.main()
