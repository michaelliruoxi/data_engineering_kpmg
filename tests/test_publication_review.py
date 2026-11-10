"""Publication lineage and served views must agree through upgrades and rollbacks."""

import pytest

from sec_pipeline import database as db
from sec_pipeline.publication import publish_corrected_set, rollback_corrected_set, served_selection
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


def _default_counts(conn, filing):
    return {view: conn.execute(f"SELECT count(*) FROM sec.{view} WHERE filing_id=%s", (filing,)).fetchone()[0]
            for view in ("analysis_ready_facts", "analysis_ready_statement_cells", "source_chunk_catalog", "chunk_citations")}


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
