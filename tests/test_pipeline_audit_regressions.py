"""Regressions from the November 11 pipeline audit.

Each case reproduces a defect found by reading the code or by running the
pipeline on filings outside the development corpus.
"""
from sec_pipeline.report_cleaning import clean_report


def _source(tmp_path, content):
    path = tmp_path / 'filing.htm'
    path.write_text('<html><body>' + content + '</body></html>')
    return path


# --------------------------------------------------------------------------
# Contents rows that link only the title cell (bank cross-reference indexes).

_CROSS_REFERENCE_INDEX = (
    '<table>'
    '<tr><td>Part I – Financial information</td><td>Page</td></tr>'
    '<tr><td>Item 1.</td><td><a href="#statements">Financial Statements</a></td><td></td></tr>'
    '<tr><td>Consolidated statements of income</td><td>93</td></tr>'
    '<tr><td>Consolidated balance sheets</td><td>95</td></tr>'
    '</table>')
_CROSS_REFERENCE_BODY = (
    '<p>Management discusses the results of the quarter here.</p>'
    '<p>Net revenue increased because of higher markets activity.</p>'
    '<h2 id="statements">Consolidated statements of income</h2><p>Total net revenue 100</p>'
    '<p>Item 3. Quantitative and Qualitative Disclosures About Market Risk.</p><p>Refer to market risk.</p>'
    '<p>Item 4. Controls and Procedures.</p><p>Controls were evaluated.</p>')


def test_contents_row_linking_only_its_title_is_not_a_body_section(tmp_path):
    path = _source(tmp_path, _CROSS_REFERENCE_INDEX + _CROSS_REFERENCE_BODY)
    report = clean_report(path, form='10-Q')
    by_key = {row['section_id']: row for row in report.sections}
    # The discussion must not be labelled financial statements from a contents row.
    assert 'part-i-item-1' not in by_key
    first = report.sections[0]
    assert (first['section_type'], first['validation_status']) == ('unknown', 'unverified')
    assert 'Management discusses' in report.full_text[first['offset_start']:first['offset_end']]
    assert by_key['part-i-item-3']['section_type'] == 'market_risk'


def test_body_row_heading_with_its_own_anchor_remains_a_section(tmp_path):
    # A body heading laid out as a table row whose title links to its own row
    # is a permalink, not navigation.
    path = _source(tmp_path,
                   '<table><tr id="fs"><td>Item 1.</td><td><a href="#fs">Financial Statements</a></td></tr></table>'
                   '<p>Revenue was 100.</p>'
                   '<table><tr><td>Item 2.</td><td>Management’s Discussion and Analysis</td></tr></table>'
                   '<p>Results improved.</p>')
    report = clean_report(path, form='10-Q')
    by_key = {row['section_id']: row for row in report.sections}
    assert by_key['part-i-item-1']['section_type'] == 'financial_statements'
    assert by_key['part-i-item-2']['section_type'] == 'mda'


# --------------------------------------------------------------------------
# Queue state: a dry run reads sources only and must not rewrite an import.

from copy import deepcopy  # noqa: E402

from sec_pipeline import backfill  # noqa: E402
from sec_pipeline.processing import ParserRevisionConflict  # noqa: E402
from tests.test_backfill import Connection, queue  # noqa: E402,F401
from tests.test_backfill_progress import progressing_queue, successful_result  # noqa: E402,F401
from tests.test_backfill_v4 import source_queue  # noqa: E402,F401

_RECEIPT = {'mode': 'ingest', 'processing_set_id': '11111111-1111-1111-1111-111111111111',
            'validation_status': 'passed'}
_DESTINATION = {'host': 'db', 'port': 5432, 'dbname': 'sec_filings'}


def _ingested(source_queue):
    q = source_queue.queue
    for accession in q.accessions:
        q.state['filings'][accession].update(status='ingested', destination=dict(_DESTINATION),
                                             result=deepcopy(_RECEIPT), processing_versions={'tables': 'tables-v3'},
                                             manifest='data/raw/sec/' + accession + '/source_manifest.json')
    return q, source_queue.profile


def test_document_set_dry_run_keeps_an_ingested_entry_and_its_receipt(source_queue):
    q, profile = _ingested(source_queue)
    before = deepcopy(q.state['filings'])
    backfill.process_queue(q.path, q.state, None, q.root, tables_version='tables-v4', mode='dry-run',
                           emit=lambda event: None)
    for accession, entry in q.state['filings'].items():
        for field in ('status', 'result', 'destination', 'processing_versions', 'manifest'):
            assert entry[field] == before[accession][field], field
        # The fixture's dry run reports a review-required reconstruction.
        assert entry['dry_run']['status'] == 'validated_review_required'
        assert entry['dry_run']['result'] == profile.run_dry_run.return_value
    assert q.state['last_run']['verified_successes'] == 3
    assert q.state['last_run']['review_required_successes'] == 3
    assert q.state['last_run']['complete'] is True


def test_failed_dry_run_over_an_ingested_entry_is_recorded_without_losing_the_import(source_queue):
    q, profile = _ingested(source_queue)
    profile.run_dry_run.side_effect = ValueError('layout no longer accepted')
    backfill.process_queue(q.path, q.state, None, q.root, tables_version='tables-v4', mode='dry-run',
                           emit=lambda event: None)
    for entry in q.state['filings'].values():
        assert entry['status'] == 'ingested' and entry['result'] == _RECEIPT
        assert entry['dry_run']['status'] == 'failed' and entry['dry_run']['error_type'] == 'ValueError'
        assert 'error_type' not in entry
    assert q.state['last_run']['verified_successes'] == 0 and q.state['last_run']['complete'] is False
    # The failure is still dry-run work for a failure-only retry.
    profile.run_dry_run.reset_mock()
    assert backfill.process_queue(q.path, q.state, None, q.root, tables_version='tables-v4', mode='dry-run',
                                  retry_failed_only=True, emit=lambda event: None) == 3
    assert profile.run_dry_run.call_count == 3


def test_bounded_document_set_dry_runs_reach_every_pending_filing(source_queue):
    q, profile = source_queue.queue, source_queue.profile
    for _ in q.accessions:
        assert backfill.process_queue(q.path, q.state, None, q.root, tables_version='tables-v4', mode='dry-run',
                                      limit=1, emit=lambda event: None) == 1
    processed = [call.kwargs['manifest_path'].parent.name for call in profile.run_dry_run.call_args_list]
    assert processed == q.accessions
    assert all(entry['status'] == 'validated_review_required' for entry in q.state['filings'].values())


