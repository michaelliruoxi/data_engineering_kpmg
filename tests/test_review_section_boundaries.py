"""Independent regressions for the November section-boundary review."""
from copy import deepcopy
import hashlib

from sec_pipeline.report_cleaning import clean_report
from sec_pipeline.report_validation import audit_report, source_heading_expectations
from sec_pipeline.table_sections import map_table_section
import pytest


def source(tmp_path, content):
    path = tmp_path / 'filing.htm'
    path.write_text('<html><body>' + content + '</body></html>')
    return path


def checks(result):
    return {row['check'] for row in result['issues']}


def annual(tmp_path, extra='', toc_extra=''):
    headings = [('one', 'Item 1. Business'), ('two', 'Item 2. Properties'),
                ('three', 'Item 3. Legal Proceedings'), ('seven', "Item 7. Management's Discussion"),
                ('eight', 'Item 8. Financial Statements'), ('fifteen', 'Item 15. Exhibits'),
                ('sixteen', 'Item 16. Form 10-K Summary')]
    toc = '<table>' + ''.join(f'<tr><td><a href="#{key}">{title}</a></td><td>3</td></tr>'
                             for key, title in headings) + toc_extra + '</table>'
    body = ''.join(f'<h2 id="{key}">{title}</h2><p>Body of {key}.</p>' for key, title in headings)
    return source(tmp_path, toc + body + extra)


