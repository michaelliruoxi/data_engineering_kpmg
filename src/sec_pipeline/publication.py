"""Explicit, reversible corrected-source publication in caller transactions."""

from uuid import UUID

from .database import DataConflict, DatabaseError, _packaged_migrations, migration_checksum_matches


def _begin(conn, filing_id):
    if conn.autocommit or not isinstance(filing_id, UUID):
        raise DatabaseError("Publication requires a filing UUID and caller-managed transaction.")
    packaged = dict(_packaged_migrations())
    for migration in ("012_publication_readers.sql", "013_reviewed_layout_versions.sql",
                      "014_reader_contracts.sql", "015_parser_revision_contracts.sql"):
        row = conn.execute("SELECT sha256 FROM sec.schema_migrations WHERE name=%s", (migration,)).fetchone()
        if row is None or not migration_checksum_matches(row[0], packaged[migration]):
            raise DatabaseError("Apply the reviewed publication/readers migrations before changing selections.")
    conn.execute("SELECT sec.lock_processing_filing(%s)", (filing_id,))


def served_selection(conn, filing_id):
    row = conn.execute(
        "SELECT processing_family,processing_set_id FROM finance.filing WHERE filing_id=%s",
        (filing_id,),
    ).fetchone()
    if row is None:
        raise DatabaseError("Filing is absent from the serving inventory.")
    return row[0], row[1]


def publish_corrected_set(conn, filing_id, processing_set_id, *, expected_current):
    if not isinstance(processing_set_id, UUID):
        raise DatabaseError("Publication requires a processing-set UUID.")
    _begin(conn, filing_id)
    with conn.transaction():
        current = served_selection(conn, filing_id)
        if current == ("source", processing_set_id):
            return current
        if current != tuple(expected_current):
            raise DataConflict("Serving selection changed since review; publication was not applied.")
        conn.execute(
            """INSERT INTO sec.preferred_source_processing_sets(filing_id,processing_set_id)
               VALUES (%s,%s) ON CONFLICT(filing_id) DO UPDATE
               SET processing_set_id=EXCLUDED.processing_set_id""",
            (filing_id, processing_set_id),
        )
        actual = served_selection(conn, filing_id)
        if actual != ("source", processing_set_id):
            raise DataConflict("Readers did not select the published processing set.")
        return actual


def rollback_corrected_set(conn, filing_id, processing_set_id):
    """Restore the exact previous reader result, or roll back the attempted change."""
    if not isinstance(processing_set_id, UUID):
        raise DatabaseError("Rollback requires the currently published set UUID.")
    _begin(conn, filing_id)
    with conn.transaction():
        if served_selection(conn, filing_id) != ("source", processing_set_id):
            raise DataConflict("The requested set is not the current reader selection.")
        previous = conn.execute(
            """SELECT event_id,old_preference,previous_served_family,previous_served_set_id
               FROM sec.source_publication_history h
               WHERE filing_id=%s AND new_preference=%s AND event_kind='publish'
                 AND NOT EXISTS(SELECT 1 FROM sec.source_publication_history r
                                WHERE r.reverts_event_id=h.event_id)
               ORDER BY event_id DESC LIMIT 1""",
            (filing_id, processing_set_id),
        ).fetchone()
        if previous is None:
            raise DatabaseError("No prior publication evidence is available for rollback.")
        event_id, pointer, family, identifier = previous
        old_context = conn.execute("SELECT current_setting('sec.rollback_publication_event',true)").fetchone()[0]
        conn.execute("SELECT set_config('sec.rollback_publication_event',%s,true)", (str(event_id),))
        if pointer is None:
            conn.execute(
                "DELETE FROM sec.preferred_source_processing_sets WHERE filing_id=%s AND processing_set_id=%s",
                (filing_id, processing_set_id),
            )
        else:
            conn.execute(
                "UPDATE sec.preferred_source_processing_sets SET processing_set_id=%s WHERE filing_id=%s",
                (pointer, filing_id),
            )
        conn.execute("SELECT set_config('sec.rollback_publication_event',%s,true)", (old_context or "",))
        actual = served_selection(conn, filing_id)
        if actual != (family, identifier):
            raise DataConflict("The prior serving baseline changed; rollback was not applied.")
        return actual
