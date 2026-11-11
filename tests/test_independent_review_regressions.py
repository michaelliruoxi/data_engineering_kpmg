"""Unmodified regression assertions from the independent review."""
import hashlib

import pytest

from sec_pipeline.fact_extraction import check_inline_correspondence
from sec_pipeline.report_cleaning import clean_report
from sec_pipeline.report_validation import audit_report, source_heading_expectations
from sec_pipeline.table_sections import map_table_section
from tests.test_numeric_tag_boundary import TAG, document, fact  # noqa: F401
from tests.test_review_section_boundaries import annual

STATEMENTS = ('<p>Report of Independent Registered Public Accounting Firm</p>'
              '<h2>CONSOLIDATED BALANCE SHEETS</h2>'
              '<table id="balance"><tr><td>Total assets</td><td>100</td></tr></table>')
SIGNATURES = '<h2>SIGNATURES</h2><p>Pursuant to the requirements, signed.</p>'


@pytest.mark.parametrize('extra', [
    '<h2>INDEX TO CONSOLIDATED FINANCIAL STATEMENTS AND SCHEDULE</h2>' + STATEMENTS + SIGNATURES,
    '<h2>INDEX TO CONSOLIDATED FINANCIAL STATEMENTS AND FINANCIAL STATEMENT SCHEDULES</h2>' + STATEMENTS + SIGNATURES,
    '<h2>CONSOLIDATED FINANCIAL STATEMENTS</h2>' + STATEMENTS + SIGNATURES,
    STATEMENTS + SIGNATURES,
    SIGNATURES + STATEMENTS,
])
def test_trailing_statements_are_separated_or_the_gate_fails(tmp_path, extra):
    path = annual(tmp_path, extra)
    report = clean_report(path, form='10-K')
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    result = audit_report(path, form='10-K', report=report, expected_section_ids=expected)
    table = {'structure': {'source_xpath': "//*[@id='balance']",
                           'html_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}
    mapping = map_table_section(table, {}, report.sections, path.read_bytes(), accession='test')
    types = {row['section_id']: row['section_type'] for row in report.sections}
    assert types.get(mapping.get('section_key')) == 'financial_statements' or result['status'] != 'passed', (
        mapping.get('section_key'), result['status'])


@pytest.mark.parametrize('body', [
    '<ul><li>Shares outstanding: {tag}1</li></ul>',
    '<p><a href="#note">Shares outstanding: {tag}1</a></p>',
    '<h3>Shares outstanding: {tag}1</h3>',
    '<p><ix:nonNumeric name="dei:EntityInformationFormerLegalOrRegisteredName" contextRef="asof">Shares: {tag}1</ix:nonNumeric></p>',
    '<ix:continuation id="more">Shares outstanding: {tag}1</ix:continuation>',
    '<ul><li>Shares outstanding: 9{tag}</li></ul>',
])
def test_adjacent_digit_is_reviewed_whatever_the_direct_parent(tmp_path, fact, body):
    source = tmp_path / 'filing.htm'
    source.write_text(document(body.format(tag=TAG)))
    assert check_inline_correspondence(source, [fact])['numeric_tag_boundary_issues']


def test_singular_signature_heading_is_separated_from_exhibits(tmp_path):
    path = annual(tmp_path, '<h2>SIGNATURE</h2><p>Pursuant to the requirements, signed.</p>')
    report = clean_report(path, form='10-K')
    assert 'signatures' in {row['section_id'] for row in report.sections}