def test_stale_ingested_entry_keeps_its_history_when_a_dry_run_precedes_the_ingest(progressing_queue):
    q = progressing_queue
    accession = q.accessions[0]
    old = successful_result(q, accession)
    old['parser_revisions'] = {'facts': 'numeric-tag-boundaries-v2', 'reports': 'legacy-visible-layout-boundaries-v3'}
    old.update(mode='ingest', processing_set_id=_RECEIPT['processing_set_id'])
    q.state['filings'][accession].update(status='ingested', destination='db', result=deepcopy(old))
    backfill.process_queue(q.path, q.state, None, q.root, mode='dry-run', limit=1, emit=lambda event: None)
    entry = q.state['filings'][accession]
    assert entry['status'] == 'ingested' and entry['result'] == old
    assert entry['dry_run']['status'] == 'validated'
    q.ingest.side_effect = ParserRevisionConflict('older parser revision')
    backfill.process_queue(q.path, q.state, None, q.root, mode='ingest', connection=Connection(), destination='db',
                           limit=1, emit=lambda event: None)
    entry = q.state['filings'][accession]
    assert (entry['status'], entry['receipt_status']) == ('ingested', 'stale_receipt')
    assert entry['result']['processing_set_id'] == _RECEIPT['processing_set_id']


# --------------------------------------------------------------------------
# Co-registrant binding with the dated 2019/2020 DEI taxonomies.

import re  # noqa: E402

import pytest  # noqa: E402

from sec_pipeline.registrants import verify_document_binding  # noqa: E402
from tests.test_combined_registrants import subsidiary_archive  # noqa: E402
from tests.test_registrants import joint_fixture, refresh_hashes  # noqa: E402


@pytest.mark.parametrize('build', [subsidiary_archive, joint_fixture])
@pytest.mark.parametrize('taxonomy', [b'2019-01-31', b'2020-01-31', b'2021'])
def test_combined_filing_binds_registrants_under_dated_dei_namespaces(build, taxonomy):
    manifest, discovered, payloads = build()
    declared = {match for data in payloads.values()
                for match in re.findall(rb'https?://xbrl\.sec\.gov/dei/[0-9-]+', data)}
    assert declared
    for role in list(payloads):
        for namespace in declared:
            payloads[role] = payloads[role].replace(namespace, b'http://xbrl.sec.gov/dei/' + taxonomy)
    refresh_hashes(manifest, payloads)
    assert verify_document_binding(manifest, discovered, payloads)


def test_combined_filing_still_requires_a_dei_namespace():
    manifest, discovered, payloads = joint_fixture()
    for role in list(payloads):
        payloads[role] = re.sub(rb'https?://xbrl\.sec\.gov/dei/[0-9-]+', b'http://example.com/dei/2024', payloads[role])
    refresh_hashes(manifest, payloads)
    with pytest.raises(ValueError):
        verify_document_binding(manifest, discovered, payloads)



# --------------------------------------------------------------------------
# Section layouts found on filings outside the development corpus.

from uuid import UUID  # noqa: E402

from sec_pipeline.chunking import chunk_report  # noqa: E402
from sec_pipeline.report_validation import audit_report, source_heading_expectations  # noqa: E402

_ANNUAL = [('i1', 'I', 'Item 1.', 'Business'), ('i1a', 'I', 'Item 1A.', 'Risk Factors'),
           ('i1b', 'I', 'Item 1B.', 'Unresolved Staff Comments'), ('i2', 'I', 'Item 2.', 'Properties'),
           ('i3', 'I', 'Item 3.', 'Legal Proceedings'), ('i4', 'I', 'Item 4.', 'Mine Safety Disclosures'),
           ('i5', 'II', 'Item 5.', "Market for Registrant's Common Equity"), ('i6', 'II', 'Item 6.', '[Reserved]'),
           ('i7', 'II', 'Item 7.', "Management's Discussion and Analysis"),
           ('i7a', 'II', 'Item 7A.', 'Quantitative and Qualitative Disclosures About Market Risk'),
           ('i8', 'II', 'Item 8.', 'Financial Statements and Supplementary Data'),
           ('i9', 'II', 'Item 9.', 'Changes in and Disagreements with Accountants'),
           ('i9a', 'II', 'Item 9A.', 'Controls and Procedures'), ('i9b', 'II', 'Item 9B.', 'Other Information'),
           ('i10', 'III', 'Item 10.', 'Directors, Executive Officers and Corporate Governance'),
           ('i11', 'III', 'Item 11.', 'Executive Compensation'), ('i12', 'III', 'Item 12.', 'Security Ownership'),
           ('i13', 'III', 'Item 13.', 'Certain Relationships and Related Transactions'),
           ('i14', 'III', 'Item 14.', 'Principal Accountant Fees and Services'),
           ('i15', 'IV', 'Item 15.', 'Exhibits and Financial Statement Schedules')]
_QUARTERLY = [('q1', 'I', 'Item 1.', 'Financial Statements'), ('q2', 'I', 'Item 2.', "Management's Discussion and Analysis"),
              ('q3', 'I', 'Item 3.', 'Quantitative and Qualitative Disclosures About Market Risk'),
              ('q4', 'I', 'Item 4.', 'Controls and Procedures'), ('r1', 'II', 'Item 1.', 'Legal Proceedings'),
              ('r2', 'II', 'Item 2.', 'Unregistered Sales of Equity Securities and Use of Proceeds'),
              ('r6', 'II', 'Item 6.', 'Exhibits')]
_PARTS = {'10-K': {'I': 'PART I', 'II': 'PART II', 'III': 'PART III', 'IV': 'PART IV'},
          '10-Q': {'I': 'PART I. FINANCIAL INFORMATION', 'II': 'PART II. OTHER INFORMATION'}}


