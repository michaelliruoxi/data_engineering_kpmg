"""Publication lineage and served views must agree through upgrades and rollbacks."""

import os

import psycopg
import pytest

from sec_pipeline import database as db
from sec_pipeline.catalog import ProcessingSetInput, select_preferred_processing_set, store_processing_set
from sec_pipeline.publication import publish_corrected_set, rollback_corrected_set, served_selection
from tests.test_catalog_postgres import disposable_catalog_database
from tests.test_corrected_profile_postgres import corrected_fixture
from tests.test_source_set_postgres import database_fixture, local_connection, seal


def test_chained_rollbacks_restore_original_baseline(local_connection):
    conn = local_connection
    item = database_fixture(conn, review=False)
    old = seal(conn, item)
    first, _, _ = corrected_fixture(conn, item, key="first")
    second, _, _ = corrected_fixture(conn, item, key="second")
    publish_corrected_set(conn, item.filing, first, expected_current=("source", old))
    publish_corrected_set(conn, item.filing, second, expected_current=("source", first))
    assert rollback_corrected_set(conn, item.filing, second) == ("source", first)
    assert rollback_corrected_set(conn, item.filing, first) == ("source", old)
    history = conn.execute("""SELECT event_id,event_kind,reverts_event_id
        FROM sec.source_publication_history WHERE filing_id=%s ORDER BY event_id""", (item.filing,)).fetchall()
    assert [r[1] for r in history] == ["publish", "publish", "rollback", "rollback"]
    assert [r[2] for r in history[2:]] == [history[1][0], history[0][0]]
    # A deliberately new publication of a withdrawn set has its own baseline.
    publish_corrected_set(conn, item.filing, second, expected_current=("source", old))
    assert rollback_corrected_set(conn, item.filing, second) == ("source", old)


def _default_counts(conn, filing, *, include_legacy=False):
    views = ("analysis_ready_facts", "analysis_ready_statement_cells", "source_chunk_catalog", "chunk_citations")
    if include_legacy:
        views += ("chunk_catalog",)
    return {view: conn.execute(f"SELECT count(*) FROM sec.{view} WHERE filing_id=%s", (filing,)).fetchone()[0]
            for view in views}


def test_unpublished_corrected_rows_do_not_change_consumer_views(local_connection):
    conn = local_connection
    item = database_fixture(conn, review=False)
    old = seal(conn, item)
    before = _default_counts(conn, item.filing)
    assert all(before.values())
    corrected, _, _ = corrected_fixture(conn, item)
    assert _default_counts(conn, item.filing) == before
    assert conn.execute("SELECT count(*) FROM sec.source_chunk_catalog_history WHERE filing_id=%s", (item.filing,)).fetchone()[0] > before["source_chunk_catalog"]
    publish_corrected_set(conn, item.filing, corrected, expected_current=("source", old))
    for view in ("analysis_ready_facts", "analysis_ready_statement_cells", "source_chunk_catalog"):
        assert conn.execute(f"SELECT DISTINCT processing_set_id FROM sec.{view} WHERE filing_id=%s", (item.filing,)).fetchall() == [(corrected,)]
    assert rollback_corrected_set(conn, item.filing, corrected) == ("source", old)
    assert _default_counts(conn, item.filing) == before