def test_post_part_iv_statements_and_signatures_are_distinct_body_sections(tmp_path):
    # Take-Two presents Item 8 by cross-reference to this separately paginated appendix.
    path = annual(tmp_path, '<h2>INDEX TO FINANCIAL STATEMENTS</h2>'
                  '<p>Report of Independent Registered Public Accounting Firm</p>'
                  '<h2>CONSOLIDATED BALANCE SHEETS</h2>'
                  '<table id="balance"><tr><td>Total assets</td><td>100</td></tr></table>'
                  '<h2>SIGNATURES</h2><p>Pursuant to the requirements, signed.</p>')
    report = clean_report(path, form='10-K')
    by_key = {row['section_id']: row for row in report.sections}
    balance = report.full_text.index('CONSOLIDATED BALANCE SHEETS')
    financials = by_key['financial-statements-appendix']
    assert financials['section_type'] == 'financial_statements'
    assert financials['offset_start'] < balance < financials['offset_end']
    assert by_key['part-iv-item-16']['offset_end'] == financials['offset_start']
    assert financials['offset_end'] == by_key['signatures']['offset_start']
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    assert audit_report(path, form='10-K', report=report, expected_section_ids=expected)['status'] == 'passed'
    table = {'structure': {'source_xpath': "//*[@id='balance']", 'html_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}
    mapping = map_table_section(table, {}, report.sections, path.read_bytes(), accession='test')
    assert mapping['mapping_status'] == 'mapped'
    assert mapping['section_key'] == 'financial-statements-appendix'

    stale = deepcopy(report)
    stale.sections = [row for row in stale.sections if row['section_id'] not in {'financial-statements-appendix', 'signatures'}]
    stale.sections[-1]['offset_end'] = len(stale.full_text)
    result = audit_report(path, form='10-K', report=stale, expected_section_ids=expected)
    assert 'unnumbered_body_boundary_missing' in checks(result)


def test_executive_officers_do_not_inherit_mine_safety(tmp_path):
    path = source(tmp_path, '<h2>Item 1. Business</h2><p>Operations.</p>'
                  '<h2>Item 4. Mine Safety Disclosures</h2><p>Not applicable.</p>'
                  '<h2>INFORMATION ABOUT OUR EXECUTIVE OFFICERS</h2><p>Our chief executive...</p>'
                  '<h2>Item 5. Market for Registrant Common Equity</h2><p>Share prices.</p>')
    report = clean_report(path, form='10-K')
    officers = next(row for row in report.sections if row['section_id'] == 'executive-officers')
    assert officers['section_type'] == 'directors_governance'
    assert 'Our chief executive' in report.full_text[officers['offset_start']:officers['offset_end']]


def test_audit_gate_rejects_page_column_toc_start_without_cli_wrapper(tmp_path):
    path = annual(tmp_path)
    report = clean_report(path, form='10-K')
    row = next(section for section in report.sections if section['section_id'] == 'part-i-item-1')
    row['source_locator']['xpath'] = '/html/body/table/tr[1]'
    row['source_locator'].pop('element_id', None)
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    assert 'section_starts_in_original_toc' in checks(audit_report(path, form='10-K', report=report, expected_section_ids=expected))


def test_combined_item_heading_requires_review_instead_of_false_pass(tmp_path):
    path = annual(tmp_path)
    path.write_text(path.read_text().replace('Item 3. Legal Proceedings', 'Items 3 and 4. Legal Proceedings and Mine Safety Disclosures'))
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    result = audit_report(path, form='10-K', expected_section_ids=expected)
    assert 'unsupported_combined_item_heading' in checks(result)
    assert result['status'] == 'failed'


def test_combined_item_omission_notice_is_not_a_body_heading(tmp_path):
    path = annual(tmp_path, '<p>Items 3 and 4 are not applicable and have been omitted.</p>')
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    assert audit_report(path, form='10-K', expected_section_ids=expected)['status'] == 'passed'


def test_unlinked_cross_reference_list_cannot_pass_as_body_outline(tmp_path):
    path = annual(tmp_path)
    path.write_text(path.read_text().replace('<p>Body of one.</p>', '<p>Body of one.</p>'
                    '<ul><li>Item 2. Properties</li><li>Item 3. Legal Proceedings</li></ul><p>Cross-references.</p>'))
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    result = audit_report(path, form='10-K', expected_section_ids=expected)
    assert result['status'] == 'failed'
    assert 'section_order_mismatch' in checks(result)


def test_rejected_intervening_boundary_does_not_extend_previous_section(tmp_path):
    path = source(tmp_path, '<p id="previous">Item 2. Properties</p><p>Our properties.</p>'
                  '<div id="legal"><b>Item 3. Legal Proceedings.</b> '
                  '<span>See <a href="#notes">Note 12</a>.</span></div>'
                  '<table id="claims"><tr><td>Claims</td><td>100</td></tr></table>')
    report = clean_report(path, form='10-K')
    table = {'structure': {'source_xpath': "//*[@id='claims']", 'html_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}
    mapping = map_table_section(table, {}, report.sections, path.read_bytes(), accession='test')
    assert mapping['mapping_status'] == 'ambiguous'
    assert mapping['section_key'] is None


def test_index_label_with_page_column_is_not_a_trailing_body_boundary(tmp_path):
    path = annual(tmp_path, '<table><tr><td><p>Index to Financial Statements</p></td>'
                  '<td><a href="#eight">40</a></td></tr></table><p>Exhibit listing.</p>')
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    report = clean_report(path, form='10-K')
    assert 'financial-statements-appendix' not in {s['section_id'] for s in report.sections}
    assert audit_report(path, form='10-K', report=report, expected_section_ids=expected)['status'] == 'passed'


def test_heading_ending_in_note_number_cannot_disappear_from_source_expectations(tmp_path):
    path = annual(tmp_path)
    path.write_text(path.read_text().replace('Item 3. Legal Proceedings', 'Item 3. Legal Proceedings — See Note 9'))
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    assert 'part-i-item-3' in expected
    assert audit_report(path, form='10-K', expected_section_ids=expected)['status'] == 'failed'


def test_nearby_cross_reference_cannot_replace_original_heading_even_in_correct_order(tmp_path):
    path = annual(tmp_path)
    path.write_text(path.read_text().replace('<h2 id="two">', '<p>Item 2. Properties</p><p>Cross-reference description.</p><h2 id="two">'))
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    result = audit_report(path, form='10-K', expected_section_ids=expected)
    assert result['status'] == 'failed'
    assert 'section_body_heading_position_mismatch' in checks(result)


def quarterly_without_toc(tmp_path, body):
    return source(tmp_path, '<h2>PART I - FINANCIAL INFORMATION</h2>'
                  '<h2>ITEM 1. FINANCIAL STATEMENTS</h2><p>Balance sheet.</p>'
                  '<h2>ITEM 2. MANAGEMENT\'S DISCUSSION AND ANALYSIS</h2><p>Results.</p>'
                  '<h2>PART II - OTHER INFORMATION</h2>'
                  '<h2>ITEM 1. LEGAL PROCEEDINGS</h2><p>Litigation.</p>' + body +
                  '<h2>ITEM 6. EXHIBITS</h2><p>Exhibit list.</p>')


def test_bare_split_block_item_label_cannot_disappear_from_both_detectors(tmp_path):
    path = quarterly_without_toc(tmp_path,
        '<div><b>ITEM 1A.</b></div><div><b>RISK FACTORS</b></div><p>Risk disclosure.</p>')
    expected = source_heading_expectations(path, form='10-Q')
    assert expected['status'] == 'derived_from_original_body'
    result = audit_report(path, form='10-Q', expected_section_ids=expected['expected_section_ids'])
    assert result['status'] == 'failed'
    assert 'unrepresented_original_item_heading' in checks(result)


def test_class_styled_issuer_item_title_cannot_disappear_from_both_detectors(tmp_path):
    path = quarterly_without_toc(tmp_path,
        '<div class="h2">Item 2. Issuer Purchases of Equity Securities</div><p>Buyback disclosure.</p>')
    expected = source_heading_expectations(path, form='10-Q')
    assert expected['status'] == 'derived_from_original_body'
    result = audit_report(path, form='10-Q', expected_section_ids=expected['expected_section_ids'])
    assert result['status'] == 'failed'
    assert 'unrepresented_original_item_heading' in checks(result)


def test_unsupported_parenthetical_item_modifier_cannot_certify_adjacent_item(tmp_path):
    path = annual(tmp_path, '<div><b>Item 9A(T). Controls and Procedures</b></div>'
                  '<p>Management evaluated disclosure controls.</p>')
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    result = audit_report(path, form='10-K', expected_section_ids=expected)
    assert result['status'] == 'failed'
    assert 'unsupported_item_heading_modifier' in checks(result)


def test_running_item_labels_and_prose_references_do_not_require_new_sections(tmp_path):
    path = quarterly_without_toc(tmp_path,
        '<p><b>ITEM 1.</b></p><p>Continued litigation disclosure.</p>'
        '<p><b>Item 3. refers to the earlier disclosure.</b></p>')
    expected = source_heading_expectations(path, form='10-Q')['expected_section_ids']
    result = audit_report(path, form='10-Q', expected_section_ids=expected)
    assert 'unrepresented_original_item_heading' not in checks(result)


def test_letter_spaced_item_number_does_not_invent_a_different_missing_item(tmp_path):
    # The original Microsoft 2021 annual report prints Item 16 as "ITEM 1 6".
    path = annual(tmp_path, '<p><b>ITEM 1 6 . FORM 10-K SUMMARY</b></p><p>Summary.</p>')
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    result = audit_report(path, form='10-K', expected_section_ids=expected)
    assert 'unrepresented_original_item_heading' not in checks(result)


@pytest.mark.parametrize('heading,body', [
    ('Financial Statements and Schedules',
     '<p>The financial statements are set forth under Part II, Item 8 of this Form 10-K.</p>'
     '<table><tr><td>Income Statements</td><td>58</td></tr>'
     '<tr><td>Balance Sheets</td><td>60</td></tr></table>'),
    ('Report of Independent Registered Public Accounting Firm',
     '<p>Consolidated Balance Sheets, April 30, 2021 and 2020</p>'
     '<p>Consolidated Statements of Income, Three Years Ended April 30, 2021</p>'),
])
def test_item15_financial_references_do_not_relabel_exhibits_as_an_appendix(tmp_path, heading, body):
    path = annual(tmp_path)
    references = '<h2>' + heading + '</h2>' + body + '<h2>Exhibit Listing</h2>'
    references += '<table id="exhibits"><tr><td>3.1</td><td>Articles of incorporation</td></tr></table>'
    path.write_text(path.read_text().replace('<p>Body of fifteen.</p>', '<p>Body of fifteen.</p>' + references))
    report = clean_report(path, form='10-K')
    assert 'financial-statements-appendix' not in {s['section_id'] for s in report.sections}
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    assert audit_report(path, form='10-K', report=report, expected_section_ids=expected)['status'] == 'passed'
    mapping = map_table_section({'structure': {'source_xpath': "//*[@id='exhibits']",
        'html_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}, {}, report.sections,
        path.read_bytes(), accession='test')
    assert mapping['section_key'] == 'part-iv-item-15'


def test_item15_actual_appended_statements_keep_financial_ownership(tmp_path):
    path = annual(tmp_path)
    appendix = ('<h2>FINANCIAL STATEMENTS AND SCHEDULES</h2>'
                '<h2>CONSOLIDATED BALANCE SHEETS</h2>'
                '<table id="balance"><tr><td>Total assets</td><td>100</td></tr></table>')
    path.write_text(path.read_text().replace('<p>Body of fifteen.</p>', '<p>Body of fifteen.</p>' + appendix))
    report = clean_report(path, form='10-K')
    mapping = map_table_section({'structure': {'source_xpath': "//*[@id='balance']",
        'html_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}, {}, report.sections,
        path.read_bytes(), accession='test')
    assert mapping['section_key'] == 'financial-statements-appendix'
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    assert audit_report(path, form='10-K', report=report, expected_section_ids=expected)['status'] == 'passed'


def test_quarterly_singular_signature_is_audited_independently(tmp_path):
    path = quarterly_without_toc(tmp_path, '')
    path.write_text(path.read_text().replace('</body>',
        '<h2>SIGNATURE</h2><p>Signed on behalf of the registrant.</p></body>'))
    report = clean_report(path, form='10-Q')
    expected = source_heading_expectations(path, form='10-Q')['expected_section_ids']
    assert 'signatures' in {row['section_id'] for row in report.sections}
    assert audit_report(path, form='10-Q', report=report, expected_section_ids=expected)['status'] == 'passed'
    stale = deepcopy(report)
    stale.sections = [row for row in stale.sections if row['section_id'] != 'signatures']
    stale.sections[-1]['offset_end'] = len(stale.full_text)
    assert 'unnumbered_body_boundary_missing' in checks(
        audit_report(path, form='10-Q', report=stale, expected_section_ids=expected))
