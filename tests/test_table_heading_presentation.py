"""Row sections and physical alignment cells must not become data headings."""

from copy import deepcopy
from pathlib import Path
import os
from uuid import UUID

from lxml import etree, html
import pytest

from sec_pipeline.fact_extraction import extract_facts
from sec_pipeline.source_cells_v3 import transform_tables as transform_v3
from sec_pipeline.source_cells_v4 import transform_tables as transform_v4
from sec_pipeline.tables import (
    _presentation_column_groups, _presentation_value, column_headers, extract_tables, layout_grid,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(os.environ.get('SEC_TEST_SOURCE_ROOT', ROOT))
SCOPE = {'filing_id': UUID(int=1), 'html_document_id': UUID(int=2), 'xml_document_id': UUID(int=3)}


def layout(markup):
    tree = html.document_fromstring(markup).getroottree()
    return tree, layout_grid(tree.xpath('//table')[0], tree)[0]


@pytest.mark.parametrize('section', ['ASSETS:', 'Current liabilities', 'Net sales:', 'Operating activities:'])
def test_full_width_row_sections_do_not_label_all_reporting_columns(section):
    tree, rows = layout(f'''<table>
        <tr><td colspan="7">(In millions, except per share data)</td></tr>
        <tr><td></td><td colspan="3">September 27, 2025</td><td colspan="3">September 28, 2024</td></tr>
        <tr><td colspan="7">(Unaudited)</td></tr>
        <tr><td colspan="7">{section}</td></tr>
        <tr><td>Cash</td><td colspan="3">42</td><td colspan="3">41</td></tr>
        </table>''')
    original = etree.tostring(tree)
    assert column_headers(rows, 4, 1) == [
        '(In millions, except per share data)', 'September 27, 2025', '(Unaudited)']
    assert column_headers(rows, 4, 4) == [
        '(In millions, except per share data)', 'September 28, 2024', '(Unaudited)']
    assert column_headers(rows, 4, 1, advanced=False)[-1] == section
    assert rows[3][0]['display_text'] == section
    assert etree.tostring(tree) == original


@pytest.mark.parametrize('units', ['(Dollars in millions)', '(Unaudited; in millions)', '(In millions, unaudited)'])
def test_shared_period_captions_and_units_after_dates_are_retained(units):
    _, rows = layout(f'''<table>
        <tr><td></td><td>2025</td><td>2024</td></tr>
        <tr><td colspan="3">Years ended September 27 and September 28</td></tr>
        <tr><td colspan="3">{units}</td></tr>
        <tr><td colspan="3">Consolidated Balance Sheets</td></tr>
        <tr><td>Cash</td><td>42</td><td>41</td></tr></table>''')
    assert column_headers(rows, 4, 1) == ['2025', 'Years ended September 27 and September 28',
                                         units, 'Consolidated Balance Sheets']


def presentation_rows(markup, first):
    _, rows = layout(markup)
    for row in rows:
        for cell in row:
            cell['classification'] = ('header' if cell['row'] < first else 'label' if cell['column'] == 0
                                     else 'resolved' if cell['display_text'].isdigit() else 'formatting')
    return rows


def test_alternate_merged_value_anchors_share_one_presentation_column():
    rows = presentation_rows('''<table><tr><td></td><td colspan="3">2025</td></tr>
        <tr><td>Sales</td><td>$</td><td colspan="2">42</td></tr>
        <tr><td>Profit</td><td colspan="3">15</td></tr></table>''', 1)
    original = deepcopy([{k: v for k, v in cell.items() if k != '_element'} for row in rows for cell in row])
    groups = _presentation_column_groups(rows, 1, [1, 2], {1: {'instant': '2025-12-31'}, 2: {'instant': '2025-12-31'}})
    assert groups == [[1, 2]]
    assert [_presentation_value(row, groups[0]) for row in rows[1:]] == ['$ 42', '15']
    assert [{k: v for k, v in cell.items() if k != '_element'} for row in rows for cell in row] == original


def test_same_period_metrics_in_separate_cells_are_not_combined_or_repeated():
    rows = presentation_rows('''<table><tr><td></td><td colspan="2">2025</td></tr>
        <tr><td>Metrics</td><td>42</td><td>15</td></tr></table>''', 1)
    groups = _presentation_column_groups(rows, 1, [1, 2], {1: {'instant': '2025-12-31'}, 2: {'instant': '2025-12-31'}})
    assert groups == [[1], [2]]
    assert [_presentation_value(rows[1], group) for group in groups] == ['42', '15']


def test_different_subheadings_and_unresolved_periods_do_not_merge():
    rows = presentation_rows('''<table><tr><td></td><td colspan="2">2025</td></tr>
        <tr><td></td><td>Basic</td><td>Diluted</td></tr>
        <tr><td>First metric</td><td>42</td><td></td></tr>
        <tr><td>Second metric</td><td></td><td>15</td></tr></table>''', 2)
    assert _presentation_column_groups(rows, 2, [1, 2], {1: {'instant': '2025-12-31'}, 2: {'instant': '2025-12-31'}}) == [[1], [2]]
    assert _presentation_column_groups(rows, 1, [1, 2], {1: None, 2: None}) == [[1], [2]]


def test_currency_prefix_of_next_value_does_not_attach_to_previous_value():
    rows = presentation_rows('''<table><tr><td>Amounts</td><td>42</td><td>$</td><td>15</td></tr>
        <tr><td>Loss</td><td>(</td><td>5</td><td>)</td></tr></table>''', 0)
    assert _presentation_value(rows[0], [1]) == '42'
    assert _presentation_value(rows[0], [3]) == '$ 15'
    assert _presentation_value(rows[1], [2]) == '( 5 )'


@pytest.mark.parametrize('accession,stem,liabilities,sales', [
    ('0000320193-25-000079', 'aapl-20250927', '285,508', ['416,161', '391,035', '383,285']),
    ('0000320193-26-000020', 'aapl-20260627', '275,746', ['109,417', '94,036', '364,357', '313,695']),
])
def test_archived_apple_k_and_q_keep_correct_headings_and_one_value_per_period(accession, stem, liabilities, sales):
    directory = SOURCE_ROOT / 'data/raw/sec' / accession
    html_path, xml_path = directory / f'{stem}.htm', directory / f'{stem}_htm.xml'
    if not html_path.is_file() or not xml_path.is_file():
        pytest.skip('Optional original Apple archive is unavailable; set SEC_TEST_SOURCE_ROOT to its project root.')
    v2 = extract_tables(html_path, xml_path, **SCOPE, tables_version='tables-v2')
    facts = extract_facts(xml_path, filing_id=SCOPE['filing_id'], source_document_id=SCOPE['xml_document_id']).require_complete().facts
    v3 = transform_v3(v2, html_path.read_bytes(), facts)
    v4 = transform_v4(v3, html_path.read_bytes(), facts)
    for tables in (v2, v3, v4):
        balance = next(t for t in tables if t.table_key == 'balance_sheet')
        cell = next(c for row in balance.structure['rows'] for c in row
                    if c['row_heading'] == 'Total liabilities' and c['display_text'] == liabilities)
        assert cell['classification'] == 'resolved'
        assert len(cell['column_headings']) == 1 and 'ASSETS:' not in cell['column_headings']
        assert cell['fact_references'][0]['concept'].endswith('}Liabilities')
        assert any(c['display_text'] == 'ASSETS:' for row in balance.structure['rows'] for c in row)
        income = next(t for t in tables if t.table_key == 'income_statement')
        assert len(income.structure['presentation_columns']) == len(sales)
        row = next(r for r in income.structure['presentation_rows'] if r['label'] == 'Total net sales')
        assert row['values'] == sales
        assert all(t.structure['inventory'].get('unresolved', 0) == 0 for t in tables)