def test_corrected_only_citations_are_hidden_until_publication(local_connection):
    conn = local_connection
    item = database_fixture(conn, review=False)
    corrected, _, _ = corrected_fixture(conn, item)
    # No source set has ever been selected. Raw legacy fixtures may remain in
    # the original citation API, but corrected report identities cannot leak.
    assert conn.execute("SELECT count(*) FROM sec.chunk_citations WHERE filing_id=%s AND extraction_version IN ('report-v2','report-v3')", (item.filing,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM sec.source_chunk_catalog WHERE filing_id=%s", (item.filing,)).fetchone()[0] == 0
    publish_corrected_set(conn, item.filing, corrected, expected_current=(None, None))
    assert conn.execute("SELECT count(*) FROM sec.chunk_citations WHERE filing_id=%s AND extraction_version IN ('report-v2','report-v3')", (item.filing,)).fetchone()[0] == 2


def _prefer_legacy(conn, item):
    identifier = store_processing_set(conn, ProcessingSetInput(
        filing_id=item.filing, set_key="explicit-legacy-consumer",
        fact_source_document_id=item.sources["xml"], fact_extraction_version="facts-v1",
        table_source_document_id=item.sources["primary"], table_extraction_version="tables-v4",
        report_id=item.reports[0].report_id, chunking_version="paragraph-v1",
        coverage=item.selection.coverage, validation_status="passed",
        validation={"checks": {"fixture": True}}, configuration={"fixture": True}))
    select_preferred_processing_set(conn, item.filing, identifier)
    return identifier


def _legacy_chunks(conn, filing):
    return conn.execute("""SELECT processing_set_id,chunk_id,report_id,text
        FROM sec.chunk_catalog WHERE filing_id=%s ORDER BY chunk_id""", (filing,)).fetchall()


def _assert_legacy_publication_contract(conn, item, legacy):
    before = _default_counts(conn, item.filing, include_legacy=True)
    chunks = _legacy_chunks(conn, item.filing)
    assert before == {"analysis_ready_facts": 0, "analysis_ready_statement_cells": 0,
                      "source_chunk_catalog": 0, "chunk_citations": 1, "chunk_catalog": 1}
    assert chunks and {row[0] for row in chunks} == {legacy}
    corrected, _, _ = corrected_fixture(conn, item)
    assert _default_counts(conn, item.filing, include_legacy=True) == before
    assert publish_corrected_set(conn, item.filing, corrected,
                                 expected_current=("legacy", legacy)) == ("source", corrected)
    assert _legacy_chunks(conn, item.filing) == chunks
    for view in ("chunk_catalog", "fact_catalog", "statement_catalog"):
        assert conn.execute(f"SELECT DISTINCT processing_set_id FROM sec.{view} WHERE filing_id=%s",
                            (item.filing,)).fetchall() == [(legacy,)]
    for view in ("analysis_ready_facts", "analysis_ready_statement_cells", "source_chunk_catalog"):
        assert conn.execute(f"SELECT DISTINCT processing_set_id FROM sec.{view} WHERE filing_id=%s",
                            (item.filing,)).fetchall() == [(corrected,)]
    assert conn.execute("SELECT DISTINCT extraction_version FROM sec.chunk_citations WHERE filing_id=%s",
                        (item.filing,)).fetchall() == [("report-v2",)]
    assert rollback_corrected_set(conn, item.filing, corrected) == ("legacy", legacy)
    assert _default_counts(conn, item.filing, include_legacy=True) == before
    assert _legacy_chunks(conn, item.filing) == chunks


def test_explicit_legacy_chunk_catalog_survives_corrected_publication(local_connection):
    conn = local_connection
    item = database_fixture(conn, review=False)
    original = seal(conn, item)
    legacy = _prefer_legacy(conn, item)
    assert served_selection(conn, item.filing) == ("legacy", legacy)
    _assert_legacy_publication_contract(conn, item, legacy)
    assert conn.execute("SELECT validation_status FROM sec.source_processing_sets WHERE id=%s",
                        (original,)).fetchone()[0] == "passed"
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.skipif(os.environ.get("SEC_DB_TEST") != "1" or os.environ.get("SEC_DB_CONCURRENCY_TEST") != "1",
                    reason="Populated upgrades require an explicitly local disposable PostgreSQL database.")
def test_populated_reader_upgrade_preserves_legacy_catalog_and_deliberately_narrows_analysis(monkeypatch):
    packaged = db._packaged_migrations()
    initial = [entry for entry in packaged if entry[0] < "012_"]
    monkeypatch.setattr(db, "_packaged_migrations", lambda: initial)
    with disposable_catalog_database() as options, psycopg.connect(**options) as conn:
        with conn.transaction():
            item = database_fixture(conn, review=False)
            original = seal(conn, item)
            legacy = _prefer_legacy(conn, item)
            before = _default_counts(conn, item.filing, include_legacy=True)
            assert all(before.values())
            assert served_selection(conn, item.filing) == ("legacy", legacy)
            chunks = _legacy_chunks(conn, item.filing)
            records = {}
            for relation in ("financial_facts", "financial_tables", "reports", "chunks",
                             "filing_processing_sets", "source_processing_sets", "preferred_processing_sets"):
                records[relation] = conn.execute(
                    f"SELECT to_jsonb(r) FROM sec.{relation} r WHERE filing_id=%s ORDER BY to_jsonb(r)::text",
                    (item.filing,)).fetchall()

        monkeypatch.setattr(db, "_packaged_migrations", lambda: packaged)
        applied = db.migrate(conn)
        assert "012_publication_readers.sql" in applied
        assert "014_reader_contracts.sql" in applied
        with conn.transaction():
            assert served_selection(conn, item.filing) == ("legacy", legacy)
            assert _legacy_chunks(conn, item.filing) == chunks
            for relation, rows in records.items():
                assert conn.execute(
                    f"SELECT to_jsonb(r) FROM sec.{relation} r WHERE filing_id=%s ORDER BY to_jsonb(r)::text",
                    (item.filing,)).fetchall() == rows
            for view in ("analysis_ready_facts", "analysis_ready_statement_cells"):
                assert conn.execute(f"SELECT count(*) FROM sec.{view} WHERE processing_set_id=%s",
                                    (original,)).fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM sec.source_fact_catalog WHERE processing_set_id=%s",
                                (original,)).fetchone()[0] == before["analysis_ready_facts"]
            _assert_legacy_publication_contract(conn, item, legacy)
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
