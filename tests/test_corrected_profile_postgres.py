"""Corrected artifacts coexist with legacy rows and publish explicitly."""

from dataclasses import replace
import os
from uuid import UUID

import psycopg
import pytest

from sec_pipeline import database as db, filing_ingestion_v4, filing_ingestion_v5, filing_ingestion_v6
from sec_pipeline.catalog import ProcessingSetInput, select_preferred_processing_set, store_processing_set
from sec_pipeline.corrected_acceptance import audit_corrected_set
from sec_pipeline.extraction_profiles import CORRECTED_PROFILE, REVIEWED_PROFILE, corrected_tables, versions_for
from sec_pipeline.parser_policy import policy_metadata
from sec_pipeline.processing import ProcessingError
from sec_pipeline.publication import publish_corrected_set, rollback_corrected_set, served_selection
from sec_pipeline.source_set_storage import SOURCE_CHECKS, ReportMembershipInput, store_v4_processing_set
from sec_pipeline.v4_table_cells import store_v4_table_cells
from tests.test_catalog_postgres import disposable_catalog_database
from tests.test_corrected_profile import original_sample_set
from tests.test_source_set_postgres import database_fixture, local_connection, seal


def corrected_fixture(conn, item, *, key="corrected-test", profile=CORRECTED_PROFILE, **changes):
    versions = versions_for(profile)
    table, = corrected_tables([item.table], profile=profile)
    table_id = db.store_financial_table(conn, table)
    cells = store_v4_table_cells(conn, table_id, table, item.fact_ids, item.occurrences, item.document_set)
    reports = []
    for name in ("primary", "secondary"):
        text = "Corrected " + name + " document text."
        report_id = db.store_report(conn, db.ReportInput(filing_id=item.filing,
            source_document_id=item.sources[name], extraction_version=versions["reports"], full_text=text))
        db.store_chunk(conn, db.ChunkInput(filing_id=item.filing, report_id=report_id,
            chunking_version=versions["chunks"], chunk_index=0, text=text, offset_start=0, offset_end=len(text)))
        reports.append(ReportMembershipInput(report_id=report_id, chunking_version=versions["chunks"]))
    selection = replace(item.selection, set_key=key, versions=versions,
        source_checks=item.selection.source_checks | {"numeric_tag_boundaries_verified": True},
        semantic_checks=item.selection.semantic_checks | {"primary_section_boundaries": True},
        configuration=item.selection.configuration | {"versions": versions, **policy_metadata(versions),
            "extraction_profile": profile, "section_validation_policy": ("source-boundaries-v2" if profile == REVIEWED_PROFILE else "original-heading-v1")})
    selection = replace(selection, **changes)
    set_id = store_v4_processing_set(conn, selection, table_ids=[table_id], reports=reports,
                                     issue_ids=list(cells.issue_ids.values()))
    return set_id, table_id, cells


def test_corrected_set_is_hidden_until_publication_and_rollback_restores_reader(local_connection):
    conn = local_connection
    item = database_fixture(conn, review=False)
    old = seal(conn, item)
    corrected, table_id, _ = corrected_fixture(conn, item)
    assert corrected_fixture(conn, item)[0] == corrected
    assert old != corrected and item.table_id != table_id
    assert served_selection(conn, item.filing) == ("source", old)
    assert conn.execute("SELECT count(*) FROM sec.reports WHERE filing_id=%s", (item.filing,)).fetchone()[0] == 4
    with pytest.raises(db.DataConflict):
        publish_corrected_set(conn, item.filing, corrected, expected_current=(None, None))
    assert served_selection(conn, item.filing) == ("source", old)
    assert publish_corrected_set(conn, item.filing, corrected, expected_current=("source", old)) == ("source", corrected)
    assert publish_corrected_set(conn, item.filing, corrected, expected_current=("source", old)) == ("source", corrected)
    assert conn.execute("SELECT count(*) FROM sec.source_publication_history WHERE filing_id=%s", (item.filing,)).fetchone()[0] == 1
    assert rollback_corrected_set(conn, item.filing, corrected) == ("source", old)
    assert conn.execute("SELECT count(*) FROM sec.source_publication_history WHERE filing_id=%s", (item.filing,)).fetchone()[0] == 2
    assert seal(conn, item) == old
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_review_issues_remain_distinct_and_cannot_be_published(local_connection):
    conn = local_connection
    item = database_fixture(conn, review=True)
    old = seal(conn, item)
    corrected, _, cells = corrected_fixture(conn, item)
    assert set(item.cell_result.issue_ids.values()).isdisjoint(cells.issue_ids.values())
    assert conn.execute("SELECT count(*) FROM sec.source_quality_issues WHERE document_set_id=%s", (item.document_set,)).fetchone()[0] == 2
    assert conn.execute("SELECT validation_status FROM sec.source_processing_sets WHERE id=%s", (corrected,)).fetchone()[0] == "review_required"
    with pytest.raises(psycopg.errors.CheckViolation, match="without unresolved review"):
        publish_corrected_set(conn, item.filing, corrected, expected_current=("source", old))
    assert served_selection(conn, item.filing) == ("source", old)
    assert conn.execute("SELECT count(*) FROM sec.source_publication_history WHERE filing_id=%s", (item.filing,)).fetchone()[0] == 0
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_republication_records_one_event_and_restores_previous_corrected_set(local_connection):
    conn = local_connection
    item = database_fixture(conn, review=False)
    old = seal(conn, item)
    first, _, _ = corrected_fixture(conn, item)
    second, _, _ = corrected_fixture(conn, item, key="second-corrected")
    publish_corrected_set(conn, item.filing, first, expected_current=("source", old))
    publish_corrected_set(conn, item.filing, second, expected_current=("source", first))
    assert conn.execute("SELECT count(*) FROM sec.source_publication_history WHERE filing_id=%s", (item.filing,)).fetchone()[0] == 2
    assert rollback_corrected_set(conn, item.filing, second) == ("source", first)


