"""The audited profile corrects stored report metadata under fresh identities.

Every case also checks that the earlier profiles keep their exact output:
stored reports and tables are immutable, so a correction is a new version.
"""
from copy import deepcopy
import unittest
from uuid import UUID, uuid5, NAMESPACE_URL

import pytest

from sec_pipeline import filing_parser_v4 as parser, filing_ingestion_v6, filing_ingestion_v7
from sec_pipeline.chunking import chunk_report
from sec_pipeline.extraction_profiles import (
    AUDITED_PROFILE, CORRECTED_PROFILES, PROFILE_BY_TABLES, REVIEWED_PROFILE, corrected_tables,
    supported_versions, versions_for,
)
from sec_pipeline.inline_document_sets import InlineDocumentSetError, read_inline_document_set
from sec_pipeline.parser_policy import policy_metadata
from sec_pipeline.report_cleaning import clean_report
from sec_pipeline.report_validation import audit_report, source_heading_expectations
from sec_pipeline.sections import SECTION_TYPES
from sec_pipeline.statement_continuations import POLICY, with_omitted_continuations
from sec_pipeline.tables import extract_tables
from sec_pipeline.v4_table_cells import v4_relational_cells
from tests.test_corrected_profile import original_sample_set  # noqa: F401
from tests import test_inline_document_sets as inline_fixtures
from tests.test_inline_document_sets import nonnumeric, target_text
from tests.test_pipeline_audit_regressions import _CALENDAR, _Statements, _filing, _source
from tests.test_v4_table_cells import conflict_fixture

NEW_TYPES = {'signatures', 'unresolved_staff_comments', 'reserved', 'selected_financial_data',
             'foreign_inspection_disclosure'}


def test_audited_profile_has_its_own_report_and_table_identities():
    reviewed, audited = versions_for(REVIEWED_PROFILE), versions_for(AUDITED_PROFILE)
    assert audited == {'facts': 'facts-v1', 'tables': 'tables-v7', 'reports': 'report-v4', 'chunks': 'paragraph-v2'}
    assert supported_versions(audited) and AUDITED_PROFILE in CORRECTED_PROFILES
    assert PROFILE_BY_TABLES['tables-v7'] == AUDITED_PROFILE and PROFILE_BY_TABLES['tables-v6'] == REVIEWED_PROFILE
    old, new = policy_metadata(reviewed)['parser_revisions'], policy_metadata(audited)['parser_revisions']
    assert new == {'facts': old['facts'], 'chunks': old['chunks'], 'reports': 'audited-section-metadata-v1',
                   'tables': 'statement-continuation-inventory-v1'}
    assert old['reports'] == 'reviewed-original-source-boundaries-v4'
    assert old['tables'] == 'registrant-numeric-cell-validation-v2'
    assert not supported_versions(audited | {'reports': 'report-v3'})
    assert filing_ingestion_v7.VERSIONS == audited
    assert filing_ingestion_v7.MIGRATIONS == filing_ingestion_v6.MIGRATIONS + ('016_audited_layout_versions.sql',)


def test_audited_cell_issue_ids_do_not_collide_with_earlier_versions():
    table, _, _ = conflict_fixture()
    reviewed, = corrected_tables([table], profile=REVIEWED_PROFILE)
    audited, = corrected_tables([table], profile=AUDITED_PROFILE)
    assert audited.extraction_version == 'tables-v7'
    assert audited.structure['rows'][0][1]['quality_issue_keys'] == [
        'tables-v7:' + key for key in table.structure['rows'][0][1]['quality_issue_keys']]
    assert audited.structure['rows'][0][1]['source_fact_references'] == reviewed.structure['rows'][0][1]['source_fact_references']
    assert v4_relational_cells(table.filing_id, audited)


