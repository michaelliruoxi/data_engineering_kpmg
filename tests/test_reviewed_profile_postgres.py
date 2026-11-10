"""Run additive reviewed versions against the disposable PostgreSQL fixture."""
from dataclasses import replace

import psycopg
import pytest

from sec_pipeline.extraction_profiles import REVIEWED_PROFILE
from sec_pipeline.publication import publish_corrected_set, rollback_corrected_set, served_selection
from sec_pipeline.parser_policy import policy_metadata
from sec_pipeline import filing_ingestion_v6
from tests.test_catalog_postgres import disposable_catalog_database
from tests.test_source_set_postgres import database_fixture, local_connection, seal
from tests.test_corrected_profile_postgres import corrected_fixture


def test_v4_v5_v6_coexist_and_reviewed_publication_is_explicit(local_connection):
    conn = local_connection
    item = database_fixture(conn, review=False)
    original = seal(conn, item)
    corrected, _, _ = corrected_fixture(conn, item)
    reviewed, _, _ = corrected_fixture(conn, item, key='reviewed-test', profile=REVIEWED_PROFILE)
    assert served_selection(conn, item.filing) == ('source', original)
    selected = conn.execute("SELECT versions FROM sec.source_processing_sets WHERE filing_id=%s", (item.filing,)).fetchall()
    assert {row[0]['tables'] for row in selected} == {'tables-v4', 'tables-v5', 'tables-v6'}
    assert publish_corrected_set(conn, item.filing, reviewed, expected_current=('source', original)) == ('source', reviewed)
    assert rollback_corrected_set(conn, item.filing, reviewed) == ('source', original)
    assert conn.execute("SELECT parser_revisions FROM finance.processing_revision WHERE processing_set_id=%s", (reviewed,)).fetchone()[0] == policy_metadata(filing_ingestion_v6.VERSIONS)['parser_revisions']
    conn.execute('SET CONSTRAINTS ALL IMMEDIATE')


def test_reviewed_tuple_rejects_old_chunk_membership(local_connection):
    conn = local_connection
    item = database_fixture(conn, review=False)
    versions = filing_ingestion_v6.VERSIONS | {'chunks': 'paragraph-v1'}
    with pytest.raises((ValueError, psycopg.errors.CheckViolation)), conn.transaction():
        corrected_fixture(conn, item, profile=REVIEWED_PROFILE, versions=versions)