def _filing(form='10-K', *, linked=True, not_applicable=(), body_parts=True, after_part=None, titles=None,
            before_heading=None, heading=None):
    """A contents table followed by the body; every option mirrors one observed layout."""
    rows, blocks, seen, page = [], [], set(), 3
    for key, part, number, title in (_ANNUAL if form == '10-K' else _QUARTERLY):
        title = (titles or {}).get(key, title)
        if part not in seen:
            seen.add(part)
            rows.append(f'<tr><td colspan="3"><b>{_PARTS[form][part]}</b></td></tr>')
            if body_parts:
                blocks.append(f'<p><b>{_PARTS[form][part]}</b></p>')
            blocks.append((after_part or {}).get(part, ''))
        if key in not_applicable:
            rows.append(f'<tr><td>{number}</td><td>{title}</td><td>N/A</td></tr>')
            continue
        cell = (lambda text: f'<a href="#{key}">{text}</a>') if linked else (lambda text: text)
        rows.append(f'<tr><td>{cell(number)}</td><td>{cell(title)}</td><td>{cell(page)}</td></tr>')
        page += 4
        blocks.append((before_heading or {}).get(key, ''))
        blocks.append((heading or {}).get(key) or f'<p id="{key}"><b>{number} {title}</b></p>')
        blocks.append(f'<p>Body text of {number} {title}, with several words of prose.</p>')
    return ('<p>FORM ' + form + '</p><p>Example Corp</p><p><b>TABLE OF CONTENTS</b></p><table>' + ''.join(rows)
            + '</table>' + ''.join(blocks) + '<p><b>SIGNATURES</b></p><p>Signed by the registrant.</p>')


def _audited(tmp_path, content, form='10-K'):
    path = _source(tmp_path, content)
    report = clean_report(path, form=form)
    chunks = chunk_report(filing_id=UUID(int=1), report_id=UUID(int=2), full_text=report.full_text,
                          sections=report.sections, chunking_version='paragraph-v2')
    expected = source_heading_expectations(path, form=form)
    result = audit_report(path, form=form, report=report, chunks=chunks,
                          expected_section_ids=expected['expected_section_ids'])
    return report, expected, result


def _checks(result):
    return {issue['check'] for issue in result['issues']}


def _ids(report):
    return [row['section_id'] for row in report.sections]


def test_baseline_layout_of_these_fixtures_passes(tmp_path):
    for form in ('10-K', '10-Q'):
        report, expected, result = _audited(tmp_path, _filing(form), form)
        assert (expected['status'], result['status']) == ('derived_from_original_toc', 'passed'), result['issues']


def test_contents_rows_marked_not_applicable_are_not_expected_in_the_body(tmp_path):
    # Pfizer 10-K 0000078003-26-000026 lists Items 1B, 4 and 9C with "N/A" as their page.
    report, expected, result = _audited(tmp_path, _filing(not_applicable={'i1b', 'i4'}))
    assert expected['status'] == 'derived_from_original_toc'
    assert [row['title'] for row in expected['declared_not_applicable']] == [
        'Item 1B. Unresolved Staff Comments N/A', 'Item 4. Mine Safety Disclosures N/A']
    assert 'part-i-item-1b' not in expected['expected_section_ids']
    assert result['status'] == 'passed', result['issues']
    assert 'part-i-item-1b' not in _ids(report) and 'part-i-item-4' not in _ids(report)


def test_unlinked_contents_row_without_a_not_applicable_marker_still_blocks_the_contents_table(tmp_path):
    content = _filing().replace('<td><a href="#i2">Item 2.</a></td><td><a href="#i2">Properties</a></td><td><a href="#i2">15</a></td>',
                                '<td>Item 2.</td><td>Properties</td><td>15</td>')
    assert 'href="#i2"' not in content
    report, expected, result = _audited(tmp_path, content)
    assert expected['status'] != 'derived_from_original_toc'


def test_unlinked_paged_contents_rows_are_not_read_as_body_headings(tmp_path):
    # Exxon Mobil 10-Q 0000034088-21-000024: a contents table with pages and no links.
    for form in ('10-K', '10-Q'):
        report, expected, result = _audited(tmp_path, _filing(form, linked=False), form)
        assert expected['status'] == 'derived_from_original_body'
        assert all('/table' not in heading['source_xpath'] for heading in expected['body_headings'])
        assert result['status'] == 'passed', result['issues']


def test_part_listed_only_in_the_contents_is_not_required_in_the_body(tmp_path):
    # Duke Energy 0001326160-26-000014 lists Parts in its contents and prints none in the body.
    for form in ('10-K', '10-Q'):
        report, expected, result = _audited(tmp_path, _filing(form, body_parts=False), form)
        assert 'part-i' in expected['expected_section_ids'] and 'part-i' not in _ids(report)
        assert result['status'] == 'passed', result['issues']


def test_body_part_heading_missing_from_the_sections_still_fails(tmp_path):
    path = _source(tmp_path, _filing())
    report = clean_report(path, form='10-K')
    index = _ids(report).index('part-ii')
    report.sections[index - 1]['offset_end'] = report.sections[index]['offset_end']
    del report.sections[index]
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    result = audit_report(path, form='10-K', report=report, expected_section_ids=expected)
    assert result['status'] == 'failed'
    assert {'missing': ['part-ii'], 'unexpected': []} in [
        {key: issue[key] for key in ('missing', 'unexpected')} for issue in result['issues']
        if issue['check'] == 'section_inventory_mismatch']


# Four short labels in deeply nested markup: more than 30 elements, under 100 visible characters.
_RUNNING_HEADER = ''.join(
    '<div><div><span><span><span><span><span><a href="#top">' + label + '</a></span></span></span></span></span></div></div>'
    for label in ("MANAGEMENT'S DISCUSSION AND ANALYSIS", 'Table of Contents', 'FINANCIAL SUMMARY', 'Index to Notes'))


def test_contents_link_to_a_page_top_above_a_running_header_is_accepted(tmp_path):
    # Target 10-Q 0000027419-26-000042: the link target is a page anchor and the
    # heading follows a markup-heavy running header (31 elements later).
    content = _filing('10-Q', heading={'q2': "<p><b>Item 2. Management's Discussion and Analysis</b></p>"},
                      before_heading={'q2': '<div id="q2"></div>' + _RUNNING_HEADER})
    report, expected, result = _audited(tmp_path, content, '10-Q')
    assert result['status'] == 'passed', result['issues']


def test_contents_link_separated_from_its_heading_by_other_text_still_fails(tmp_path):
    prose = '<p>' + 'Unrelated disclosure that belongs to the previous Item. ' * 8 + '</p>'
    content = _filing('10-Q', heading={'q2': "<p><b>Item 2. Management's Discussion and Analysis</b></p>"},
                      before_heading={'q2': '<div id="q2"></div>' + prose * 40})
    report, expected, result = _audited(tmp_path, content, '10-Q')
    assert 'section_navigation_target_mismatch' in _checks(result)