def test_audited_original_source_rebuild_and_dry_run(original_sample_set):  # noqa: F811
    path, root = original_sample_set
    audited = parser.parse_filing_set(path, root, profile=AUDITED_PROFILE)
    reviewed = parser.parse_filing_set(path, root, profile=REVIEWED_PROFILE)
    assert audited.versions == filing_ingestion_v7.VERSIONS
    assert {table.extraction_version for table in audited.tables} == {'tables-v7'}
    assert {document.report.extraction_version for document in audited.documents} == {'report-v4'}
    assert audited.semantic_checks['primary_section_boundaries'] is True
    # The same facts, occurrences and cells; only the corrected parts differ.
    assert audited.facts == reviewed.facts and audited.counts == reviewed.counts
    for new, old in zip(audited.tables, reviewed.tables):
        assert ([(cell['cell_id'], cell['display_text'], cell['classification'], cell.get('fact_references'))
                 for row in new.structure['rows'] for cell in row]
                == [(cell['cell_id'], cell['display_text'], cell['classification'], cell.get('fact_references'))
                    for row in old.structure['rows'] for cell in row])
        assert 'omitted_continuations' not in old.structure
    recorded = [table.structure['omitted_continuations'] for table in audited.tables
                if 'omitted_continuations' in table.structure]
    assert len(recorded) == len({table.statement_type for table in audited.tables})
    assert all(entry['policy'] == POLICY and isinstance(entry['tables'], list) for entry in recorded)
    for document, earlier in zip(audited.documents, reviewed.documents):
        text = document.report.full_text
        assert text.replace(' ', '') == earlier.report.full_text.replace(' ', '')
        assert [row['section_id'] for row in document.report.sections] == [row['section_id'] for row in earlier.report.sections]
        assert {row['section_type'] for row in earlier.report.sections}.isdisjoint(NEW_TYPES)
        assert {row['section_type'] for row in document.report.sections} <= SECTION_TYPES
        for chunk in document.chunks:
            assert chunk.chunking_version == 'paragraph-v2' and chunk.text == text[chunk.offset_start:chunk.offset_end]
    result = filing_ingestion_v7.configuration_from_manifest(audited.manifest, audited.manifest_sha256)
    assert result['extraction_profile'] == AUDITED_PROFILE
    assert result['section_validation_policy'] == 'source-boundaries-v2'
    assert result['parser_revisions'] == policy_metadata(audited.versions)['parser_revisions']


def test_audited_preflight_requires_its_migration():
    from contextlib import nullcontext
    from types import SimpleNamespace
    from sec_pipeline.database import _packaged_migrations, migration_digest
    from sec_pipeline.filing_ingestion_v5 import MigrationPrerequisiteError
    packaged = {name: migration_digest(content) for name, content in _packaged_migrations()}
    applied = dict(packaged)

    def connection():
        return SimpleNamespace(autocommit=False, closed=False,
                               info=SimpleNamespace(transaction_status=SimpleNamespace(name='IDLE')),
                               transaction=lambda: nullcontext(),
                               execute=lambda sql, parameters: SimpleNamespace(
                                   fetchone=lambda: (applied[parameters[0]],) if parameters[0] in applied else None))

    filing_ingestion_v7.preflight(connection())
    del applied['016_audited_layout_versions.sql']
    filing_ingestion_v6.preflight(connection())
    with pytest.raises(MigrationPrerequisiteError):
        filing_ingestion_v7.preflight(connection())


# ------------------------------------------------------------------ report-v4

_RUN_IN = ('<p id="i1"><b>Item 1. Business.</b> Example Corp is a leading maker of widgets that it sells worldwide. '
           'Net sales were $1,234<sup>2</sup> and revenues<span style="position:relative;top:-3pt">(1)</span> rose '
           'on the 17<sup>th</sup> day across 10 m<sup>2</sup> of floor space, as discussed in Note<sup>3</sup>.</p>')
_PROXY = '<p id="i10"><b>Item 10. Directors, Executive Officers and Corporate Governance</b></p>'


def _annual(tmp_path, **options):
    content = ('<p>Portions of the proxy statement are incorporated by reference into Part III.</p>'
               + _filing(heading={'i1': _RUN_IN, 'i10': _PROXY}, **options)).replace(
        'Body text of Item 10. Directors, Executive Officers and Corporate Governance, with several words of prose.',
        'The information required is incorporated by reference to our proxy statement.')
    path = _source(tmp_path, content)
    return path, clean_report(path, form='10-K'), clean_report(path, form='10-K', report_version='report-v4')


def test_report_v4_title_is_the_heading_not_its_first_paragraph(tmp_path):
    path, old, new = _annual(tmp_path)
    before = next(row for row in old.sections if row['section_id'] == 'part-i-item-1')
    after = next(row for row in new.sections if row['section_id'] == 'part-i-item-1')
    assert before['title'].startswith('Item 1. Business. Example Corp is a leading maker') and len(before['title']) > 150
    assert (after['title'], after['original_title']) == ('Item 1. Business', 'Item 1. Business')
    # The sentence stays in the section body, at the same offsets.
    assert after['offset_start'] == before['offset_start']
    assert 'Example Corp is a leading maker' in new.full_text[after['offset_start']:after['offset_end']]
    # An ordinary heading is unchanged.
    assert [row['title'] for row in new.sections if row['section_id'] == 'part-i-item-2'] == ['Item 2. Properties']


