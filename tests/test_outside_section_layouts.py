"""Section layouts of two filings that the corrected profiles rejected.

A Clorox 10-K numbers its Items "Item 1.A." and prints its statements in an
exhibit with the company name under each title; a Con Edison 10-Q prints no
Item 1 heading above its statements. report-v4 reads all three from the
original document. Earlier report versions keep their exact output, and each
rule has the cases it must keep rejecting.
"""
from copy import deepcopy
from uuid import UUID

from lxml import html
import pytest

from sec_pipeline.chunking import chunk_report
from sec_pipeline.report_cleaning import clean_report
from sec_pipeline.report_validation import audit_report, source_heading_expectations
from sec_pipeline.source_locators import source_xpath
from tests.test_pipeline_audit_regressions import _filing, _source

EARLIER = (None, 'report-v1', 'report-v2', 'report-v3')


def _audit(path, form, version, report=None):
    report = clean_report(path, form=form, report_version=version) if report is None else report
    chunks = chunk_report(filing_id=UUID(int=1), report_id=UUID(int=2), full_text=report.full_text,
                          sections=report.sections, chunking_version='paragraph-v2')
    expected = source_heading_expectations(path, form=form, report_version=version)
    result = audit_report(path, form=form, report=report, chunks=chunks,
                          expected_section_ids=expected['expected_section_ids'], report_version=version)
    return report, expected, result


def _ids(report):
    return [row['section_id'] for row in report.sections]


def _checks(result):
    return {issue['check'] for issue in result['issues']}


# ------------------------------------------------------------ "Item 1.A."

_LETTERED = ('1A', '1B', '7A', '9A', '9B')


def _dotted(content, *, contents=True, body=True):
    """Rewrite the lettered Items of the standard annual fixture as "Item 1.A."."""
    head, table, rest = content.partition('</table>')
    for number in _LETTERED:
        dotted = f'Item {number[:-1]}.{number[-1]}.'
        if contents:
            head = head.replace(f'Item {number}.', dotted)
        if body:
            rest = rest.replace(f'Item {number}.', dotted)
    return head + table + rest


def test_report_v4_reads_dotted_item_numbers(tmp_path):
    path = _source(tmp_path, _dotted(_filing()))
    report, expected, result = _audit(path, '10-K', 'report-v4')
    assert result['status'] == 'passed', result['issues']
    assert expected['status'] == 'derived_from_original_toc'
    plain = tmp_path / 'plain'
    plain.mkdir()
    assert _ids(report) == _ids(clean_report(_source(plain, _filing()), form='10-K', report_version='report-v4'))
    risk = next(row for row in report.sections if row['section_id'] == 'part-i-item-1a')
    # The stored title uses the standard number; the original spelling is kept beside it.
    assert (risk['title'], risk['original_title'], risk['section_type']) == (
        'Item 1A. Risk Factors', 'Item 1.A. Risk Factors', 'risk_factors')
    assert report.full_text[risk['offset_start']:].startswith('Item 1.A. Risk Factors')
    assert {'part-i-item-1b', 'part-ii-item-7a', 'part-ii-item-9a', 'part-ii-item-9b'} <= set(_ids(report))


@pytest.mark.parametrize('contents,body', [(True, False), (False, True)])
def test_report_v4_matches_dotted_contents_with_plain_headings_and_the_reverse(tmp_path, contents, body):
    path = _source(tmp_path, _dotted(_filing(), contents=contents, body=body))
    report, expected, result = _audit(path, '10-K', 'report-v4')
    assert result['status'] == 'passed', result['issues']
    assert 'part-i-item-1a' in expected['expected_section_ids'] and 'part-i-item-1a' in _ids(report)


def test_earlier_versions_keep_rejecting_dotted_item_numbers(tmp_path):
    path = _source(tmp_path, _dotted(_filing()))
    first = clean_report(path, form='10-K')
    assert 'part-i-item-1a' not in _ids(first) and 'part-i-item-1' in _ids(first)
    for version in EARLIER:
        report, expected, result = _audit(path, '10-K', version)
        assert (report.full_text, report.sections) == (first.full_text, first.sections)
        assert (expected['status'], expected.get('reason')) == ('unverified', 'duplicate_original_toc_heading')
        assert result['status'] != 'passed'
    # The contents alone in dotted form: the expectation is unverified as before.
    path = _source(tmp_path, _dotted(_filing(), body=False))
    assert source_heading_expectations(path, form='10-K')['reason'] == 'duplicate_original_toc_heading'