def test_first_part_heading_followed_by_an_introductory_note_is_a_section(tmp_path):
    # Walmart 10-K 0000104169-26-000055 prints a forward-looking-statements note
    # between "PART I" and "Item 1. Business".
    note = '<p><b>Cautionary Statement Regarding Forward-Looking Statements</b></p><p>This report contains statements.</p>'
    for form in ('10-K', '10-Q'):
        report, expected, result = _audited(tmp_path, _filing(form, after_part={'I': note}), form)
        by_key = {row['section_id']: row for row in report.sections}
        part = by_key['part-i']
        assert 'Cautionary Statement' in report.full_text[part['offset_start']:part['offset_end']]
        assert 'Cautionary Statement' not in report.full_text[:by_key['front-matter']['offset_end']]
        assert result['status'] == 'passed', result['issues']


def test_contents_part_line_never_becomes_the_first_body_part(tmp_path):
    # Without links the contents rows lie between its "PART I" line and Item 1.
    report, expected, result = _audited(tmp_path, _filing(linked=False, body_parts=False))
    assert 'part-i' not in _ids(report)
    front = report.sections[0]
    assert front['section_id'] == 'front-matter' and 'TABLE OF CONTENTS' in report.full_text[:front['offset_end']]


@pytest.mark.parametrize('form,key,title,section', [
    ('10-K', 'i6', 'Intentionally Omitted', 'part-ii-item-6'),                       # Coca-Cola 0000021344-21-000008
    ('10-K', 'i6', '[Removed and Reserved]', 'part-ii-item-6'),
    ('10-K', 'i6', 'Selected Consolidated Financial Data', 'part-ii-item-6'),
    ('10-K', 'i8', 'Consolidated Financial Statements and Supplementary Data', 'part-ii-item-8'),
    ('10-K', 'i7a', 'Quantitative and Qualitative Disclosure About Market Risk', 'part-ii-item-7a'),
    ('10-K', 'i5', "Market Price of and Dividends on the Registrant's Common Equity", 'part-ii-item-5'),
    ('10-K', 'i5', 'Market for Our Ordinary Shares and Related Matters', 'part-ii-item-5'),
    ('10-K', 'i1', 'Description of Business', 'part-i-item-1'),
    ('10-K', 'i4', 'Mine Safety Disclosure', 'part-i-item-4'),
    ('10-K', 'i9a', 'Controls &amp; Procedures', 'part-ii-item-9a'),
    ('10-K', 'i15', 'Financial Statement Schedules and Exhibits', 'part-iv-item-15'),
    ('10-Q', 'q1', 'Interim Condensed Consolidated Financial Statements', 'part-i-item-1'),
    ('10-Q', 'q3', 'Quantitative and Qualitative Disclosure of Market Risk', 'part-i-item-3'),
])
def test_ordinary_item_title_wordings_are_recognized(tmp_path, form, key, title, section):
    report, expected, result = _audited(tmp_path, _filing(form, titles={key: title}), form)
    assert section in _ids(report)
    assert result['status'] == 'passed', result['issues']


def test_part_heading_with_a_colon_is_recognized(tmp_path):
    content = _filing('10-Q').replace('PART I. FINANCIAL INFORMATION', 'PART I: FINANCIAL INFORMATION').replace(
        'PART II. OTHER INFORMATION', 'PART II: OTHER INFORMATION')
    report, expected, result = _audited(tmp_path, content, '10-Q')
    assert {'part-i', 'part-ii'} <= set(_ids(report))
    assert result['status'] == 'passed', result['issues']


def test_center_is_a_block_boundary_under_report_v4(tmp_path):
    path = _source(tmp_path, '<div>First sentence ends here.<center>ITEM 1. BUSINESS</center>Second sentence starts here.</div>'
                             '<div>More text.<center>ITEM 2. PROPERTIES</center>Offices.</div>')
    report = clean_report(path, form='10-K', report_version='report-v4')
    assert 'here.ITEM' not in report.full_text and 'BUSINESSSecond' not in report.full_text
    assert {'part-i-item-1', 'part-i-item-2'} <= set(_ids(report))


@pytest.mark.parametrize('version', [None, 'report-v1', 'report-v2', 'report-v3'])
def test_earlier_report_versions_keep_their_block_boundaries(tmp_path, version):
    # Stored reports are immutable: replaying an earlier version must render
    # the same text, so only report-v4 breaks around the added block elements.
    path = _source(tmp_path, '<div>First paragraph.<center>Second paragraph.</center>Third paragraph.</div>'
                             '<div>Intro <figure>Figure text</figure> outro.</div>')
    earlier = clean_report(path, form='10-K', report_version=version).full_text
    audited = clean_report(path, form='10-K', report_version='report-v4').full_text
    assert 'First paragraph.Second paragraph.Third paragraph.' in earlier
    assert 'Intro Figure text outro.' in earlier
    assert 'First paragraph.\n\nSecond paragraph.\n\nThird paragraph.' in audited
    assert 'Intro\n\nFigure text\n\noutro.' in audited


# --------------------------------------------------------------------------
# A heading block that also holds the index table of its own section.

import hashlib  # noqa: E402

from sec_pipeline.table_sections import map_table_section  # noqa: E402

_INDEXED_ITEM_8 = (
    '<div id="i8"><span><b>Item 8. Financial Statements and Supplementary Data</b></span>'
    '<table><tr><td></td><td>Page</td></tr>'
    '<tr><td><a href="#bs">Consolidated Balance Sheets</a></td><td><a href="#bs">52</a></td></tr></table></div>'
    '<p id="bs"><b>CONSOLIDATED BALANCE SHEETS</b></p>'
    '<table id="balance"><tr><td>Total assets</td><td>100</td></tr></table>')


