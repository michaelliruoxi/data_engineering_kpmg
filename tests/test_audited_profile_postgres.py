"""Run the audited V7 versions against the disposable PostgreSQL fixture."""
import psycopg
import pytest

from sec_pipeline import database as db, filing_ingestion_v7
from sec_pipeline.extraction_profiles import AUDITED_PROFILE, REVIEWED_PROFILE
from sec_pipeline.parser_policy import policy_metadata
from sec_pipeline.publication import publish_corrected_set, rollback_corrected_set, served_selection
from sec_pipeline.sections import store_report_sections
from tests.test_catalog_postgres import disposable_catalog_database  # noqa: F401
from tests.test_corrected_profile_postgres import corrected_fixture
from tests.test_source_set_postgres import database_fixture, local_connection, seal  # noqa: F401


def test_v4_to_v7_coexist_and_audited_publication_is_explicit(local_connection):  # noqa: F811
    conn = local_connection
    item = database_fixture(conn, review=False)
    original = seal(conn, item)
    corrected, _, _ = corrected_fixture(conn, item)
    reviewed, _, _ = corrected_fixture(conn, item, key='reviewed-test', profile=REVIEWED_PROFILE)
    audited, _, _ = corrected_fixture(conn, item, key='audited-test', profile=AUDITED_PROFILE)
    assert len({original, corrected, reviewed, audited}) == 4
    assert served_selection(conn, item.filing) == ('source', original)
    selected = conn.execute("SELECT versions FROM sec.source_processing_sets WHERE filing_id=%s", (item.filing,)).fetchall()
    assert {row[0]['tables'] for row in selected} == {'tables-v4', 'tables-v5', 'tables-v6', 'tables-v7'}
    assert {row[0]['reports'] for row in selected} == {'report-v1', 'report-v2', 'report-v3', 'report-v4'}
    assert publish_corrected_set(conn, item.filing, audited, expected_current=('source', original)) == ('source', audited)
    assert rollback_corrected_set(conn, item.filing, audited) == ('source', original)
    # The reviewed set is still publishable beside it, under its own revisions.
    assert publish_corrected_set(conn, item.filing, reviewed, expected_current=('source', original)) == ('source', reviewed)
    assert rollback_corrected_set(conn, item.filing, reviewed) == ('source', original)
    recorded = conn.execute("SELECT parser_revisions FROM finance.processing_revision WHERE processing_set_id=%s",
                            (audited,)).fetchone()[0]
    assert recorded == policy_metadata(filing_ingestion_v7.VERSIONS)['parser_revisions']
    assert recorded['reports'] == 'audited-section-metadata-v1'
    conn.execute('SET CONSTRAINTS ALL IMMEDIATE')


def test_audited_tuple_rejects_another_versions_reports_or_revisions(local_connection):  # noqa: F811
    conn = local_connection
    item = database_fixture(conn, review=False)
    with pytest.raises((ValueError, psycopg.errors.CheckViolation)), conn.transaction():
        corrected_fixture(conn, item, profile=AUDITED_PROFILE, versions=filing_ingestion_v7.VERSIONS | {'reports': 'report-v3'})
    with pytest.raises((ValueError, psycopg.errors.CheckViolation)), conn.transaction():
        corrected_fixture(conn, item, profile=AUDITED_PROFILE, versions=filing_ingestion_v7.VERSIONS | {'chunks': 'paragraph-v1'})
    stale = policy_metadata(filing_ingestion_v7.VERSIONS)['parser_revisions'] | {'reports': 'reviewed-original-source-boundaries-v4'}
    selection = item.selection.configuration | {'versions': filing_ingestion_v7.VERSIONS, 'parser_revisions': stale,
                                                'extraction_profile': AUDITED_PROFILE,
                                                'section_validation_policy': 'source-boundaries-v2'}
    with pytest.raises((ValueError, psycopg.errors.CheckViolation)), conn.transaction():
        corrected_fixture(conn, item, key='stale-revisions', profile=AUDITED_PROFILE, configuration=selection)


def test_report_v4_section_types_are_stored(local_connection):  # noqa: F811
    conn = local_connection
    item = database_fixture(conn, review=False)
    text = 'Item 1B. Unresolved Staff Comments\n\nNone.\n\nItem 6. [Reserved]\n\nSIGNATURES\n\nSigned.'
    cuts = [0, text.index('Item 6.'), text.index('SIGNATURES'), len(text)]
    kinds = ('unresolved_staff_comments', 'reserved', 'signatures')
    sections = [{'section_id': f'section-{index}', 'section_type': kind, 'title': kind, 'part': None, 'item': None,
                 'offset_start': cuts[index], 'offset_end': cuts[index + 1], 'validation_status': 'detected',
                 'source_locator': {}} for index, kind in enumerate(kinds)]
    report = db.ReportInput(filing_id=item.filing, source_document_id=item.sources['primary'],
                            extraction_version='report-v4', full_text=text, sections=sections)
    report_id = db.store_report(conn, report)
    stored = store_report_sections(conn, report, report_id=report_id)
    assert len(stored) == 3
    rows = conn.execute("""SELECT rs.section_type, st.label FROM sec.report_sections rs
        JOIN sec.section_types st ON st.code=rs.section_type WHERE rs.report_id=%s ORDER BY rs.section_order""",
        (report_id,)).fetchall()
    assert [row[0] for row in rows] == list(kinds) and all(row[1] for row in rows)
    for code in ('selected_financial_data', 'foreign_inspection_disclosure'):
        assert conn.execute('SELECT 1 FROM sec.section_types WHERE code=%s', (code,)).fetchone()