def test_report_v4_flags_proxy_incorporation_only_in_part_iii(tmp_path):
    path, old, new = _annual(tmp_path)

    def flagged(report):
        return {row['section_id'] for row in report.sections
                if row.get('source_locator', {}).get('incorporation_status') == 'unresolved'}

    assert flagged(old) == {'front-matter', 'part-iii-item-10'}
    assert flagged(new) == {'part-iii-item-10'}


def test_report_v4_types_signatures_and_formerly_unknown_items(tmp_path):
    path, old, new = _annual(tmp_path)
    old_types = {row['section_id']: (row['section_type'], row['validation_status']) for row in old.sections}
    new_types = {row['section_id']: (row['section_type'], row['validation_status']) for row in new.sections}
    assert old_types['signatures'] == ('other_information', 'detected')
    assert old_types['part-i-item-1b'] == old_types['part-ii-item-6'] == ('unknown', 'unverified')
    assert new_types['signatures'] == ('signatures', 'detected')
    assert new_types['part-i-item-1b'] == ('unresolved_staff_comments', 'detected')
    assert new_types['part-ii-item-6'] == ('reserved', 'detected')
    assert new_types['part-ii-item-9b'] == ('other_information', 'detected')        # still the real Item 9B
    assert {kind for kind, _ in new_types.values()} <= SECTION_TYPES
    path, old, new = _annual(tmp_path, titles={'i6': 'Selected Financial Data'})
    assert next(row['section_type'] for row in new.sections if row['section_id'] == 'part-ii-item-6') == 'selected_financial_data'


def test_report_v4_separates_note_references_and_nothing_else(tmp_path):
    path, old, new = _annual(tmp_path)
    assert '$1,2342 and revenues(1) rose' in old.full_text and 'in Note3.' in old.full_text
    assert '$1,234 2 and revenues (1) rose' in new.full_text and 'in Note 3.' in new.full_text
    assert 'on the 17th day across 10 m2 of floor space' in new.full_text          # ordinal and unit exponent stay joined
    assert new.full_text.replace(' ', '') == old.full_text.replace(' ', '')        # only spaces were added
    assert len(new.full_text) == len(old.full_text) + 3


def test_report_v4_output_passes_the_same_independent_audit(tmp_path):
    path, old, new = _annual(tmp_path)
    for report, version in ((old, 'paragraph-v2'), (new, 'paragraph-v2')):
        chunks = chunk_report(filing_id=UUID(int=1), report_id=UUID(int=2), full_text=report.full_text,
                              sections=report.sections, chunking_version=version)
        expected = source_heading_expectations(path, form='10-K')['expected_section_ids']
        result = audit_report(path, form='10-K', report=report, chunks=chunks, expected_section_ids=expected)
        assert result['status'] == 'passed', result['issues']


def test_unversioned_and_earlier_versions_keep_their_exact_output(tmp_path):
    path, old, new = _annual(tmp_path)
    for version in (None, 'report-v1', 'report-v2', 'report-v3'):
        report = clean_report(path, form='10-K', report_version=version)
        assert (report.full_text, report.sections) == (old.full_text, old.sections)
    assert (new.full_text, new.sections) != (old.full_text, old.sections)


# ------------------------------------------------------------------ tables-v7

_SUPPLEMENT = ('<div><p><b>SUPPLEMENTAL DISCLOSURES OF CASH FLOWS INFORMATION</b></p><table><tr><td></td><td>2025</td></tr>'
               '<tr><td>Interest paid</td><td><ix:nonFraction id="s1" name="us-gaap:InterestPaidNet" contextRef="CTX" '
               'unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">12</ix:nonFraction></td></tr></table></div>')
_LEASE_NOTE = ('<div><p>Note 9. Leases</p><table><tr><td>Lease liabilities</td><td><ix:nonFraction id="n1" '
               'name="us-gaap:OperatingLeaseLiability" contextRef="CTX" unitRef="usd" decimals="-6" scale="6" '
               'format="ixt:num-dot-decimal">7</ix:nonFraction></td></tr></table></div>'
               '<div><p><b>Supplemental cash flow information related to leases</b></p><table><tr><td>Cash paid for leases</td>'
               '<td><ix:nonFraction id="n2" name="us-gaap:OperatingLeasePayments" contextRef="CTX" unitRef="usd" decimals="-6" '
               'scale="6" format="ixt:num-dot-decimal">3</ix:nonFraction></td></tr></table></div>')