def test_a_period_before_a_word_is_not_a_dotted_item_number(tmp_path):
    # "Item 1.Business" is Item 1 written without a space, and "Item 1. A ..." is Item 1.
    content = _filing(heading={'i1': '<p id="i1"><b>Item 1.Business</b></p>'})
    report, expected, result = _audit(_source(tmp_path, content), '10-K', 'report-v4')
    assert result['status'] == 'passed', result['issues']
    assert next(row['title'] for row in report.sections if row['section_id'] == 'part-i-item-1') == 'Item 1. Business'
    content = _filing(after_part={'II': '<p><b>Item 5. A Risk Factors summary follows.</b></p>'})
    report = clean_report(_source(tmp_path, content), form='10-K', report_version='report-v4')
    assert 'part-ii-item-5a' not in _ids(report) and _ids(report).count('part-i-item-1a') == 1


# ------------------------------------- Item 1 printed without its heading

_ROWS = ''.join(f'<tr><td>Line {index}</td><td>{index}00</td></tr>' for index in range(1, 13))
_INCOME = ('<div id="s1"></div><p>Example Corp</p><p><b>CONSOLIDATED INCOME STATEMENT (UNAUDITED)</b></p>'
           '<table><tr><td>Revenues</td><td>1,000</td></tr>' + _ROWS + '<tr><td>Net income</td><td>100</td></tr></table>')
_BALANCE = ('<div id="s2"></div><p>Example Corp</p><p><b>CONSOLIDATED BALANCE SHEET (UNAUDITED)</b></p>'
            '<table><tr><td>Total assets</td><td>5,000</td></tr><tr><td>Total liabilities</td><td>3,000</td></tr></table>')


def _grouped(*, group='<tr><td>Item 1.</td><td>Financial Statements (Unaudited)</td><td></td></tr>',
             children=('<tr><td></td><td>Consolidated Income Statement</td><td><a href="#s1">3</a></td></tr>'
                       '<tr><td></td><td>Consolidated Balance Sheet</td><td><a href="#s2">4</a></td></tr>'),
             statements=_INCOME + _BALANCE):
    """The standard quarterly fixture with Item 1 as a contents group label and no body heading."""
    content = _filing('10-Q', body_parts=False)
    row_start = content.index('<tr><td><a href="#q1">')
    row_end = content.index('</tr>', row_start) + len('</tr>')
    content = content[:row_start] + group + children + content[row_end:]
    heading = '<p id="q1"><b>Item 1. Financial Statements</b></p>'
    prose = '<p>Body text of Item 1. Financial Statements, with several words of prose.</p>'
    assert heading + prose in content
    return content.replace(heading + prose, statements)


def test_report_v4_starts_item_1_at_the_first_statement_its_contents_link(tmp_path):
    path = _source(tmp_path, _grouped())
    report, expected, result = _audit(path, '10-Q', 'report-v4')
    assert result['status'] == 'passed', result['issues']
    row = next(row for row in expected['rows'] if row['section_id'] == 'part-i-item-1')
    assert row['navigation_on_child_rows'] and row['navigation_targets'] == ['s1', 's2']
    section = next(row for row in report.sections if row['section_id'] == 'part-i-item-1')
    assert (section['title'], section['original_title'], section['section_type']) == (
        'Item 1. FINANCIAL STATEMENTS', 'Example Corp', 'financial_statements')
    locator = section['source_locator']
    assert (locator['heading_method'], locator['target_element_id']) == ('original_toc_target', 's1')
    assert (locator['toc_xpath'], locator['target_xpath']) == ('/html/body/table[1]/tr[2]', '/html/body/div[1]')
    body = report.full_text[section['offset_start']:section['offset_end']]
    assert body.startswith('Example Corp') and 'CONSOLIDATED BALANCE SHEET' in body and 'Item 2.' not in body
    assert _ids(report)[:3] == ['front-matter', 'part-i-item-1', 'part-i-item-2']


def test_earlier_versions_keep_rejecting_a_missing_item_1_heading(tmp_path):
    path = _source(tmp_path, _grouped())
    first = clean_report(path, form='10-Q')
    assert 'part-i-item-1' not in _ids(first)
    for version in EARLIER:
        report, expected, result = _audit(path, '10-Q', version)
        assert (report.full_text, report.sections) == (first.full_text, first.sections)
        assert 'section_inventory_mismatch' in _checks(result)


@pytest.mark.parametrize('options', [
    # The group label itself is a link: another layout, not established by this rule.
    {'group': '<tr><td><a href="#s1">Item 1.</a></td><td><a href="#s1">Financial Statements (Unaudited)</a></td><td></td></tr>'},
    # No statement row carries a link.
    {'children': '<tr><td></td><td>Consolidated Income Statement</td><td>3</td></tr>'},
    # A linked row whose target does not exist.
    {'children': '<tr><td></td><td>Consolidated Income Statement</td><td><a href="#missing">3</a></td></tr>'},
    # The linked page has prose and no statement.
    {'statements': '<div id="s1"></div><p>Example Corp</p><p>Results improved over the prior year quarter.</p>'
                   '<div id="s2"></div><p>More prose about the quarter.</p>'},
    # A second group label makes the contents ambiguous.
    {'children': '<tr><td></td><td>Consolidated Income Statement</td><td><a href="#s1">3</a></td></tr>'
                 '<tr><td>Item 1.</td><td>Financial Statements</td><td></td></tr>'
                 '<tr><td></td><td>Consolidated Balance Sheet</td><td><a href="#s2">4</a></td></tr>'},
])
def test_report_v4_assigns_no_item_1_without_complete_contents_evidence(tmp_path, options):
    path = _source(tmp_path, _grouped(**options))
    report, expected, result = _audit(path, '10-Q', 'report-v4')
    assert 'part-i-item-1' not in _ids(report)
    assert result['status'] != 'passed'


