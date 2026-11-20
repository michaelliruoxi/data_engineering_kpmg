"""More cells tables-v3 reads as the page prints them (corroborated layouts only).

* a "0" of one point that only keeps a ruled row open;
* a body row that restates the end of its columns' periods;
* a balance a row prints under its own month and year, left of the period columns;
* a tagged number printed across grid columns of which only one has a period;
* a short header row that prints nothing right of the label column.
"""

import unittest

from lxml import html

from sec_pipeline.tables import (
    _column_periods, _dated_parentheticals, _displayed_header_rows, _restated_period_captions, _spacer_zero, layout_grid,
    validate_statement_segments,
)
from tests.test_displayed_header_alignment import CAPTIONS, GAAP, HIDDEN, INCOME, PERIODS, YEARS, document, numbered
from tests.test_printed_cell_readings import END, HALF, OPENING, Filing, unresolved


def cells_of(structure):
    return {cell['display_text']: cell for row in structure['rows'] for cell in row if cell['display_text']}


class SpacerZeroTests(unittest.TestCase):
    SPACER = '<span style="color:#000000;font-size:1pt;font-weight:700">0</span>'

    def filing(self, cell):
        filing = Filing()
        filing.raw('balance', f'<tr><td></td><td>{cell}</td></tr>')
        return filing

    def test_a_zero_of_one_point_is_formatting(self):
        filing = self.filing(self.SPACER)
        structures, tables = filing.extract()
        self.assertEqual(unresolved(structures), [])
        self.assertEqual(cells_of(structures['balance_sheet'])['0']['classification'], 'formatting')
        validate_statement_segments(tables, filing.tree(), filing.facts, corroborated_layouts=True)

    def test_without_the_option_the_zero_stays_unresolved(self):
        structures, _ = self.filing(self.SPACER).extract(corroborated_layouts=False)
        self.assertEqual(unresolved(structures), [('0', 'untagged_financial_cell')])

    def test_a_readable_or_different_number_stays_unresolved(self):
        for cell, text in (('<span style="font-size:10pt">0</span>', '0'), ('<span style="font-size:1.5pt">0</span>', '0'),
                           ('<span>0</span>', '0'), ('<span style="font-size:1pt">5</span>', '5'),
                           ('<span style="font-size:1pt">0</span>7', '07'),
                           ('<span style="font-size:1pt">0</span><span style="font-size:1pt">0</span>', '00'),
                           ('<span style="font-size:1pt"><b style="font-size:9pt">0</b></span>', '0')):
            with self.subTest(cell=cell):
                structures, _ = self.filing(cell).extract()
                self.assertEqual(unresolved(structures), [(text, 'untagged_financial_cell')])

    def test_the_nearest_font_size_decides(self):
        cell = html.fragment_fromstring('<td style="font-size:1pt"><span><b>0</b></span></td>')
        self.assertTrue(_spacer_zero(cell))
        self.assertFalse(_spacer_zero(html.fragment_fromstring('<td style="font-size:1pt"><span style="font-size:8pt">0</span></td>')))
        self.assertFalse(_spacer_zero(html.fragment_fromstring('<td style="line-height:1pt">0</td>')))


class RestatedPeriodCaptionTests(unittest.TestCase):
    """The cash flow column of the fixture covers the six months ended June 30, 2025."""

    def filing(self, *rows):
        filing = Filing()
        for row in rows:
            filing.raw('cash', row(filing) if callable(row) else row)
        return filing

    def test_a_full_date_equal_to_the_column_end_is_a_header(self):
        filing = self.filing('<tr><td>Reconciliation of cash and restricted cash</td><td>June 30,2025</td></tr>')
        structures, tables = filing.extract()
        self.assertEqual(unresolved(structures), [])
        self.assertEqual(cells_of(structures['cash_flow'])['June 30,2025']['classification'], 'header')
        validate_statement_segments(tables, filing.tree(), filing.facts, corroborated_layouts=True)

    def test_a_month_and_day_row_with_the_years_below_it_is_a_header(self):
        structures, _ = self.filing('<tr><td></td><td>June 30,</td></tr>', '<tr><td></td><td>2025</td></tr>').extract()
        self.assertEqual(unresolved(structures), [])
        cells = cells_of(structures['cash_flow'])
        self.assertEqual((cells['June 30,']['classification'], cells['2025']['classification']), ('header', 'header'))

    def test_without_the_option_the_captions_stay_unresolved(self):
        structures, _ = self.filing('<tr><td></td><td>June 30,</td></tr>', '<tr><td></td><td>2025</td></tr>').extract(
            corroborated_layouts=False)
        self.assertEqual(unresolved(structures), [('June 30,', 'untagged_financial_cell'), ('2025', 'untagged_financial_cell')])

    def test_what_is_no_caption_of_its_own_column_stays_unresolved(self):
        cases = (
            (('<tr><td></td><td>2025</td></tr>',), [('2025', 'untagged_financial_cell')]),  # a year alone could be an amount
            (('<tr><td></td><td>June 30,</td></tr>', '<tr><td></td><td></td></tr>', '<tr><td></td><td>2025</td></tr>'),
             [('2025', 'untagged_financial_cell')]),  # not directly below the month and day
            (('<tr><td></td><td>June 30,</td></tr>', '<tr><td></td><td>2024</td></tr>'), [('2024', 'untagged_financial_cell')]),
            (('<tr><td></td><td>March 31, 2025</td></tr>',), [('March 31, 2025', 'untagged_financial_cell')]),
            (('<tr><td></td><td>December 31,</td></tr>',), [('December 31,', 'untagged_financial_cell')]),
            (('<tr><td></td><td>June 30, 2025 (restated)</td></tr>',), [('June 30, 2025 (restated)', 'untagged_financial_cell')]),
        )
        for rows, expected in cases:
            with self.subTest(rows=rows):
                structures, _ = self.filing(*rows).extract()
                self.assertEqual(unresolved(structures), expected)

    def test_a_row_that_carries_a_tag_is_no_caption_row(self):
        tree = html.document_fromstring(
            '<table><tr><td></td><td>Six Months Ended June 30, 2025</td><td>note</td></tr>'
            '<tr><td>Cash</td><td>June 30, 2025</td><td><ix:nonfraction id="a">1</ix:nonfraction></td></tr>'
            '<tr><td>Cash</td><td>June 30, 2025</td><td></td></tr></table>').getroottree()
        rows, _ = layout_grid(tree.xpath('//table')[0], tree)
        captions = _restated_period_captions(rows, 1, {1: HALF, 2: None})
        self.assertEqual(captions, frozenset({rows[2][1]['cell_id']}))
        # A column without a period has no end a caption could restate.
        self.assertEqual(_restated_period_captions(rows, 1, {1: None, 2: None}), frozenset())