@pytest.mark.parametrize("changes", [
    {"semantic_checks": {"period_header_consistency": True}},
    {"source_checks": {key: True for key in SOURCE_CHECKS}},
    {"configuration": {"versions": versions_for(CORRECTED_PROFILE), "extraction_profile": CORRECTED_PROFILE,
                       "section_validation_policy": "original-heading-v1", "parser_revisions": {}}},
])
def test_database_refuses_missing_original_checks_or_stale_receipts(local_connection, changes):
    conn = local_connection
    item = database_fixture(conn, review=False)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        corrected_fixture(conn, item, **changes)


def test_corrected_only_filing_has_no_reader_content_before_publication(local_connection):
    conn = local_connection
    item = database_fixture(conn, review=False)
    corrected, _, _ = corrected_fixture(conn, item)
    assert served_selection(conn, item.filing) == (None, None)
    publish_corrected_set(conn, item.filing, corrected, expected_current=(None, None))
    assert rollback_corrected_set(conn, item.filing, corrected) == (None, None)


def test_rollback_refuses_a_different_baseline(local_connection):
    conn = local_connection
    item = database_fixture(conn, review=False)
    old = seal(conn, item)
    corrected, _, _ = corrected_fixture(conn, item)
    publish_corrected_set(conn, item.filing, corrected, expected_current=("source", old))
    # A new legacy preference changes the fallback while the corrected pointer
    # remains active; rollback must not silently select that different dataset.
    later = store_processing_set(conn, ProcessingSetInput(filing_id=item.filing, set_key="later-legacy",
        fact_source_document_id=item.sources["xml"], fact_extraction_version="facts-v1",
        coverage={"facts": "complete", "tables": "absent", "reports": "absent", "chunks": "absent"},
        validation_status="passed", validation={"checks": {"fixture": True}}, configuration={"fixture": True}))
    select_preferred_processing_set(conn, item.filing, later)
    with pytest.raises(db.DataConflict, match="baseline changed"):
        rollback_corrected_set(conn, item.filing, corrected)
    assert served_selection(conn, item.filing) == ("source", corrected)


@pytest.mark.skipif(os.environ.get("SEC_DB_TEST") != "1" or os.environ.get("SEC_DB_CONCURRENCY_TEST") != "1",
                    reason="Committed tests require an explicitly local disposable database.")
@pytest.mark.parametrize("ingestion,profile", [
    pytest.param(filing_ingestion_v5, CORRECTED_PROFILE, id="corrected-v5"),
    pytest.param(filing_ingestion_v6, REVIEWED_PROFILE, id="reviewed-v6"),
])
def test_original_filing_commits_replays_rolls_back_and_preserves_old_outputs(
        original_sample_set, monkeypatch, ingestion, profile):
    manifest, root = original_sample_set
    versions = versions_for(profile)
    with disposable_catalog_database() as options, psycopg.connect(**options) as conn:
        old = filing_ingestion_v4.run_ingestion(conn, manifest_path=manifest, data_root=root)
        old_id, filing_id = UUID(old["processing_set_id"]), UUID(old["filing_id"])
        original = filing_ingestion_v4._verify_stored

        def reject(*args, **kwargs):
            original(*args, **kwargs)
            raise ProcessingError("Injected failure after stored verification")

        monkeypatch.setattr(filing_ingestion_v4, "_verify_stored", reject)
        with pytest.raises(ProcessingError, match="Injected failure"):
            ingestion.run_ingestion(conn, manifest_path=manifest, data_root=root)
        with conn.transaction():
            assert conn.execute("SELECT count(*) FROM sec.reports WHERE extraction_version=%s", (versions["reports"],)).fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM sec.financial_tables WHERE extraction_version=%s", (versions["tables"],)).fetchone()[0] == 0
            assert served_selection(conn, filing_id) == ("source", old_id)
        monkeypatch.setattr(filing_ingestion_v4, "_verify_stored", original)
        new = ingestion.run_ingestion(conn, manifest_path=manifest, data_root=root)
        assert new["versions"] == versions
        assert ingestion.run_ingestion(conn, manifest_path=manifest, data_root=root) == new
        assert filing_ingestion_v4.run_ingestion(conn, manifest_path=manifest, data_root=root) == old
        new_id = UUID(new["processing_set_id"])
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            acceptance = audit_corrected_set(conn, new["accession_number"], new_id, root)
            assert acceptance["status"] == "passed", acceptance
            assert len(acceptance["table_relationships"]["section_mappings"]) == 3
            assert acceptance["retrieval_index_status"] == "not_verified"
        with conn.transaction():
            assert conn.execute("SELECT count(*) FROM sec.financial_facts").fetchone()[0] == 1622
            assert conn.execute("SELECT count(*) FROM sec.financial_tables").fetchone()[0] == 6
            assert conn.execute("SELECT count(*) FROM sec.reports").fetchone()[0] == 2
            assert conn.execute("SELECT DISTINCT chunking_version FROM sec.source_processing_reports WHERE processing_set_id=%s", (new_id,)).fetchall() == [(versions["chunks"],)]
            assert served_selection(conn, filing_id) == ("source", old_id)
            publish_corrected_set(conn, filing_id, new_id, expected_current=("source", old_id))
        with conn.transaction():
            assert served_selection(conn, filing_id) == ("source", new_id)
            rollback_corrected_set(conn, filing_id, new_id)
        with conn.transaction():
            assert served_selection(conn, filing_id) == ("source", old_id)
            assert conn.execute("SELECT count(*) FROM sec.source_publication_history").fetchone()[0] == 2
