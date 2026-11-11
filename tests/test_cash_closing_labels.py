"""A headless closing table is identified by its row labels and proved by its balances."""

from pathlib import Path
from uuid import UUID

from lxml import html
import pytest

from sec_pipeline.tables import TableExtractionError, select_statement_segments


GAAP = 'http://fasb.org/us-gaap/2026'
CASH = 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
PERIOD = {'startDate': '2025-01-01', 'endDate': '2025-12-31'}
PLAIN = ('Net increase in cash and cash equivalents', 'Cash and cash equivalents at beginning of the period',
         'Cash and cash equivalents at end of the period')


def document(labels=PLAIN, *, closing='25'):
    definitions = [
        ('income', 'Consolidated Statements of Income', 'Year Ended December 31, 2025',
         [('Revenues', 'Revenues', '100', PERIOD), ('Operating income', 'OperatingIncomeLoss', '10', PERIOD),
          ('Net income', 'NetIncomeLoss', '8', PERIOD)]),
        ('balance', 'Consolidated Balance Sheets', 'December 31, 2025',
         [(name, name, value, {'instant': '2025-12-31'})
          for name, value in (('Assets', '100'), ('Liabilities', '40'), ('StockholdersEquity', '60'))]),
        ('cash', 'Consolidated Statements of Cash Flows', 'Year Ended December 31, 2025',
         [('Net cash provided by operating activities', 'NetCashProvidedByUsedInOperatingActivities', '10', PERIOD),
          ('Net cash used in investing activities', 'NetCashProvidedByUsedInInvestingActivities', '-3', PERIOD),
          ('Net cash used in financing activities', 'NetCashProvidedByUsedInFinancingActivities', '-2', PERIOD)]),
        ('closing', None, '',
         [(labels[0], CASH + 'PeriodIncreaseDecreaseIncludingExchangeRateEffect', '5', PERIOD),
          (labels[1], CASH, '20', {'instant': '2024-12-31'}), (labels[2], CASH, closing, {'instant': '2025-12-31'})]),
    ]
    tables, facts = [], {}
    for key, heading, header, lines in definitions:
        rows = [f'<tr><td></td><td>{header}</td></tr>']
        for index, (label, concept, value, period) in enumerate(lines):
            identifier = f'{key}-{index}'
            rows.append(f'<tr><td>{label}</td><td><ix:nonfraction id="{identifier}" name="g:{concept}" '
                        f'contextref="{identifier}" unitref="usd">{value}</ix:nonfraction></td></tr>')
            facts[identifier] = {'xml_id': identifier, 'concept': '{' + GAAP + '}' + concept, 'context_ref': identifier,
                                 'unit_ref': 'usd', 'period': dict(period), 'raw_value': value, 'is_nil': False,
                                 'dimensions': [],
                                 'unit': {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []},
                                 'entity': {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'}}
        tables.append((f'<p>{heading}</p>' if heading else '') + f'<table id="{key}">{"".join(rows)}</table>')
    tree = html.document_fromstring(f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
                                   + ''.join(tables) + '</body></html>').getroottree()
    return tree, facts


@pytest.mark.parametrize('labels', [
    PLAIN,
    ('Net (decrease) increase in cash and cash equivalents', PLAIN[1], PLAIN[2]),
    ('Net increase (decrease) in cash and cash equivalents', PLAIN[1], PLAIN[2]),
    ('Net change in cash and cash equivalents', 'Cash and cash equivalents at beginning of period',
     'Cash and cash equivalents at end of period'),
    ('Net increase in cash, cash equivalents and restricted cash', PLAIN[1],
     'Cash, cash equivalents and restricted cash at end of the period'),
    ('Net (decrease) increase in cash, cash equivalents, and restricted cash',
     'Cash, cash equivalents, and restricted cash at beginning of the period',
     'Cash, cash equivalents, and restricted cash at end of the period'),
])
def test_closing_rows_are_recognized_across_printed_wordings(labels):
    tree, facts = document(labels)
    selected = select_statement_segments(tree, facts)
    assert [part.table_key for part in selected] == ['income_statement', 'balance_sheet',
                                                    'cash_flow.segment_1', 'cash_flow.segment_2']
    proof = selected[-1].group['closure_evidence'][0]
    assert (proof['opening_xml_id'], proof['change_xml_id'], proof['closing_xml_id']) == ('closing-1', 'closing-0', 'closing-2')
    assert selected[-1].group['header_inheritance']['columns']['1']['period'] == PERIOD


@pytest.mark.parametrize('labels', [
    ('Net sales increase', PLAIN[1], PLAIN[2]),
    (PLAIN[0], 'Inventory at beginning of the period', PLAIN[2]),
    (PLAIN[0], PLAIN[1], 'Restricted cash at end of the period'),
    (PLAIN[0], 'Cash and cash equivalents at beginning of the year', 'Cash and cash equivalents at end of the year'),
])
def test_other_rows_do_not_become_a_cash_closing_table(labels):
    tree, facts = document(labels)
    with pytest.raises(TableExtractionError):
        select_statement_segments(tree, facts)


@pytest.mark.parametrize('labels', [PLAIN, ('Net (decrease) increase in cash, cash equivalents and restricted cash',
                                            'Cash, cash equivalents and restricted cash at beginning of the period',
                                            'Cash, cash equivalents and restricted cash at end of the period')])
def test_accepted_wording_still_requires_balances_that_reconcile(labels):
    tree, facts = document(labels, closing='26')
    with pytest.raises(TableExtractionError, match='do not reconcile'):
        select_statement_segments(tree, facts)