class DatedParentheticalTests(unittest.TestCase):
    """Shares issued at each balance date, each in a row of its own before the amount columns."""

    def filing(self, first='June 2025', second='December 2024', period=OPENING):
        filing = Filing()
        filing.headers['balance'] = '<tr><td></td><td></td><td></td><td>June 30, 2025</td></tr>'
        filing.rows['balance'] = []
        filing.raw('balance', f'<tr><td>Common stock – shares issued –</td><td>{first}</td>'
                              f'<td>{filing.tag("CommonStockSharesIssued", END, "9")}</td><td></td></tr>')
        filing.raw('balance', f'<tr><td></td><td>{second}</td><td>{filing.tag("CommonStockSharesIssued", period, "8")}</td>'
                              f'<td>{filing.tag("CommonStockValue", END, "3")}</td></tr>')
        for concept in ('Assets', 'Liabilities', 'StockholdersEquity'):
            filing.raw('balance', f'<tr><td>Total {concept}</td><td></td><td></td><td>{filing.tag(concept, END)}</td></tr>')
        return filing

    def test_a_balance_under_its_own_month_and_year_is_part_of_the_label(self):
        filing = self.filing()
        structures, tables = filing.extract()
        self.assertEqual(unresolved(structures), [])
        cells = cells_of(structures['balance_sheet'])
        self.assertEqual((cells['9']['classification'], cells['8']['classification']), ('resolved_label', 'resolved_label'))
        self.assertEqual([cells[text]['fact_references'][0]['period'] for text in ('9', '8')], [END, OPENING])
        self.assertEqual((cells['June 2025']['classification'], cells['December 2024']['classification']), ('label', 'label'))
        # The amount beside the second caption is an ordinary cell of the dated column.
        self.assertEqual((cells['3']['classification'], cells['3']['column_period']), ('resolved', END))
        self.assertEqual([column['column'] for column in structures['balance_sheet']['presentation_columns']], [3])
        validate_statement_segments(tables, filing.tree(), filing.facts, corroborated_layouts=True)

    def test_without_the_option_the_row_stays_unresolved(self):
        structures, _ = self.filing().extract(corroborated_layouts=False)
        self.assertEqual(unresolved(structures), [
            ('June 2025', 'untagged_financial_cell'), ('9', 'Unresolved column period'),
            ('December 2024', 'untagged_financial_cell'), ('8', 'Unresolved column period')])

    def test_a_caption_that_does_not_name_the_balance_date_leaves_the_row_unresolved(self):
        for filing, texts in ((self.filing(second='March 2025'), ('March 2025', '8')),
                              (self.filing(second='December 2024', period=HALF), ('December 2024', '8')),
                              (self.filing(second='December'), ('8',))):
            with self.subTest(texts=texts):
                structures, _ = filing.extract()
                self.assertEqual([text for text, _ in unresolved(structures)], list(texts))

    def test_a_number_without_its_caption_or_under_a_dated_column_is_not_taken(self):
        tree = html.document_fromstring(
            '<table><tr><td></td><td></td><td></td><td>June 30, 2025</td></tr>'
            '<tr><td>Shares</td><td>June 2025</td><td><ix:nonfraction id="a">9</ix:nonfraction></td><td></td></tr>'
            '<tr><td>Shares</td><td></td><td><ix:nonfraction id="b">9</ix:nonfraction></td><td></td></tr>'
            '<tr><td>Shares</td><td>June 2025</td><td>12</td><td><ix:nonfraction id="c">9</ix:nonfraction></td></tr>'
            '</table>').getroottree()
        rows, _ = layout_grid(tree.xpath('//table')[0], tree)
        facts = {key: {'period': dict(END)} for key in 'abc'}
        tagged, captions = _dated_parentheticals(rows, 1, {1: None, 2: None, 3: END}, facts, {'a': 1, 'b': 1, 'c': 1})
        self.assertEqual((tagged, captions), (frozenset({rows[1][2]['cell_id']}), frozenset({rows[1][1]['cell_id']})))
        # An id used twice in the page, or a table without a dated column, gives nothing.
        self.assertEqual(_dated_parentheticals(rows, 1, {1: None, 2: None, 3: END}, facts, {'a': 2, 'b': 1, 'c': 1})[0], frozenset())
        self.assertEqual(_dated_parentheticals(rows, 1, {1: None, 2: None, 3: None}, facts, {'a': 1}), (frozenset(), frozenset()))


