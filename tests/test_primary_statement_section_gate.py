"""Statement ownership is enforced independently of recognized heading wording."""

from copy import deepcopy
import hashlib
from uuid import UUID

from lxml import html
import pytest

from sec_pipeline import corrected_acceptance as acceptance
from sec_pipeline import filing_parser_v4 as parser
from sec_pipeline.extraction_profiles import CORRECTED_PROFILE, LEGACY_PROFILE, REVIEWED_PROFILE
from sec_pipeline.primary_statement_sections import primary_statement_section_issues
from sec_pipeline.report_cleaning import clean_report
from sec_pipeline.report_validation import audit_report, source_heading_expectations
from sec_pipeline.source_locators import source_xpath
from sec_pipeline.tables import select_statements
from tests.test_corrected_acceptance import snapshot_with_supplemental
from tests.test_corrected_profile import original_sample_set
from tests.test_review_section_boundaries import annual


@pytest.mark.parametrize('heading', ['', '<h2>UNUSUAL ATTACHMENT TITLE</h2>', '<h2>SIGNATURES</h2>'])
def test_selected_table_gate_has_no_heading_text_dependency(tmp_path, heading):
    path = annual(tmp_path, heading + '<table id="primary"><tr><td>Assets</td><td>100</td></tr></table>')
    report = clean_report(path, form='10-K')
    table = {'table_key': 'balance_sheet', 'statement_type': 'balance_sheet',
             'structure': {'source_xpath': "//*[@id='primary']",
                           'html_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}
    issues = primary_statement_section_issues([table], {'sections': report.sections},
                                             path.read_bytes(), accession='test')
    assert len(issues) == 1
    assert issues[0]['check'] == 'primary_statement_outside_financial_section'
    expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
    audit = audit_report(path, form='10-K', report=report, expected_section_ids=expected,
                         selected_tables=[table])
    assert audit['status'] == 'failed'
    assert any(issue['check'] == 'primary_statement_outside_financial_section' for issue in audit['issues'])


@pytest.mark.parametrize('prefix', ['', '<p>Original supplemental member.</p>'])
@pytest.mark.parametrize('heading', ['CONSOLIDATED BALANCE SHEETS', 'CONSOLIDATED STATEMENTS OF EARNINGS',
                                     'CONSOLIDATED STATEMENTS OF CASH FLOWS'])
def test_supplemental_primary_statements_have_explicit_financial_ownership(tmp_path, prefix, heading):
    path = tmp_path / 'supplemental.htm'
    path.write_text('<html><body>' + prefix + '<h2>' + heading + '</h2>'
                    '<table id="primary"><tr><td>Assets</td><td>100</td></tr></table></body></html>')
    report = clean_report(path, form='10-K')
    table = {'table_key': 'balance_sheet', 'structure': {'source_xpath': "//*[@id='primary']",
             'html_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}
    assert all(section['offset_start'] < section['offset_end'] for section in report.sections)
    assert primary_statement_section_issues([table], {'sections': report.sections},
                                           path.read_bytes(), accession='test') == []


@pytest.mark.parametrize('profile', [LEGACY_PROFILE, CORRECTED_PROFILE, REVIEWED_PROFILE])
def test_parser_rejects_selected_statements_under_nonfinancial_section(
        original_sample_set, monkeypatch, profile):
    original = parser.clean_report

    def mislabeled(*args, **kwargs):
        report = original(*args, **kwargs)
        for section in report.sections:
            if section['section_type'] == 'financial_statements':
                section['section_type'] = 'summary'
        return report

    monkeypatch.setattr(parser, 'clean_report', mislabeled)
    with pytest.raises(parser.FilingSetParseError, match='primary statements must map to financial_statements'):
        parser.parse_filing_set(*original_sample_set, profile=profile)


@pytest.mark.parametrize('section_type', ['financial_statements', 'summary', 'other_information', 'unknown'])
def test_stored_acceptance_rechecks_primary_statement_section_type(
        tmp_path, original_sample_set, monkeypatch, section_type):
    snapshot = snapshot_with_supplemental(tmp_path, original_sample_set)
    # Give the synthetic relational rows valid UUIDs, as the receiving database does.
    identifiers = {section['id']: str(UUID(int=index))
                   for index, section in enumerate(snapshot['sections'], 1)}
    for section in snapshot['sections']:
        section['id'] = identifiers[section['id']]
    for chunk in snapshot['chunks']:
        chunk['section_id'] = identifiers[chunk['section_id']]
    primary = snapshot['sources'][0]
    source = (tmp_path / primary['local_path']).read_bytes()
    tree = html.document_fromstring(source, parser=html.HTMLParser(encoding='utf-8', no_network=True)).getroottree()
    table = select_statements(tree)['balance_sheet']
    snapshot['tables'] = [{'id': str(UUID(int=999)), 'source_document_id': primary['id'],
        'table_key': 'balance_sheet', 'statement_type': 'balance_sheet',
        'structure': {'html_sha256': primary['sha256'], 'source_xpath': source_xpath(tree, table)}}]
    for section in snapshot['sections']:
        if section['section_type'] == 'financial_statements':
            section['section_type'] = section_type
    monkeypatch.setattr(acceptance, 'read_snapshot', lambda *args, **kwargs: deepcopy(snapshot))
    # A successful generic relationship audit cannot override the semantic invariant.
    monkeypatch.setattr(acceptance, 'audit_snapshot', lambda *args, **kwargs: {'status': 'passed'})
    result = acceptance.audit_corrected_set(None, 'fixture', UUID(int=3), tmp_path)
    violations = [error for error in result['errors']
                  if error['check'] == 'primary_statement_outside_financial_section']
    if section_type == 'financial_statements':
        assert result['status'] == 'passed', result
        assert not violations
    else:
        assert result['status'] == 'failed'
        assert len(violations) == 1
        assert violations[0]['section_type'] == section_type