def _statement_tables(tmp_path, extra):
    html_path, xml_path = _Statements(**_CALENDAR).write(tmp_path)
    context = 'c2025010120251231'
    html_path.write_text(html_path.read_text().replace('</body>', extra.replace('CTX', context) + '</body>'))
    facts = ''.join(f'<us-gaap:{name} id="{identifier}" contextRef="{context}" unitRef="usd" decimals="-6">{value}000000</us-gaap:{name}>'
                    for identifier, name, value in (('s1', 'InterestPaidNet', 12), ('n1', 'OperatingLeaseLiability', 7),
                                                    ('n2', 'OperatingLeasePayments', 3)) if f'id="{identifier}"' in extra)
    xml_path.write_text(xml_path.read_text().replace('</xbrli:xbrl>', facts + '</xbrli:xbrl>'))
    filing = uuid5(NAMESPACE_URL, 'audited-statements')
    tables = extract_tables(html_path, xml_path, filing_id=filing, html_document_id=uuid5(filing, 'html'),
                            xml_document_id=uuid5(filing, 'xml'), facts_version='facts-v1', tables_version='tables-v2')
    return tables, html_path.read_bytes()


def test_tagged_table_printed_after_the_cash_flow_statement_is_recorded_as_omitted(tmp_path):
    # Casey's 10-Q 0000726958-21-000026: the supplemental disclosures page holds
    # 12 tagged facts and is not one of the extracted cash-flow segments.
    tables, source = _statement_tables(tmp_path, _SUPPLEMENT + _LEASE_NOTE)
    assert {table.statement_type for table in tables} == {'income_statement', 'balance_sheet', 'cash_flow'}
    before = deepcopy([table.structure for table in tables])
    audited = with_omitted_continuations(tables, source)
    assert [table.structure for table in tables] == before                         # inputs are not modified
    by_type = {table.statement_type: table.structure['omitted_continuations'] for table in audited}
    assert by_type['income_statement'] == by_type['balance_sheet'] == {'policy': POLICY, 'tables': []}
    omitted, = by_type['cash_flow']['tables']
    assert (omitted['reason'], omitted['tagged_fact_count']) == ('supplemental_cash_flow_disclosures', 1)
    assert omitted['heading'] == 'SUPPLEMENTAL DISCLOSURES OF CASH FLOWS INFORMATION'
    # Rows and cells are untouched; only the inventory is added.
    for new, old in zip(audited, tables):
        assert {key: value for key, value in new.structure.items() if key != 'omitted_continuations'} == old.structure


def test_supplemental_title_in_a_note_is_not_part_of_the_statement(tmp_path):
    tables, source = _statement_tables(tmp_path, _LEASE_NOTE)
    audited = with_omitted_continuations(tables, source)
    assert all(table.structure['omitted_continuations']['tables'] == [] for table in audited)


# ------------------------------------------------------------------ whitespace

class _InlineFixture(unittest.TestCase):
    __test__ = False
    fixture = inline_fixtures.InlineDocumentSetTests.fixture

    def runTest(self):
        pass


@pytest.mark.parametrize('printed,extracted,facet', [
    ('Denver, CO\t', 'Denver, CO', 'collapse'),            # Prologis 0001193125-26-051453, dei:AuditorLocation
    ('\n94111', ' 94111', 'replace'),                      # Prologis 0001564590-21-020897, dei:EntityAddressPostalZipCode
    (' First\n line\tlast ', 'First line last', 'collapse'),
    ('First\nline', 'First line', 'replace'),
])
def test_audited_reader_accepts_xml_schema_whitespace_normalization_and_records_it(printed, extracted, facet):
    case = _InlineFixture()
    try:
        docs, facts, digest = case.fixture(nonnumeric(text=printed), target_text(value=extracted))
        with pytest.raises(InlineDocumentSetError):
            read_inline_document_set(docs, facts, xml_sha256=digest)                # every earlier profile
        result = read_inline_document_set(docs, facts, xml_sha256=digest, normalized_text=True)
        evidence = result.occurrences[0]['evidence']
        assert evidence['inline_whitespace'] == {'policy': 'xml-schema-whitespace-normalization-v1', 'facet': facet,
                                                 'original_text': printed}
        assert (evidence['inline_mapped_value'], evidence['raw_value']) == (extracted, extracted)
        assert result.inventory['nonnumeric_whitespace_normalized'] == 1
    finally:
        case.doCleanups()


@pytest.mark.parametrize('printed,extracted', [
    ('First line', 'First lines'),             # a different value
    ('Firstline', 'First line'),               # whitespace only in the XML value
    ('First line', 'First line'),         # a no-break space is content
    ('First line.', 'First line'),
])
def test_audited_reader_still_rejects_any_other_difference(printed, extracted):
    case = _InlineFixture()
    try:
        docs, facts, digest = case.fixture(nonnumeric(text=printed), target_text(value=extracted))
        with pytest.raises(InlineDocumentSetError):
            read_inline_document_set(docs, facts, xml_sha256=digest, normalized_text=True)
    finally:
        case.doCleanups()
