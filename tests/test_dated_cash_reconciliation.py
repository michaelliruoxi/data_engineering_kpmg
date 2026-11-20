"""A reconciliation of cash headed by its own balance dates (tables-v3 corroborated layouts only).

A cash flow statement may close with the components of its cash balance under
the balance sheet dates ("June 30, 2026", "December 31, 2025") instead of the
statement's periods, without the title that names the balance sheets.
"""

import unittest

from lxml import html

from sec_pipeline.tables import _balance_date_captions, layout_grid, validate_statement_segments
from tests.test_printed_cell_readings import END, OPENING, TOTAL_CASH, Filing, unresolved


CASH = 'CashAndCashEquivalentsAtCarryingValue'
SEGREGATED = 'CashAndSecuritiesSegregatedUnderFederalAndOtherRegulations'


def block(*rows, caption='December 31, 2024'):
    """The fixture's cash flow statement (six months ended June 30, 2025) followed by a dated block."""
    filing = Filing()
    filing.raw('cash', f'<tr><td></td><td>{caption}</td></tr>')
    filing.raw('cash', '<tr><td>Reconciliation of cash and restricted cash</td><td></td></tr>')
    for label, concept, period, value in rows:
        filing.raw('cash', f'<tr><td>{label}</td><td>{filing.tag(concept, period, value)}</td></tr>')
    return filing


COMPONENTS = (('Cash and cash equivalents', CASH, OPENING, '4'), ('Segregated cash', SEGREGATED, OPENING, '1'),
              ('Total cash', TOTAL_CASH, OPENING, '5'))


class DatedReconciliationTests(unittest.TestCase):
    def test_cash_balances_are_read_under_the_date_the_block_prints(self):
        filing = block(*COMPONENTS)
        structures, tables = filing.extract()
        self.assertEqual(unresolved(structures), [])
        cash = structures['cash_flow']
        cells = {cell['display_text']: cell for row in cash['rows'] for cell in row if cell['display_text']}
        self.assertEqual(cells['December 31, 2024']['classification'], 'header')
        for text in ('4', '1', '5'):
            self.assertEqual((cells[text]['classification'], cells[text]['column_period']), ('resolved', OPENING))
        section, = cash['cash_reconciliation_sections']
        self.assertEqual(section['column_periods'], {'1': OPENING})
        self.assertEqual(section['first_data_row'] - section['header_start_row'], 2)
        validate_statement_segments(tables, filing.tree(), filing.facts, corroborated_layouts=True)

    def test_without_the_option_the_block_stays_unresolved(self):
        structures, _ = block(*COMPONENTS).extract(corroborated_layouts=False)
        self.assertIn(('December 31, 2024', 'untagged_financial_cell'), unresolved(structures))
        self.assertNotIn('cash_reconciliation_sections', structures['cash_flow'])

    def test_a_balance_that_is_no_cash_balance_stays_unresolved(self):
        structures, _ = block(*COMPONENTS, ('Dividends payable', 'DividendsPayableCurrent', OPENING, '3')).extract()
        self.assertEqual(unresolved(structures), [('3', 'Unexpected instant in cash-flow row')])

    def test_a_balance_of_another_date_stays_unresolved(self):
        structures, _ = block(*COMPONENTS, ('Restricted cash', SEGREGATED, END, '9')).extract()
        self.assertEqual([text for text, _ in unresolved(structures)], ['9'])

    def test_a_caption_of_the_columns_own_end_opens_no_section(self):
        structures, _ = block(caption='June 30, 2025').extract()
        cash = structures['cash_flow']
        self.assertEqual(unresolved(structures), [])
        self.assertNotIn('cash_reconciliation_sections', cash)

    def test_what_counts_as_a_row_of_balance_dates(self):
        tree = html.document_fromstring(
            '<table><tr><td></td><td>h</td><td>h</td></tr>'
            '<tr><td></td><td>June 30, 2026</td><td>December 31,2025</td></tr>'
            '<tr><td>Cash</td><td>June 30, 2026</td><td><ix:nonfraction id="a">1</ix:nonfraction></td></tr>'
            '<tr><td></td><td>June 30, 2026</td><td>(restated)</td></tr>'
            '<tr><td></td><td>June 30,</td><td>2025</td></tr>'
            '<tr><td>Cash</td><td></td><td></td></tr></table>').getroottree()
        rows, _ = layout_grid(tree.xpath('//table')[0], tree)
        self.assertEqual([_balance_date_captions(row, frozenset()) for row in rows[1:]], [True, False, False, False, False])
        # A row that only restates its own columns' ends is the statement's caption, not a block's.
        restated = frozenset(cell['cell_id'] for cell in rows[1] if cell['column'] > 0)
        self.assertFalse(_balance_date_captions(rows[1], restated))


if __name__ == '__main__':
    unittest.main()