def _statement_mapping(tmp_path, content):
    path = _source(tmp_path, content)
    report = clean_report(path, form='10-K')
    table = {'structure': {'source_xpath': "//*[@id='balance']",
                           'html_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}
    return report, map_table_section(table, {}, report.sections, path.read_bytes(), accession='test')


def test_heading_block_with_its_own_index_table_is_a_section_boundary(tmp_path):
    # UnitedHealth 10-K 0000731766-26-000062: the Item 8 heading and the linked
    # index of statements share one block, and the statements follow it.
    report, mapping = _statement_mapping(tmp_path, _filing(heading={'i8': _INDEXED_ITEM_8}))
    assert mapping['mapping_status'] == 'mapped', mapping['evidence']
    assert mapping['section_key'] == 'part-ii-item-8'
    assert mapping['evidence']['rejected_boundaries'] == []


def test_section_located_in_a_linked_contents_row_is_still_rejected(tmp_path):
    report, mapping = _statement_mapping(tmp_path, _filing(heading={'i8': _INDEXED_ITEM_8}))
    row = next(section for section in report.sections if section['section_id'] == 'part-ii-item-9')
    path = tmp_path / 'filing.htm'
    row['source_locator'] = {'source_file': path.name, 'xpath': '/html/body/table/tr[14]'}
    table = {'structure': {'source_xpath': "//*[@id='balance']",
                           'html_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}
    again = map_table_section(table, {}, report.sections, path.read_bytes(), accession='test')
    assert [boundary['reason'] for boundary in again['evidence']['rejected_boundaries']] == ['toc_hyperlink']


# --------------------------------------------------------------------------
# A document in another encoding must not be stored with replacement characters.

from sec_pipeline.report_cleaning import ReportEncodingError  # noqa: E402

_ENCODED_BODY = ('<html><head><meta charset="{charset}"></head><body><p>Item 1. Business</p>'
                 '<p>Revenue rose 5 % — see “Risk Factors”. Costs were €10 million.</p></body></html>')


def test_utf8_document_with_non_ascii_text_is_cleaned(tmp_path):
    path = tmp_path / 'filing.htm'
    path.write_bytes(_ENCODED_BODY.format(charset='utf-8').encode('utf-8'))
    report = clean_report(path, form='10-K')
    assert '— see “Risk Factors”' in report.full_text and '�' not in report.full_text


def test_windows_1252_document_is_rejected_instead_of_storing_replacement_characters(tmp_path):
    path = tmp_path / 'filing.htm'
    path.write_bytes(_ENCODED_BODY.format(charset='windows-1252').encode('cp1252'))
    with pytest.raises(ReportEncodingError):
        clean_report(path, form='10-K')


def test_replacement_character_present_in_the_source_is_kept(tmp_path):
    for written in ('�', '&#65533;', '&#xFFFD;'):
        path = tmp_path / 'filing.htm'
        path.write_bytes(f'<html><body><p>Item 1. Business</p><p>Unknown symbol {written} here.</p></body></html>'.encode('utf-8'))
        assert 'Unknown symbol � here.' in clean_report(path, form='10-K').full_text


# --------------------------------------------------------------------------
# Transformation Registry 3 date names still used by 2021 filings.

from sec_pipeline.inline_value_transforms import (  # noqa: E402
    UnsupportedInlineTransform, nonnumeric_value, validate_transform)

_REGISTRY_3 = '{http://www.xbrl.org/inlineXBRL/transformation/2015-02-26}'


@pytest.mark.parametrize('name,shown,expected', [
    ('datemonthdayyear', '1/1/2023', '2023-01-01'),          # UnitedHealth 0000731766-21-000013
    ('datemonthdayyear', '12/31/2037', '2037-12-31'),
    ('datemonthyearen', 'January 2023', '2023-01'),          # Prologis 0001564590-21-020897
    ('datedaymonthen', '31 December', '--12-31'),
    ('datedaymonthyearen', '31 March 2026', '2026-03-31'),
    ('datemonthdayyearen', 'June 30, 2026', '2026-06-30'),   # already supported
])
def test_registry_3_date_names_use_the_same_grammar_as_registry_4(name, shown, expected):
    validate_transform(_REGISTRY_3 + name, 'nonnumeric')
    assert nonnumeric_value(shown, _REGISTRY_3 + name) == expected


def test_registry_3_dates_still_reject_impossible_or_unknown_input():
    with pytest.raises(UnsupportedInlineTransform):
        nonnumeric_value('13/45/2023', _REGISTRY_3 + 'datemonthdayyear')
    with pytest.raises(UnsupportedInlineTransform):
        validate_transform(_REGISTRY_3 + 'dateyearmonthdaycjk', 'nonnumeric')
    with pytest.raises(UnsupportedInlineTransform):
        validate_transform(_REGISTRY_3 + 'datemonthdayyear', 'numeric')


# --------------------------------------------------------------------------
# Statement layouts found on filings outside the development corpus.
# A minimal three-statement Inline XBRL filing and its instance.

from uuid import uuid5, NAMESPACE_URL  # noqa: E402

from sec_pipeline import tables as table_module  # noqa: E402
from sec_pipeline.tables import extract_tables  # noqa: E402

_CASH = 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
_NAMESPACES = ('xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:xbrli="http://www.xbrl.org/2003/instance" '
               'xmlns:us-gaap="http://fasb.org/us-gaap/2025" xmlns:dei="http://xbrl.sec.gov/dei/2025" '
               'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
               'xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12" '
               'xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink"')


class _Statements:
    """Income statement, balance sheet and cash flow with caller-chosen headers and periods."""

    def __init__(self, *, durations, instants, opening, closing=None, duration_header, instant_header,
                 opening_label='Cash at beginning of period', closing_label='Cash at end of period', dei=None):
        self.durations, self.instants, self.opening = durations, instants, opening
        self.closing = closing or instants
        self.duration_header, self.instant_header = duration_header, instant_header
        self.opening_label, self.closing_label, self.dei = opening_label, closing_label, dei or {}
        self.contexts, self.facts = {}, []

    def _context(self, period):
        key = 'c' + ''.join(period if isinstance(period, tuple) else (period,)).replace('-', '')
        self.contexts[key] = period
        return key

    def _row(self, label, concept, periods, shown, sign=False):
        cells = ''
        for period, value in zip(periods, shown):
            identifier = f'f{len(self.facts) + 1}'
            context = self._context(period)
            self.facts.append((identifier, concept, context, str(int(value) * 10 ** 6 * (-1 if sign else 1))))
            cells += (f'<td><ix:nonFraction id="{identifier}" name="us-gaap:{concept}" contextRef="{context}" unitRef="usd" '
                      f'decimals="-6" scale="6" format="ixt:num-dot-decimal"{" sign=" + chr(34) + "-" + chr(34) if sign else ""}>'
                      f'{value}</ix:nonFraction></td>')
        return f'<tr><td>{label}</td>{cells}</tr>'

    @staticmethod
    def _header(rows):
        return ''.join('<tr>' + ''.join(f'<td>{cell}</td>' for cell in row) + '</tr>' for row in rows)

    def write(self, directory):
        d, i = self.durations, self.instants
        income = (self._row('Total revenues', 'Revenues', d, ('1000', '900')) + self._row('Cost of sales', 'CostOfRevenue', d, ('600', '550'))
                  + self._row('Operating income', 'OperatingIncomeLoss', d, ('400', '350'))
                  + self._row('Provision for income taxes', 'IncomeTaxExpenseBenefit', d, ('100', '90'))
                  + self._row('Net income', 'NetIncomeLoss', d, ('300', '260')))
        balance = (self._row('Cash and cash equivalents', 'CashAndCashEquivalentsAtCarryingValue', i, ('500', '450'))
                   + self._row('Total assets', 'Assets', i, ('2000', '1800')) + self._row('Total liabilities', 'Liabilities', i, ('1200', '1100'))
                   + self._row("Total stockholders' equity", 'StockholdersEquity', i, ('800', '700'))
                   + self._row("Total liabilities and stockholders' equity", 'LiabilitiesAndStockholdersEquity', i, ('2000', '1800')))
        cash = (self._row('Net cash provided by operating activities', 'NetCashProvidedByUsedInOperatingActivities', d, ('350', '300'))
                + self._row('Net cash used in investing activities', 'NetCashProvidedByUsedInInvestingActivities', d, ('200', '150'), sign=True)
                + self._row('Net cash used in financing activities', 'NetCashProvidedByUsedInFinancingActivities', d, ('100', '100'), sign=True)
                + self._row('Net increase in cash', _CASH + 'PeriodIncreaseDecreaseIncludingExchangeRateEffect', d, ('50', '50'))
                + self._row(self.opening_label, _CASH, self.opening, ('450', '400'))
                + self._row(self.closing_label, _CASH, self.closing, ('500', '450')))
        body = ''.join(f'<div><p><b>{title}</b></p><p>(In millions)</p><table>{self._header(header)}{rows}</table></div>'
                       for title, header, rows in (('CONSOLIDATED STATEMENTS OF INCOME', self.duration_header, income),
                                                   ('CONSOLIDATED BALANCE SHEETS', self.instant_header, balance),
                                                   ('CONSOLIDATED STATEMENTS OF CASH FLOWS', self.duration_header, cash)))
        document = self._context(self.durations[0])
        contexts = ''.join(
            f'<xbrli:context id="{key}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001'
            '</xbrli:identifier></xbrli:entity><xbrli:period>'
            + (f'<xbrli:startDate>{period[0]}</xbrli:startDate><xbrli:endDate>{period[1]}</xbrli:endDate>'
               if isinstance(period, tuple) else f'<xbrli:instant>{period}</xbrli:instant>')
            + '</xbrli:period></xbrli:context>' for key, period in self.contexts.items())
        units = '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
        hidden = ''.join(f'<ix:nonNumeric id="d{n}" name="dei:{name}" contextRef="{document}">{value}</ix:nonNumeric>'
                         for n, (name, value) in enumerate(self.dei.items()))
        html_path, xml_path = directory / 'co.htm', directory / 'co_htm.xml'
        html_path.write_text(
            f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" {_NAMESPACES}><head><title>t</title></head>'
            f'<body><div style="display:none"><ix:header><ix:hidden>{hidden}</ix:hidden><ix:resources>{contexts}{units}</ix:resources>'
            f'</ix:header></div>{body}</body></html>', encoding='utf-8')
        xml_path.write_text(
            f'<?xml version="1.0" encoding="utf-8"?><xbrli:xbrl {_NAMESPACES}><link:schemaRef xlink:type="simple" xlink:href="co.xsd"/>'
            + contexts + units
            + ''.join(f'<dei:{name} id="d{n}" contextRef="{document}">{value}</dei:{name}>' for n, (name, value) in enumerate(self.dei.items()))
            + ''.join(f'<us-gaap:{concept} id="{identifier}" contextRef="{context}" unitRef="usd" decimals="-6">{raw}</us-gaap:{concept}>'
                      for identifier, concept, context, raw in self.facts)
            + '</xbrli:xbrl>', encoding='utf-8')
        return html_path, xml_path


def _unresolved(tmp_path, **options):
    """Reasons of every unresolved cell, and the periods bound to the value columns."""
    html_path, xml_path = _Statements(**options).write(tmp_path)
    filing = uuid5(NAMESPACE_URL, 'audit-regression')
    try:
        tables = extract_tables(html_path, xml_path, filing_id=filing, html_document_id=uuid5(filing, 'html'),
                                xml_document_id=uuid5(filing, 'xml'), facts_version='facts-v1', tables_version='tables-v2')
    except table_module.TableExtractionError as error:
        return [str(error)], {}
    cells = [cell for table in tables for row in table.structure['rows'] for cell in row]
    periods = {table.statement_type: sorted({json.dumps(cell['column_period'], sort_keys=True) for row in table.structure['rows']
                                             for cell in row if cell.get('fact_references')}) for table in tables}
    return sorted({cell['unresolved_reason'] for cell in cells if cell['classification'] == 'unresolved'}), periods


import json  # noqa: E402

_CALENDAR = dict(durations=(('2025-01-01', '2025-12-31'), ('2024-01-01', '2024-12-31')), instants=('2025-12-31', '2024-12-31'),
                 opening=('2024-12-31', '2023-12-31'),
                 duration_header=[['', 'Years Ended December 31,'], ['', '2025', '2024']],
                 instant_header=[['', 'December 31,'], ['', '2025', '2024']])


def test_statement_fixture_resolves_in_its_plain_calendar_form(tmp_path):
    assert _unresolved(tmp_path, **_CALENDAR)[0] == []


# Coca-Cola 0001628280-26-067844 and Pfizer 0000078003-26-000127: a calendar fiscal
# year whose quarters close on the Friday or Sunday nearest the quarter end.
_WEEKDAY_QUARTERS = dict(
    durations=(('2026-01-01', '2026-10-02'), ('2025-01-01', '2025-09-26')), instants=('2026-10-02', '2025-12-31'),
    opening=('2025-12-31', '2024-12-31'), closing=('2026-10-02', '2025-09-26'),
    duration_header=[['', 'Nine Months Ended'], ['', 'October 2, 2026', 'September 26, 2025']],
    instant_header=[['', 'October 2, 2026', 'December 31, 2025']])


def test_year_to_date_period_ending_near_a_month_end_is_bound_to_its_tagged_dates(tmp_path):
    reasons, periods = _unresolved(tmp_path, **_WEEKDAY_QUARTERS)
    assert reasons == []
    assert periods['income_statement'] == ['{"endDate": "2025-09-26", "startDate": "2025-01-01"}',
                                           '{"endDate": "2026-10-02", "startDate": "2026-01-01"}']


def test_period_that_starts_a_day_early_has_no_reported_balance_and_stays_unresolved(tmp_path):
    # The documented NetApp defect: the comparative period starts one day early.
    early = dict(_WEEKDAY_QUARTERS, durations=(('2026-01-01', '2026-10-02'), ('2024-12-31', '2025-09-26')))
    reasons, periods = _unresolved(tmp_path, **early)
    assert 'Fact period differs from column heading' in reasons
    assert '{"endDate": "2025-09-26", "startDate": "2024-12-31"}' not in periods['income_statement']


def test_period_far_from_the_printed_length_stays_unresolved(tmp_path):
    wrong = dict(_WEEKDAY_QUARTERS, durations=(('2026-01-01', '2026-10-02'), ('2025-04-01', '2025-09-26')),
                 opening=('2025-12-31', '2025-03-31'))
    reasons, periods = _unresolved(tmp_path, **wrong)
    assert 'Fact period differs from column heading' in reasons
    assert '{"endDate": "2025-09-26", "startDate": "2025-04-01"}' not in periods['income_statement']


# Target 0000027419-26-000016: fiscal 2025 ends January 31, 2026 and the columns say "2025".
_RETAIL_YEARS = dict(
    durations=(('2025-02-02', '2026-01-31'), ('2024-02-04', '2025-02-01')), instants=('2026-01-31', '2025-02-01'),
    opening=('2025-02-01', '2024-02-03'),
    duration_header=[['(millions)', '2025', '2024']], instant_header=[['(millions)', 'January 31, 2026', 'February 1, 2025']])


def test_fiscal_year_named_for_its_first_calendar_year_needs_the_filings_own_declaration(tmp_path):
    declared = dict(_RETAIL_YEARS, dei={'DocumentFiscalYearFocus': '2025', 'DocumentPeriodEndDate': '2026-01-31'})
    reasons, periods = _unresolved(tmp_path, **declared)
    assert reasons == []
    assert periods['income_statement'] == ['{"endDate": "2025-02-01", "startDate": "2024-02-04"}',
                                           '{"endDate": "2026-01-31", "startDate": "2025-02-02"}']
    assert 'Unresolved column period' in _unresolved(tmp_path, **_RETAIL_YEARS)[0]
    same_year = dict(_RETAIL_YEARS, dei={'DocumentFiscalYearFocus': '2026', 'DocumentPeriodEndDate': '2026-01-31'})
    assert 'Unresolved column period' in _unresolved(tmp_path, **same_year)[0]


# AT&T 0000732717-21-000030 and Pfizer 0000078003-21-000065: the duration caption is
# marked up over the first value column only.
def test_caption_split_over_two_cells_applies_to_every_bare_year_column(tmp_path):
    split = dict(durations=(('2021-01-01', '2021-03-31'), ('2020-01-01', '2020-03-31')), instants=('2021-03-31', '2020-12-31'),
                 opening=('2020-12-31', '2019-12-31'), closing=('2021-03-31', '2020-03-31'),
                 duration_header=[['', 'Three months ended'], ['', 'March 31,'], ['', '2021', '2020']],
                 instant_header=[['', 'March 31, 2021', 'December 31, 2020']])
    assert _unresolved(tmp_path, **split)[0] == []
    two_captions = dict(split, duration_header=[['', 'Three months ended', 'Nine months ended'], ['', 'March 31,'], ['', '2021', '2020']])
    assert _unresolved(tmp_path, **two_captions)[0] != []


def test_duration_caption_over_the_first_dated_column_applies_to_the_other(tmp_path):
    dated = dict(durations=(('2021-01-01', '2021-04-04'), ('2020-01-01', '2020-03-29')), instants=('2021-04-04', '2020-12-31'),
                 opening=('2020-12-31', '2019-12-31'), closing=('2021-04-04', '2020-03-29'),
                 duration_header=[['', 'Three Months Ended'], ['(MILLIONS)', 'April 4, 2021', 'March 29, 2020']],
                 instant_header=[['', 'April 4, 2021', 'December 31, 2020']])
    reasons, periods = _unresolved(tmp_path, **dated)
    assert reasons == []
    assert '{"endDate": "2020-03-29", "startDate": "2020-01-01"}' in periods['income_statement']
    # The caption supplies wording only; a column tagged with another period is not accepted.
    other = dict(dated, durations=(('2021-01-01', '2021-04-04'), ('2019-07-01', '2020-03-29')), opening=('2020-12-31', '2019-06-30'))
    assert _unresolved(tmp_path, **other)[0] != []


# Bank of America 0000070858-26-000157 and Progressive 0000080661-26-000379.
@pytest.mark.parametrize('opening_label,closing_label', [
    ('Cash and cash equivalents at January 1', 'Cash and cash equivalents at December 31'),
    ('Cash, cash equivalents and restricted cash – January 1', 'Cash, cash equivalents and restricted cash – December 31'),
    ('Cash at beginning of fiscal year', 'Cash at end of fiscal year'),
])
def test_cash_balances_labelled_by_date_or_fiscal_year_are_bound(tmp_path, opening_label, closing_label):
    assert _unresolved(tmp_path, **_CALENDAR, opening_label=opening_label, closing_label=closing_label)[0] == []


def test_dated_cash_label_does_not_hide_a_misdated_opening_balance(tmp_path):
    # An opening balance dated the first day of the period, not the day before, is still a mismatch.
    reasons, _ = _unresolved(tmp_path, **dict(_CALENDAR, opening=('2025-01-01', '2024-01-01')),
                             opening_label='Cash and cash equivalents at January 1',
                             closing_label='Cash and cash equivalents at December 31')
    assert reasons != []


def test_cash_label_naming_a_day_inside_the_period_is_not_an_endpoint(tmp_path):
    reasons, _ = _unresolved(tmp_path, **_CALENDAR, opening_label='Cash and cash equivalents at June 30',
                             closing_label='Cash and cash equivalents at December 31')
    assert reasons != []


def test_date_stacked_above_its_year_in_one_cell_is_read(tmp_path):
    # Bank of America 0000070858-26-000548 prints "September 30" above "2026" in one cell.
    stacked = dict(_CALENDAR, instant_header=[['', 'December 31<br/>2025', 'December 31<br/>2024']])
    reasons, periods = _unresolved(tmp_path, **stacked)
    assert reasons == []
    assert periods['balance_sheet'] == ['{"instant": "2024-12-31"}', '{"instant": "2025-12-31"}']


def test_access_block_during_a_dry_run_leaves_an_ingested_entry_ingested(source_queue, monkeypatch):
    from sec_pipeline import acquisition
    q, profile = _ingested(source_queue)
    newest = q.accessions[0]
    (q.root / 'data' / 'raw' / 'sec' / newest / 'source_manifest.json').unlink()

    def blocked(client, filing, root):
        raise acquisition.AccessBlocked('SEC returned HTTP 403; session stopped.')

    monkeypatch.setattr(acquisition, 'download_filing', blocked)
    with pytest.raises(acquisition.AccessBlocked):
        backfill.process_queue(q.path, q.state, object(), q.root, tables_version='tables-v4', mode='dry-run',
                               emit=lambda event: None)
    entry = q.state['filings'][newest]
    assert entry['status'] == 'ingested' and entry['result'] == _RECEIPT
    assert entry['dry_run']['status'] == 'access_blocked'


# --------------------------------------------------------------------------
# Discovery must show what it could not queue instead of reading as complete.

from sec_pipeline.acquisition import discover_filings  # noqa: E402

_ROWS = [  # accession, form, filed, period, primary document
    ('0000000001-21-000001', '10-K', '2022-02-15', '2021-12-31', 'a.htm'),
    ('0000000001-22-000002', '10-Q', '2022-05-05', '2022-03-31', 'b.htm'),
    ('0000000001-22-000003', '10-KT', '2022-11-10', '2022-09-30', 'c.htm'),
    ('0000000001-22-000004', '10-K/A', '2022-11-20', '2021-12-31', 'd.htm'),
    ('0000000001-22-000005', 'NT 10-Q', '2023-02-10', '2022-12-31', 'e.htm'),
    ('0000000001-23-000006', '10-Q', '2023-02-20', '2022-12-31', 'f.htm'),
    ('0000000001-23-000007', '10-Q', '2023-05-05', '2023-03-31', 'g.txt'),       # not an HTML primary document
    ('0000000001-23-000008', '8-K', '2023-05-06', '2023-05-06', 'h.htm'),
]


class _Submissions:
    def __init__(self, rows):
        names = ('accessionNumber', 'form', 'filingDate', 'reportDate', 'primaryDocument')
        self.document = {'cik': '1', 'name': 'Example Co',
                         'filings': {'recent': {name: [row[index] for row in rows] for index, name in enumerate(names)}, 'files': []}}

    def get_json(self, url):
        return self.document


def test_discovery_returns_supported_filings_and_records_what_it_excludes():
    found = discover_filings(_Submissions(_ROWS), '1', '2021-01-01', '2023-12-31')
    assert [(row['form'], row['reporting_period_end']) for row in found] == [
        ('10-K', '2021-12-31'), ('10-Q', '2022-03-31'), ('10-Q', '2022-12-31')]
    assert found.related_forms == {'10-K/A': 1, '10-KT': 1, 'NT 10-Q': 1}
    assert [(row['accession_number'], row['form']) for row in found.unsupported] == [('0000000001-23-000007', '10-Q')]
    assert json.loads(json.dumps(found)) == list(found)          # still a plain list for every caller
    # Forms outside the window are not counted.
    assert discover_filings(_Submissions(_ROWS), '1', '2021-01-01', '2022-06-30').related_forms == {}


def test_discovered_queue_reports_gaps_related_forms_and_unsupported_filings(tmp_path):
    state = {'schema_version': 1, 'configuration': {'since': '2021-01-01', 'until': '2023-12-31'}, 'companies': {}, 'filings': {}}
    universe = {'companies': [{'cik': '0000000001', 'tickers': ['EX']}]}
    summary = backfill.discover_queue(tmp_path / 'queue.json', state, universe, _Submissions(_ROWS), emit=lambda event: None)
    record = state['companies']['0000000001']
    assert record['status'] == 'discovered' and record['filing_count'] == 3 and record['discovered_at_utc']
    assert record['max_period_gap_days'] == 275 and len(record['unsupported_filings']) == 1
    warnings = {row['warning']: row for row in summary['coverage_warnings']}
    assert set(warnings) == {'related_periodic_forms_not_queued', 'unsupported_filings_not_queued', 'reporting_period_gap'}
    assert warnings['related_periodic_forms_not_queued']['forms'] == {'10-K/A': 1, '10-KT': 1, 'NT 10-Q': 1}
    assert warnings['reporting_period_gap']['issuers'] == {'0000000001': 275}
    assert summary['filings_discovered'] == 3 and len(state['filings']) == 3


def test_window_that_has_not_ended_is_reported_as_open():
    def state(until):
        return {'configuration': {'since': '2021-01-01', 'until': until},
                'companies': {'0000000001': {'status': 'discovered', 'filing_count': 4,
                                             'discovered_at_utc': '2026-10-30T00:02:19+00:00'}}}
    open_window = backfill.coverage_warnings(state('2026-12-31'))
    assert [row['warning'] for row in open_window] == ['discovery_window_still_open']
    assert open_window[0]['issuers'] == 1 and open_window[0]['until'] == '2026-12-31'
    assert backfill.coverage_warnings(state('2026-10-29')) == []
    # A queue discovered before this field existed makes no claim either way.
    legacy = {'configuration': {'until': '2026-12-31'}, 'companies': {'0000000001': {'status': 'discovered', 'filing_count': 4}}}
    assert backfill.coverage_warnings(legacy) == []
