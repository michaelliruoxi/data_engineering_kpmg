"""A populated parser-policy upgrade preserves history without republishing stale sets."""

import os

import psycopg
import pytest

from sec_pipeline import database as db
from sec_pipeline.extraction_profiles import CORRECTED_PROFILE, REVIEWED_PROFILE, versions_for
from sec_pipeline.publication import publish_corrected_set, rollback_corrected_set, served_selection
from tests.test_catalog_postgres import disposable_catalog_database
from tests.test_corrected_profile_postgres import corrected_fixture
from tests.test_source_set_postgres import database_fixture, seal


def _historical_configuration(item, profile):
    """The literal receipts required by the unchanged migration 013."""
    versions = versions_for(profile)
    revisions = {
        "facts": "numeric-tag-boundaries-v2",
        "reports": ("corrected-visible-layout-boundaries-v3" if profile == CORRECTED_PROFILE
                    else "reviewed-original-source-boundaries-v3"),
        "tables": "registrant-numeric-cell-validation-v2",
    }
    if profile == REVIEWED_PROFILE:
        revisions["chunks"] = "token-boundary-overlap-v2"
    return item.selection.configuration | {
        "versions": versions, "parser_revisions": revisions, "extraction_profile": profile,
        "section_validation_policy": ("original-heading-v1" if profile == CORRECTED_PROFILE
                                      else "source-boundaries-v2"),
    }


def _immutable_rows(conn, filing, set_ids):
    result = {}
    for relation in ("source_documents", "financial_facts", "financial_tables", "table_cells",
                     "reports", "report_sections", "chunks", "fact_source_occurrences"):
        result[relation] = conn.execute(
            f"SELECT to_jsonb(r) FROM sec.{relation} r WHERE filing_id=%s ORDER BY id", (filing,)
        ).fetchall()
    result["source_processing_sets"] = conn.execute(
        "SELECT to_jsonb(r) FROM sec.source_processing_sets r WHERE id=ANY(%s) ORDER BY id", (set_ids,)
    ).fetchall()
    return result


@pytest.mark.skipif(os.environ.get("SEC_DB_TEST") != "1" or os.environ.get("SEC_DB_CONCURRENCY_TEST") != "1",
                    reason="Populated upgrades require an explicitly local disposable PostgreSQL database.")
@pytest.mark.parametrize("profile", [CORRECTED_PROFILE, REVIEWED_PROFILE], ids=["historical-v5", "historical-v6"])
def test_populated_revision_upgrade_blocks_new_publication_and_preserves_historical_rollback(monkeypatch, profile):
    packaged = db._packaged_migrations()
    before_revision_upgrade = [entry for entry in packaged if entry[0] < "015_"]
    monkeypatch.setattr(db, "_packaged_migrations", lambda: before_revision_upgrade)
    with disposable_catalog_database() as options, psycopg.connect(**options) as conn:
        with conn.transaction():
            item = database_fixture(conn, review=False)
            baseline = seal(conn, item)
            historical = _historical_configuration(item, profile)
            approved, _, _ = corrected_fixture(conn, item, key="historically-approved", profile=profile,
                                                configuration=historical)
            unpublished, _, _ = corrected_fixture(conn, item, key="historically-unpublished", profile=profile,
                                                   configuration=historical)
            # Publish using the then-current SQL contract, before migration 015.
            conn.execute("INSERT INTO sec.preferred_source_processing_sets(filing_id,processing_set_id) VALUES (%s,%s)",
                         (item.filing, approved))
            event = conn.execute("SELECT event_id FROM sec.source_publication_history WHERE filing_id=%s",
                                 (item.filing,)).fetchone()[0]
            old_pointer = conn.execute("SELECT to_jsonb(p) FROM sec.preferred_source_processing_sets p WHERE filing_id=%s",
                                       (item.filing,)).fetchone()[0]
            original = _immutable_rows(conn, item.filing, [baseline, approved, unpublished])
            assert served_selection(conn, item.filing) == ("source", approved)

        monkeypatch.setattr(db, "_packaged_migrations", lambda: packaged)
        # 016 adds the audited V7 identities and leaves the V5/V6 rules of 015 as they are.
        assert db.migrate(conn) == ["015_parser_revision_contracts.sql", "016_audited_layout_versions.sql"]
        with conn.transaction():
            assert _immutable_rows(conn, item.filing, [baseline, approved, unpublished]) == original
            assert served_selection(conn, item.filing) == ("source", approved)

            # SQL no-ops retain the old pointer, timestamp and history. They are
            # not a new decision to publish an old parser receipt.
            conn.execute("UPDATE sec.preferred_source_processing_sets SET processing_set_id=%s WHERE filing_id=%s",
                         (approved, item.filing))
            conn.execute("""INSERT INTO sec.preferred_source_processing_sets(filing_id,processing_set_id)
                VALUES (%s,%s) ON CONFLICT(filing_id) DO UPDATE SET processing_set_id=EXCLUDED.processing_set_id""",
                         (item.filing, approved))
            assert conn.execute("SELECT to_jsonb(p) FROM sec.preferred_source_processing_sets p WHERE filing_id=%s",
                                (item.filing,)).fetchone()[0] == old_pointer
            assert conn.execute("SELECT count(*) FROM sec.source_publication_history WHERE filing_id=%s",
                                (item.filing,)).fetchone()[0] == 1

            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                publish_corrected_set(conn, item.filing, unpublished, expected_current=("source", approved))
            assert served_selection(conn, item.filing) == ("source", approved)

            # Naming an actual event does not authorize an unrelated stale
            # target: the existing reversion lineage must match exactly.
            with pytest.raises(psycopg.errors.CheckViolation, match="active publication event"), conn.transaction():
                conn.execute("SELECT set_config('sec.rollback_publication_event',%s,true)", (str(event),))
                conn.execute("UPDATE sec.preferred_source_processing_sets SET processing_set_id=%s WHERE filing_id=%s",
                             (unpublished, item.filing))

            current, _, _ = corrected_fixture(conn, item, key="current-parser", profile=profile)
            assert publish_corrected_set(conn, item.filing, current,
                                         expected_current=("source", approved)) == ("source", current)
            assert rollback_corrected_set(conn, item.filing, current) == ("source", approved)
            assert rollback_corrected_set(conn, item.filing, approved) == ("source", baseline)
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                publish_corrected_set(conn, item.filing, approved, expected_current=("source", baseline))
            assert served_selection(conn, item.filing) == ("source", baseline)
            assert _immutable_rows(conn, item.filing, [baseline, approved, unpublished]) == original
            history = conn.execute("""SELECT event_id,event_kind,reverts_event_id
                FROM sec.source_publication_history WHERE filing_id=%s ORDER BY event_id""", (item.filing,)).fetchall()
            assert [row[1] for row in history] == ["publish", "publish", "rollback", "rollback"]
            assert [row[2] for row in history[2:]] == [history[1][0], history[0][0]]
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