class SpannedColumnTests(unittest.TestCase):
    """A currency sign in a column of its own; one number is printed across that column and the dated one."""

    def filing(self, wide, header='<tr><td></td><td></td><td>June 30, 2025</td></tr>'):
        filing = Filing()
        filing.headers['balance'] = header
        filing.rows['balance'] = []
        for concept in ('Assets', 'Liabilities', 'StockholdersEquity'):
            filing.raw('balance', f'<tr><td>Total {concept}</td><td>$</td><td>{filing.tag(concept, END)}</td></tr>')
        filing.raw('balance', wide(filing))
        return filing

    def test_the_number_takes_the_one_period_of_the_columns_it_spans(self):
        filing = self.filing(lambda f: f'<tr><td>Other assets</td><td colspan="2">{f.tag("OtherAssets", END, "2")}</td></tr>')
        structures, tables = filing.extract()
        self.assertEqual(unresolved(structures), [])
        cell = cells_of(structures['balance_sheet'])['2']
        self.assertEqual((cell['column'], cell['colspan'], cell['classification'], cell['column_period']), (1, 2, 'resolved', END))
        validate_statement_segments(tables, filing.tree(), filing.facts, corroborated_layouts=True)

    def test_without_the_option_the_number_stays_unresolved(self):
        filing = self.filing(lambda f: f'<tr><td>Other assets</td><td colspan="2">{f.tag("OtherAssets", END, "2")}</td></tr>')
        structures, _ = filing.extract(corroborated_layouts=False)
        self.assertEqual(unresolved(structures), [('2', 'Unresolved column period')])

    def test_the_tag_must_carry_that_period_and_the_columns_must_agree(self):
        other = self.filing(lambda f: f'<tr><td>Other assets</td><td colspan="2">{f.tag("OtherAssets", OPENING, "2")}</td></tr>')
        self.assertEqual(unresolved(other.extract()[0]), [('2', 'Fact period differs from column heading')])
        header = '<tr><td></td><td></td><td>June 30, 2025</td><td>December 31, 2024</td></tr>'
        # The second dated column has no cell of its own below the header; its header still counts.
        two = self.filing(lambda f: f'<tr><td>Other assets</td><td colspan="3">{f.tag("OtherAssets", END, "2")}</td></tr>',
                          header=header)
        self.assertEqual(unresolved(two.extract()[0]), [('2', 'Unresolved column period')])


class ShortHeaderRowTests(unittest.TestCase):
    """A sizing row or a section title above the captions is shorter than the numbered rows."""

    def periods(self, *short):
        body, facts = numbered('income', INCOME, PERIODS)
        tree = document(f'<table>{"".join(short)}{CAPTIONS}{YEARS}{body}</table>')
        rows, _ = layout_grid(tree.xpath('//table')[0], tree)
        first = len(short) + 2
        return rows, first, _column_periods(rows, first, facts, {'g': GAAP}, {key: 1 for key in facts}, advanced=True,
                                           corroborate=True)[0]

    def test_a_short_row_without_a_caption_does_not_stop_the_displayed_reading(self):
        for short in ((f'<tr><td></td>{HIDDEN}</tr>',), (f'<tr><td>Revenues</td>{HIDDEN}</tr>',), ('<tr><td></td></tr>',),
                      (f'<tr><td></td>{HIDDEN}</tr>', f'<tr><td>(in millions)</td>{HIDDEN}</tr>')):
            with self.subTest(short=short):
                rows, first, periods = self.periods(*short)
                self.assertIsNotNone(_displayed_header_rows(rows, first))
                self.assertEqual([periods[column] for column in (3, 4, 5, 6)], list(PERIODS))

    def test_a_short_row_that_prints_right_of_the_label_still_stops_it(self):
        rows, first, periods = self.periods(f'<tr><td></td>{HIDDEN}<td>Unaudited</td></tr>')
        self.assertIsNone(_displayed_header_rows(rows, first))
        self.assertNotEqual([periods[column] for column in (3, 4, 5, 6)], list(PERIODS))


if __name__ == '__main__':
    unittest.main()
