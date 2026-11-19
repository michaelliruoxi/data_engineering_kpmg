"""Cells tables-v3 reads as the page prints them (corroborated layouts only).

* a row label printed right of an empty first column, carrying tagged amounts;
* a cell that only points to a note;
* a balance that an income statement row dates at the period's end;
* the components of a cash balance, proved by the total printed below them;
* a column headed by a fiscal year that closed in the first days of January.
"""

from pathlib import Path
import tempfile
import unittest
from unittest import mock
from uuid import UUID

from lxml import html

from sec_pipeline.statement_headings import _line_metadata
from sec_pipeline.tables import (
    TableExtractionError, _column_periods, _fiscal_year_boundaries, _label_columns, _labelled_period_end,
    _labelled_years, _note_reference, _segment_candidate, _segment_facts, _summed_cash_components, extract_tables,
    layout_grid, validate_statement_segments,
)


GAAP = 'http://fasb.org/us-gaap/2026'
HALF = {'startDate': '2025-01-01', 'endDate': '2025-06-30'}
END, OPENING = {'instant': '2025-06-30'}, {'instant': '2024-12-31'}
TOTAL_CASH = 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'


def fact(identifier, concept, period, value='1'):
    return {'xml_id': identifier, 'concept': '{' + GAAP + '}' + concept, 'context_ref': identifier, 'unit_ref': 'usd',
            'period': dict(period), 'raw_value': value, 'is_nil': False, 'dimensions': [],
            'unit': {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []},
            'entity': {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'}}


class Filing:
    """Three statements whose rows a test adds to; every tagged number becomes a fact."""

    def __init__(self):
        self.facts = {}
        self.rows = {'income': [], 'balance': [], 'cash': []}
        self.headers = {'income': '<tr><td></td><td>Six Months Ended June 30, 2025</td></tr>',
                        'balance': '<tr><td></td><td>June 30, 2025</td></tr>',
                        'cash': '<tr><td></td><td>Six Months Ended June 30, 2025</td></tr>'}
        for concept in ('Revenues', 'OperatingIncomeLoss', 'NetIncomeLoss'):
            self.line('income', concept, concept, HALF)
        for concept in ('Assets', 'Liabilities', 'StockholdersEquity'):
            self.line('balance', concept, concept, END)
        for concept, value in (('NetCashProvidedByUsedInOperatingActivities', '4'), ('NetCashProvidedByUsedInInvestingActivities', '2'),
                               ('NetCashProvidedByUsedInFinancingActivities', '1')):
            self.line('cash', concept, concept, HALF, value)
        self.line('cash', 'Cash, cash equivalents and restricted cash at end of period', TOTAL_CASH, END, '7')

    def tag(self, concept, period, value='1'):
        identifier = f'f{len(self.facts)}'
        self.facts[identifier] = fact(identifier, concept, period, value)
        return (f'<ix:nonfraction id="{identifier}" name="g:{concept}" contextref="{identifier}" unitref="usd">'
                f'{value}</ix:nonfraction>')

    def line(self, statement, label, concept, period, value='1'):
        self.rows[statement].append(f'<tr><td>{label}</td><td>{self.tag(concept, period, value)}</td></tr>')

    def raw(self, statement, markup):
        self.rows[statement].append(markup)

    def source(self):
        titles = {'income': 'Consolidated Statements of Income', 'balance': 'Consolidated Balance Sheets',
                  'cash': 'Consolidated Statements of Cash Flows'}
        body = ''.join(f'<p>{titles[key]}</p><table id="{key}">{self.headers[key]}{"".join(self.rows[key])}</table>'
                       for key in ('income', 'balance', 'cash'))
        return f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>{body}</body></html>'

    def tree(self):
        return html.document_fromstring(self.source()).getroottree()

    def extract(self, corroborated_layouts=True):
        with tempfile.TemporaryDirectory() as folder:
            html_path, xml_path = Path(folder) / 'filing.htm', Path(folder) / 'filing.xml'
            html_path.write_text(self.source(), encoding='utf-8')
            xml_path.write_text('<xbrl/>', encoding='utf-8')
            with mock.patch('sec_pipeline.tables.xml_evidence', return_value=self.facts):
                tables = extract_tables(html_path, xml_path, filing_id=UUID(int=1), html_document_id=UUID(int=2),
                                        xml_document_id=UUID(int=3), tables_version='tables-v2',
                                        corroborated_layouts=corroborated_layouts)
        return {table.statement_type: table.structure for table in tables}, tables

    def cells(self, statement, corroborated_layouts=True):
        structures, _ = self.extract(corroborated_layouts)
        return {cell['display_text']: cell for row in structures[statement]['rows'] for cell in row if cell['display_text']}


def unresolved(structures):
    return [(cell['display_text'], cell['unresolved_reason']) for structure in structures.values()
            for row in structure['rows'] for cell in row if cell['classification'] == 'unresolved']


class LabelColumnTests(unittest.TestCase):
    """A balance sheet that leaves its first column empty and prints the labels in the second."""

    def filing(self, extra=''):
        filing = Filing()
        filing.headers['balance'] = '<tr><td></td><td></td><td>June 30, 2025</td></tr>'
        filing.rows['balance'] = []
        equipment = ('<tr><td></td><td>Property and equipment, net of accumulated depreciation of '
                     + filing.tag('AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment', END, '5') + ' and '
                     + filing.tag('AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment', OPENING, '4')
                     + '</td><td>' + filing.tag('PropertyPlantAndEquipmentNet', END, '9') + '</td></tr>')
        filing.raw('balance', equipment)
        filing.raw('balance', '<tr><td></td><td>Commitments and contingencies (Note 12)</td><td></td></tr>')
        for concept in ('Assets', 'Liabilities', 'StockholdersEquity'):
            filing.raw('balance', f'<tr><td></td><td>Total {concept}</td><td>{filing.tag(concept, END)}</td></tr>')
        filing.raw('balance', extra(filing) if callable(extra) else extra)
        return filing

    def test_tagged_amounts_in_a_label_are_part_of_the_label(self):
        filing = self.filing()
        structures, tables = filing.extract()
        self.assertEqual(unresolved(structures), [])
        cells = {cell['display_text']: cell for row in structures['balance_sheet']['rows'] for cell in row}
        label = cells['Property and equipment, net of accumulated depreciation of 5 and 4']
        self.assertEqual((label['column'], label['classification'], len(label['fact_references'])), (1, 'resolved_label', 2))
        self.assertEqual({reference['period']['instant'] for reference in label['fact_references']}, {'2025-06-30', '2024-12-31'})
        # The label column is no value column of the statement.
        self.assertEqual([column['column'] for column in structures['balance_sheet']['presentation_columns']], [2])
        self.assertEqual(cells['Commitments and contingencies (Note 12)']['classification'], 'label')
        # A label without a digit keeps the classification it always had there.
        self.assertEqual(cells['Total Assets']['classification'], 'formatting')
        validate_statement_segments(tables, filing.tree(), filing.facts, corroborated_layouts=True)

    def test_without_the_option_the_label_stays_unresolved(self):
        structures, _ = self.filing().extract(corroborated_layouts=False)
        self.assertEqual(unresolved(structures), [
            ('Property and equipment, net of accumulated depreciation of 5 and 4', 'Unresolved column period'),
            ('Commitments and contingencies (Note 12)', 'untagged_financial_cell')])

    def test_a_column_that_prints_a_number_or_follows_another_cell_holds_no_labels(self):
        structures, _ = self.filing(
            lambda filing: '<tr><td></td><td>' + filing.tag('OtherAssets', END, '3') + '</td><td></td></tr>').extract()
        self.assertIn(('Property and equipment, net of accumulated depreciation of 5 and 4', 'Unresolved column period'),
                      unresolved(structures))
        tree = html.document_fromstring(
            '<table><tr><td></td><td></td><td>June 30, 2025</td></tr>'
            '<tr><td>Cash</td><td>held in trust</td><td>1</td></tr><tr><td></td><td>Total assets</td><td>2</td></tr></table>').getroottree()
        rows, _ = layout_grid(tree.xpath('//table')[0], tree)
        self.assertEqual(_label_columns(rows, 1, {1: None, 2: END}), frozenset())
        self.assertEqual(_label_columns(rows[:1] + rows[2:], 1, {1: None, 2: END}), frozenset({1}))
        # A column the header dates is a value column whatever it prints.
        self.assertEqual(_label_columns(rows[:1] + rows[2:], 1, {1: END, 2: END}), frozenset())

    def test_extended_selection_reads_past_the_label_column(self):
        filing = self.filing()
        tree, facts = filing.tree(), _segment_facts(filing.facts)
        table = tree.xpath('//table[@id="balance"]')[0]
        identifiers = {key: 1 for key in facts}
        with self.assertRaisesRegex(TableExtractionError, 'unresolved column period'):
            _segment_candidate(tree, table, 'balance_sheet', facts, identifiers)
        candidate = _segment_candidate(tree, table, 'balance_sheet', facts, identifiers, label_columns=True)
        self.assertEqual(candidate['axes'], ['{"instant": "2025-06-30"}'])
        self.assertEqual(candidate['coverage']['{"instant": "2025-06-30"}'], {0, 1, 2})


class NoteReferenceTests(unittest.TestCase):
    def filing(self, heading, reference):
        filing = Filing()
        filing.headers['income'] = f'<tr><td></td><td>{heading}</td><td>Six Months Ended June 30, 2025</td></tr>'
        filing.rows['income'] = [f'<tr><td>{concept}</td><td>{reference if concept == "Revenues" else ""}</td>'
                                 f'<td>{filing.tag(concept, HALF)}</td></tr>'
                                 for concept in ('Revenues', 'OperatingIncomeLoss', 'NetIncomeLoss')]
        return filing

    def test_a_reference_to_a_note_is_not_a_financial_number(self):
        for heading, reference in (('Notes', '3'), ('Note', '4, 5'), ('', '(Note 3)'), ('', 'Notes 6 and 7'), ('Notes', '(Note 3)')):
            with self.subTest(heading=heading, reference=reference):
                filing = self.filing(heading, reference)
                structures, _ = filing.extract()
                self.assertEqual(unresolved(structures), [])
                self.assertEqual(filing.cells('income_statement')[reference]['classification'], 'label')
                self.assertEqual(unresolved(filing.extract(corroborated_layouts=False)[0]), [(reference, 'untagged_financial_cell')])

    def test_a_bare_number_needs_the_notes_heading(self):
        for heading, reference in (('', '3'), ('Ref', '3'), ('Notes', '1,234'), ('Notes', '2025'), ('', '(3)'), ('', 'Note 3 of 12')):
            with self.subTest(heading=heading, reference=reference):
                self.assertEqual(unresolved(self.filing(heading, reference).extract()[0]), [(reference, 'untagged_financial_cell')])

    def test_a_column_with_a_period_or_a_tagged_number_holds_no_references(self):
        cell = {'column': 1, 'column_period': None, 'column_headings': ['Notes'], 'display_text': '3'}
        self.assertTrue(_note_reference(cell, frozenset({2})))
        self.assertFalse(_note_reference(cell, frozenset({1, 2})))
        self.assertFalse(_note_reference({**cell, 'column_period': HALF}, frozenset()))
        self.assertFalse(_note_reference({**cell, 'display_text': '(Note 3)', 'column_period': HALF}, frozenset()))


class PeriodEndBalanceTests(unittest.TestCase):
    def filing(self, label, period=END):
        filing = Filing()
        filing.line('income', label, 'CommonStockSharesOutstanding', period, '493')
        return filing

    def test_a_balance_the_row_dates_at_the_period_end_is_accepted_in_an_income_statement(self):
        for label in ('Shares outstanding at June 30,', 'Shares outstanding at June 30, 2025', 'Actual shares outstanding at period end',
                      'Shares outstanding at year-end', 'Shares outstanding as of the end of the period'):
            with self.subTest(label=label):
                filing = self.filing(label)
                structures, tables = filing.extract()
                self.assertEqual(unresolved(structures), [])
                cell = filing.cells('income_statement')['493']
                self.assertEqual((cell['classification'], cell['fact_references'][0]['period']), ('resolved', END))
                self.assertEqual(cell['column_period'], HALF)
                validate_statement_segments(tables, filing.tree(), filing.facts, corroborated_layouts=True)
                self.assertEqual(unresolved(filing.extract(corroborated_layouts=False)[0]),
                                 [('493', 'Fact period differs from column heading')])

    def test_another_day_or_a_label_that_names_no_period_end_is_refused(self):
        refused = (('Shares outstanding', END), ('Shares outstanding at March 31,', END), ('Shares outstanding at June 30, 2024', END),
                   ('Shares outstanding at June 30,', OPENING), ('Shares outstanding at period end', {'instant': '2025-03-31'}),
                   ('Shares outstanding at beginning of period', OPENING))
        for label, period in refused:
            with self.subTest(label=label, period=period):
                self.assertEqual(unresolved(self.filing(label, period).extract()[0]),
                                 [('493', 'Fact period differs from column heading')])

    def test_only_a_duration_column_has_a_period_end(self):
        self.assertTrue(_labelled_period_end('Shares outstanding at June 30,', HALF))
        self.assertFalse(_labelled_period_end('Shares outstanding at June 30,', END))
        self.assertFalse(_labelled_period_end(None, HALF))


class CashComponentTests(unittest.TestCase):
    def filing(self, *, held='4', total='7', components=True, extra=''):
        filing = Filing()
        filing.raw('cash', '<tr><td>Reconciliation of cash and funds held on behalf of clients:</td><td></td></tr>')
        if components:
            filing.line('cash', 'Cash and cash equivalents', 'CashAndCashEquivalentsAtCarryingValue', END, '3')
        filing.line('cash', 'Funds held on behalf of clients', 'FundsHeldForClients', END, held)
        filing.raw('cash', extra)
        filing.line('cash', 'Total cash and funds held on behalf of clients', TOTAL_CASH, END, total)
        return filing

    def test_components_that_add_up_to_the_total_below_them_are_cash_balances(self):
        filing = self.filing()
        structures, tables = filing.extract()
        self.assertEqual(unresolved(structures), [])
        cells = filing.cells('cash_flow')
        held = cells['4']
        self.assertEqual((held['classification'], held['fact_references'][0]['period'], held['column_period']), ('resolved', END, HALF))
        total = [key for key, value in filing.facts.items() if value['concept'].endswith(TOTAL_CASH)][-1]
        self.assertEqual(structures['cash_flow']['cash_component_sums'], [{
            'verification_version': 'cash-component-sum-v1', 'total_xml_id': total,
            'component_xml_ids': [held['fact_references'][0]['xml_id']]}])
        validate_statement_segments(tables, filing.tree(), filing.facts, corroborated_layouts=True)
        self.assertIn(('4', 'Unexpected instant in cash-flow row'), unresolved(filing.extract(corroborated_layouts=False)[0]))

    def test_a_block_that_does_not_add_up_proves_nothing(self):
        dated = '<tr><td>Restricted cash</td><td>{}</td></tr>'
        cases = {
            'the total differs': self.filing(total='8'),
            'a single component': self.filing(components=False, held='7'),
            'a component of another day': self.filing(extra=dated.format(Filing().tag('RestrictedCash', OPENING, '0').replace('f0', 'x'))),
        }
        cases['a component of another day'].facts['x'] = fact('x', 'RestrictedCash', OPENING, '0')
        for name, filing in cases.items():
            with self.subTest(name):
                structures, _ = filing.extract()
                self.assertIn(('4' if name != 'a single component' else '7', 'Unexpected instant in cash-flow row'),
                              unresolved(structures))
                self.assertNotIn('cash_component_sums', structures['cash_flow'])

    def test_a_liability_in_a_supplemental_row_stays_a_company_tagging_error(self):
        # Reviewed judgment: a balance under a period column that no rule proves is recorded only by the flagged profile.
        filing = self.filing()
        filing.line('cash', 'Dividends declared, not yet paid', 'DividendsPayableCurrentAndNoncurrent', END, '2')
        self.assertEqual(unresolved(filing.extract()[0]), [('2', 'Unexpected instant in cash-flow row')])

    def test_the_proof_is_computed_from_the_rows_alone(self):
        filing = self.filing()
        tree = filing.tree()
        rows, _ = layout_grid(tree.xpath('//table[@id="cash"]')[0], tree)
        proven = _summed_cash_components(rows, 1, filing.facts, {key: 1 for key in filing.facts})
        components = {filing.facts[key]['concept'].rsplit('}', 1)[-1] for key in proven}
        self.assertEqual(components, {'CashAndCashEquivalentsAtCarryingValue', 'FundsHeldForClients'})
        self.assertEqual(len(set(proven.values())), 1)
        # An identifier that occurs twice in the document identifies nothing.
        self.assertEqual(_summed_cash_components(rows, 1, filing.facts, {key: 2 for key in filing.facts}), {})


class FiscalYearColumnTests(unittest.TestCase):
    YEAR_2024 = {'startDate': '2023-12-30', 'endDate': '2025-01-03'}
    YEAR_END = {'instant': '2025-01-03'}

    def periods(self, header_rows, period, *, extra=(), concepts=('Assets', 'Liabilities', 'StockholdersEquity')):
        facts, rows = {}, []
        for index, concept in enumerate(concepts):
            facts[f'c{index}'] = fact(f'c{index}', concept, period)
            rows.append(f'<tr><td>{concept}</td><td><ix:nonfraction id="c{index}" name="g:{concept}" contextref="c{index}" '
                        f'unitref="usd">1</ix:nonfraction></td></tr>')
        for index, other in enumerate(extra):
            facts[f'e{index}'] = other
        tree = html.document_fromstring(
            f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body><table>{header_rows}{"".join(rows)}'
            '</table></body></html>').getroottree()
        grid, _ = layout_grid(tree.xpath('//table')[0], tree)
        first = header_rows.count('<tr>')
        periods, provenance = _column_periods(grid, first, facts, {'g': GAAP}, {key: 1 for key in facts}, advanced=True,
                                              corroborate=True)
        return periods[1], provenance.get('1', {}), grid

    def test_a_balance_column_headed_by_the_fiscal_year_that_closed_in_early_january(self):
        year = fact('year', 'Revenues', self.YEAR_2024)
        for header in ('<tr><td>At the End of Year</td><td>2024</td></tr>', '<tr><td></td><td>Fiscal Year End</td></tr><tr><td></td><td>2024</td></tr>'):
            with self.subTest(header=header):
                period, evidence, _ = self.periods(header, self.YEAR_END, extra=(year,))
                self.assertEqual(period, self.YEAR_END)
                self.assertEqual((evidence['method'], evidence['anchor'], evidence['printed_year']),
                                 ('corroborated_printed_header', 'fiscal_year_end', 2024))
        # The next fiscal year's first period also shows the day the year closed on.
        quarter = fact('quarter', 'Revenues', {'startDate': '2025-01-04', 'endDate': '2025-04-04'})
        self.assertEqual(self.periods('<tr><td></td><td>2024</td></tr>', self.YEAR_END, extra=(quarter,))[0], self.YEAR_END)

    def test_the_filing_must_show_a_fiscal_period_to_close_on_that_day(self):
        header = '<tr><td></td><td>2024</td></tr>'
        self.assertIsNone(self.periods(header, self.YEAR_END)[0])
        dimensioned = {**fact('year', 'Revenues', self.YEAR_2024), 'dimensions': [{'kind': 'explicit', 'dimension': 'd', 'member': 'm'}]}
        self.assertIsNone(self.periods(header, self.YEAR_END, extra=(dimensioned,))[0])
        # Only the first week of January is still the year before; a printed date must be the tagged one.
        february = {'instant': '2025-02-01'}
        closing = fact('year', 'Revenues', {'startDate': '2024-02-04', 'endDate': '2025-02-01'})
        self.assertIsNone(self.periods(header, february, extra=(closing,))[0])
        year = fact('year', 'Revenues', self.YEAR_2024)
        self.assertEqual(self.periods('<tr><td></td><td>December 31, 2024</td></tr>', self.YEAR_END, extra=(year,))[0],
                         {'instant': '2024-12-31'})
        self.assertIsNone(self.periods('<tr><td></td><td>2023</td></tr>', self.YEAR_END, extra=(year,))[0])

    def test_fiscal_year_boundaries_come_from_undimensioned_periods(self):
        facts = {'year': fact('year', 'Revenues', self.YEAR_2024), 'quarter': fact('quarter', 'Revenues', {'startDate': '2025-01-04', 'endDate': '2025-04-04'}),
                 'date': fact('date', 'Assets', self.YEAR_END)}
        self.assertEqual(_fiscal_year_boundaries(facts), {'2023-12-29', '2025-01-03'})

    def test_a_duration_column_names_the_year_its_period_lies_in(self):
        income = ('Revenues', 'OperatingIncomeLoss', 'NetIncomeLoss')
        fiscal_2022 = {'startDate': '2022-01-03', 'endDate': '2023-01-01'}
        period, evidence, _ = self.periods('<tr><td></td><td>For the Fiscal Year</td></tr><tr><td></td><td>2022</td></tr>',
                                           fiscal_2022, concepts=income)
        self.assertEqual((period, evidence['anchor']), (fiscal_2022, 'printed_duration'))
        # A year that ends later in January is named for the year it ends in.
        later = {'startDate': '2022-01-30', 'endDate': '2023-01-28'}
        self.assertIsNone(self.periods('<tr><td></td><td>For the Fiscal Year</td></tr><tr><td></td><td>2022</td></tr>',
                                       later, concepts=income)[0])
        self.assertEqual(_labelled_years(__import__('datetime').date(2023, 1, 1), None), {2023, 2022})
        self.assertEqual(_labelled_years(__import__('datetime').date(2023, 1, 8), None), {2023})
        self.assertEqual(_labelled_years(__import__('datetime').date(2023, 1, 28), 1), {2023, 2022})

    def test_a_year_glued_to_the_next_word_or_to_a_footnote_marker_is_still_a_year(self):
        income = ('Revenues', 'OperatingIncomeLoss', 'NetIncomeLoss')
        year = {'startDate': '2024-01-01', 'endDate': '2024-12-31'}
        header = '<tr><td></td><td>For the Years Ended December 31,</td></tr><tr><td></td><td>2024<span>As Adjusted</span></td></tr>'
        period, evidence, _ = self.periods(header, year, concepts=income)
        self.assertEqual((period, evidence['anchor']), (year, 'printed_duration'))
        header = '<tr><td></td><td>Six Months Ended June 30,</td></tr><tr><td></td><td>2025<sup>1</sup></td></tr>'
        period, evidence, grid = self.periods(header, HALF, concepts=income)
        self.assertEqual((period, evidence['anchor']), (HALF, 'printed_duration'))
        # The header cell keeps the text the page prints.
        self.assertEqual(grid[1][1]['display_text'], '20251')
        # Without the raised marker "20251" is no year.
        self.assertIsNone(self.periods(header.replace('<sup>1</sup>', '1'), HALF, concepts=income)[0])


class RatioColumnTests(unittest.TestCase):
    """A column of percentages printed beside its amounts takes the period of those amounts."""

    QUARTER = {'startDate': '2024-01-01', 'endDate': '2024-03-31'}
    PURE = {'numerator': ['{http://www.xbrl.org/2003/instance}pure'], 'denominator': []}

    def periods(self, *, caption='Percent to Sales', unit=None, ratio_period=None):
        facts, rows = {}, []
        for index, concept in enumerate(('Revenues', 'CostOfRevenue', 'NetIncomeLoss')):
            amount, ratio = f'a{index}', f'r{index}'
            facts[amount] = fact(amount, concept, self.QUARTER)
            facts[ratio] = {**fact(ratio, concept + 'PercentToSales', ratio_period or self.QUARTER), 'unit': unit or self.PURE,
                            'unit_ref': 'pure'}
            rows.append(f'<tr><td>{concept}</td>'
                        f'<td><ix:nonfraction id="{amount}" name="g:{concept}" contextref="{amount}" unitref="usd">1</ix:nonfraction></td>'
                        f'<td><ix:nonfraction id="{ratio}" name="g:{concept}PercentToSales" contextref="{ratio}" unitref="pure">1'
                        f'</ix:nonfraction></td></tr>')
        tree = html.document_fromstring(
            f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body><table>'
            '<tr><td></td><td colspan="2">Fiscal First Quarter Ended</td></tr>'
            f'<tr><td></td><td>March 31, 2024</td><td>{caption}</td></tr>{"".join(rows)}</table></body></html>').getroottree()
        grid, _ = layout_grid(tree.xpath('//table')[0], tree)
        periods, provenance = _column_periods(grid, 2, facts, {'g': GAAP}, {key: 1 for key in facts}, advanced=True, corroborate=True)
        return periods, provenance

    def test_percentages_beside_a_dated_column_take_its_period(self):
        for caption in ('Percent to Sales', 'Percentto Sales', '% of Sales'):
            with self.subTest(caption=caption):
                periods, provenance = self.periods(caption=caption)
                self.assertEqual((periods[1], periods[2]), (self.QUARTER, self.QUARTER))
                self.assertEqual((provenance['2']['anchor'], provenance['2']['amount_column']), ('ratio_of_adjacent_column', 1))

    def test_amounts_another_period_or_a_caption_that_says_no_percent_are_refused(self):
        usd = {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []}
        for options in ({'unit': usd}, {'ratio_period': {'startDate': '2023-01-01', 'endDate': '2023-03-31'}}, {'caption': 'Pro Forma'}):
            with self.subTest(options=options):
                self.assertIsNone(self.periods(**options)[0][2])


class TypedDimensionTests(unittest.TestCase):
    """A cell's reference states a typed dimension exactly as the stored fact does (all profiles)."""

    INSTANCE = """<?xml version="1.0" encoding="utf-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
    xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink"
    xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:us-gaap="http://fasb.org/us-gaap/2026" xmlns:ex="http://example.com/2026">
  <link:schemaRef xlink:type="simple" xlink:href="https://example.com/ex.xsd"/>
  <xbrli:context id="location">
    <xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001</xbrli:identifier><xbrli:segment>
      <xbrldi:typedMember dimension="us-gaap:StatementOfFinancialPositionLocationBalanceAxis"><us-gaap:QName.domain>us-gaap:OtherAssets</us-gaap:QName.domain></xbrldi:typedMember>
    </xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
  <us-gaap:RestrictedCash id="restricted" contextRef="location" unitRef="usd" decimals="-6">5000000</us-gaap:RestrictedCash>
</xbrli:xbrl>"""

    def test_the_table_reader_and_the_fact_extractor_give_one_value(self):
        from sec_pipeline.fact_extraction import extract_facts
        from sec_pipeline.source_cells_v3 import fact_reference
        from sec_pipeline.tables import xml_evidence

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'instance.xml'
            path.write_text(self.INSTANCE, encoding='utf-8')
            evidence = xml_evidence(path)['restricted']
            stored, = extract_facts(path, filing_id=UUID(int=1), source_document_id=UUID(int=2)).facts
        value = '<us-gaap:QName.domain xmlns:us-gaap="http://fasb.org/us-gaap/2026">us-gaap:OtherAssets</us-gaap:QName.domain>'
        self.assertEqual(evidence['dimensions'], [{
            'kind': 'typed', 'dimension': '{http://fasb.org/us-gaap/2026}StatementOfFinancialPositionLocationBalanceAxis', 'value': value}])
        self.assertEqual(evidence['dimensions'], fact_reference(stored)['dimensions'])


class HeaderLineTests(unittest.TestCase):
    def test_period_lines_and_units_lines_printed_between_a_title_and_its_table(self):
        for line in ('For the Three Months Ended March 30, 2024 and April 1, 2023, respectively',
                     '(In US$ and in thousands, except share and per share data)', '(In US$ and in thousands)'):
            with self.subTest(line=line):
                self.assertTrue(_line_metadata(line, ()))
        for line in ('The amounts for 2024 and 2023 are presented respectively in Note 3', '(In US$ and in thousands of tons shipped)'):
            with self.subTest(line=line):
                self.assertFalse(_line_metadata(line, ()))


if __name__ == '__main__':
    unittest.main()