def test_a_printed_item_1_heading_is_used_as_before(tmp_path):
    statements = '<p id="q1"><b>Item 1. Financial Statements</b></p>' + _INCOME + _BALANCE
    path = _source(tmp_path, _grouped(statements=statements))
    report, expected, result = _audit(path, '10-Q', 'report-v4')
    assert result['status'] == 'passed', result['issues']
    section = next(row for row in report.sections if row['section_id'] == 'part-i-item-1')
    assert section['original_title'] == 'Item 1. Financial Statements'
    assert 'heading_method' not in section['source_locator']
    # The contents entry adds nothing when the body has its own heading.
    earlier = clean_report(path, form='10-Q', report_version='report-v3')
    assert _ids(report) == _ids(earlier)


def test_an_item_1_heading_split_over_two_lines_is_used_as_before(tmp_path):
    statements = '<p id="q1"><b>Item 1.<br>Financial Statements</b></p>' + _INCOME + _BALANCE
    path = _source(tmp_path, _grouped(statements=statements))
    report, expected, result = _audit(path, '10-Q', 'report-v4')
    assert result['status'] == 'passed', result['issues']
    section = next(row for row in report.sections if row['section_id'] == 'part-i-item-1')
    assert 'heading_method' not in section['source_locator']
    assert report.full_text[section['offset_start']:].startswith('Item 1.')


def test_the_independent_audit_rejects_an_item_1_that_starts_elsewhere(tmp_path):
    path = _source(tmp_path, _grouped())
    report, _, passed = _audit(path, '10-Q', 'report-v4')
    assert passed['status'] == 'passed'
    # The same section moved to the second linked statement, more than a page header away.
    moved = deepcopy(report)
    front, item = moved.sections[0], moved.sections[1]
    front['offset_end'] = item['offset_start'] = moved.full_text.index('Example Corp', item['offset_start'] + 1)
    tree = html.document_fromstring(path.read_bytes(), parser=html.HTMLParser(encoding='utf-8', no_network=True)).getroottree()
    second = tree.xpath('//div[@id="s2"]/following-sibling::p[1]')[0]
    assert ''.join(second.itertext()) == 'Example Corp'
    item['source_locator']['xpath'] = source_xpath(tree, second)
    _, _, result = _audit(path, '10-Q', 'report-v4', report=moved)
    assert _checks(result) == {'section_navigation_target_mismatch'}


# --------------------------- statements in an exhibit, company name below

# The period caption shares the table's own block, so the title is two blocks above it.
_EXHIBIT = ('<div>Exhibit 99.1</div><div><b>MANAGEMENT\'S DISCUSSION AND ANALYSIS</b></div>'
            '<div>Net sales grew on higher volume, as discussed below in several words of prose.</div>'
            '<div><b>CONSOLIDATED STATEMENTS OF EARNINGS</b></div>{between}'
            '<div><span>Years ended June 30</span><table><tr><td>Net sales</td><td>6,720</td></tr>'
            '<tr><td>Net earnings</td><td>810</td></tr></table></div>'
            '<div>See Notes to Consolidated Financial Statements</div>')


def test_report_v4_finds_exhibit_statements_titled_above_the_company_name(tmp_path):
    path = _source(tmp_path, _EXHIBIT.format(between='<div><i>The Example Company</i></div>'))
    report = clean_report(path, form='10-K', report_version='report-v4')
    assert [(row['section_id'], row['section_type']) for row in report.sections] == [
        ('full-report', 'unknown'), ('financial-statements-appendix', 'financial_statements')]
    appendix = report.sections[1]
    assert report.full_text[appendix['offset_start']:].startswith('CONSOLIDATED STATEMENTS OF EARNINGS')
    first = clean_report(path, form='10-K')
    assert _ids(first) == ['full-report']
    for version in EARLIER:
        earlier = clean_report(path, form='10-K', report_version=version)
        assert (earlier.full_text, earlier.sections) == (first.full_text, first.sections)


def test_report_v4_keeps_a_title_separated_from_its_table_by_prose_unassigned(tmp_path):
    between = '<div>The following discussion explains how management measures the results of each segment.</div>'
    report = clean_report(_source(tmp_path, _EXHIBIT.format(between=between)), form='10-K', report_version='report-v4')
    assert _ids(report) == ['full-report']
