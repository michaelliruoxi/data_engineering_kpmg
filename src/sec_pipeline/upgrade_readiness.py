"""Read-only inventory of a receiving database before a migration or reprocessing.

Nothing here applies a migration, changes a selection or writes a row. The plan
predicts what ``database.migrate`` would do from the recorded ledger alone, and
the receipt inventory counts stored sets by the parser revisions they recorded.
"""

from .database import _packaged_migrations, migration_checksum_matches
from .parser_policy import policy_metadata


READER_IMPACT_SQL = """
SELECT count(*) AS legacy_served_filings_with_passed_source_set,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM sec.source_processing_sets v4
           WHERE v4.filing_id = served.filing_id
             AND v4.validation_status = 'passed'
             AND v4.versions->>'tables' = 'tables-v4'
       )) AS legacy_served_filings_with_passed_v4_set
FROM finance.filing served
WHERE served.processing_family = 'legacy'
  AND EXISTS (
      SELECT 1 FROM sec.source_processing_sets source_set
      WHERE source_set.filing_id = served.filing_id
        AND source_set.validation_status = 'passed'
  )
"""

LEGACY_RECEIPTS_SQL = """
SELECT p.set_key, p.fact_extraction_version, p.table_extraction_version, r.extraction_version,
       p.chunking_version, p.validation_status, p.configuration->'parser_revisions', count(*)
FROM sec.filing_processing_sets p LEFT JOIN sec.reports r ON r.id = p.report_id
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY 1, 2, 3, 4, 5, 6
"""

SOURCE_RECEIPTS_SQL = """
SELECT p.set_key, p.versions, p.validation_status, p.configuration->'parser_revisions', count(*)
FROM sec.source_processing_sets p
GROUP BY 1, 2, 3, 4
ORDER BY 1, 3
"""


def migration_plan(applied, packaged=None):
    """Predict ``database.migrate`` for a recorded ledger without running it.

    ``applied`` maps recorded migration names to their recorded digests. The
    three blocking conditions are exactly the ones ``migrate`` raises on.
    """
    packaged = _packaged_migrations() if packaged is None else list(packaged)
    names = [name for name, _ in packaged]
    pending = [name for name in names if name not in applied]
    changed = [name for name, content in packaged
               if name in applied and not migration_checksum_matches(applied[name], content)]
    unknown = sorted(set(applied) - set(names))
    first_pending = pending[0] if pending else None
    after_pending = [name for name in names if name in applied and first_pending is not None and name > first_pending]
    blockers = []
    if unknown:
        blockers.append('database_has_migrations_absent_from_this_checkout')
    if changed:
        blockers.append('applied_migration_checksum_differs')
    if after_pending:
        blockers.append('applied_history_is_not_a_contiguous_prefix')
    return {'packaged': names, 'applied': [name for name in names if name in applied], 'pending': pending,
            'checksum_mismatches': changed, 'unknown_applied': unknown, 'applied_after_pending': after_pending,
            'blockers': blockers,
            'status': 'blocked' if blockers else 'pending' if pending else 'current'}


def _exists(conn, relation):
    return conn.execute('SELECT to_regclass(%s)', (relation,)).fetchone()[0] is not None


def _receipt(set_key, versions, status, revisions, count):
    expected = policy_metadata(versions).get('parser_revisions')
    return {'set_key': set_key, 'versions': versions, 'validation_status': status,
            'recorded_parser_revisions': revisions, 'current_parser_revisions': expected,
            # A set that predates revision receipts, or recorded an older one,
            # cannot be replayed under its identity by this checkout's parser.
            'stale_for_this_checkout': revisions != expected, 'processing_sets': count}


def read_inventory(conn):
    """Read everything inside the caller's READ ONLY transaction (tuple rows)."""
    if conn.execute('SHOW transaction_read_only').fetchone()[0] != 'on':
        raise ValueError('Read-only transaction required')
    result = {'observed_at_utc': conn.execute('SELECT transaction_timestamp()').fetchone()[0].isoformat(),
              'database': conn.info.dbname, 'server_version': conn.info.server_version,
              'database_size_bytes': conn.execute('SELECT pg_database_size(current_database())').fetchone()[0],
              'active_other_sessions': conn.execute(
                  "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                  "AND pid<>pg_backend_pid() AND state<>'idle'").fetchone()[0],
              'reader_role_exists': conn.execute(
                  "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='sec_reader')").fetchone()[0]}
    ledger = {}
    if _exists(conn, 'sec.schema_migrations'):
        ledger = dict(conn.execute('SELECT name, sha256 FROM sec.schema_migrations ORDER BY name').fetchall())
    result['migration_ledger'] = ledger
    result['migrations'] = migration_plan(ledger)

    has_legacy = _exists(conn, 'sec.filing_processing_sets') and _exists(conn, 'sec.reports')
    has_source = _exists(conn, 'sec.source_processing_sets')
    has_serving = _exists(conn, 'finance.filing')
    receipts = []
    if has_legacy:
        for set_key, facts, tables, reports, chunks, status, revisions, count in conn.execute(LEGACY_RECEIPTS_SQL).fetchall():
            versions = {key: value for key, value in
                        (('facts', facts), ('tables', tables), ('reports', reports), ('chunks', chunks)) if value}
            receipts.append({'processing_family': 'legacy', **_receipt(set_key, versions, status, revisions, count)})
    if has_source:
        for set_key, versions, status, revisions, count in conn.execute(SOURCE_RECEIPTS_SQL).fetchall():
            receipts.append({'processing_family': 'source', **_receipt(set_key, versions, status, revisions, count)})
    result['receipts'] = receipts if has_legacy or has_source else None
    result['served_selection_counts'] = (
        {family or 'none': count for family, count in conn.execute(
            'SELECT processing_family, count(*) FROM finance.filing GROUP BY 1 ORDER BY 1').fetchall()}
        if has_serving else None)
    # Migration 012 narrows the default source-analysis readers to the served
    # set. These filings lose their older source rows there until republished.
    if has_serving and has_source:
        total, v4 = conn.execute(READER_IMPACT_SQL).fetchone()
        result['reader_impact'] = {'status': 'counted', 'legacy_served_filings_with_passed_source_set': total,
                                   'legacy_served_filings_with_passed_v4_set': v4}
    else:
        result['reader_impact'] = {'status': 'not_available',
                                   'reason': 'requires finance.filing (migration 010) and sec.source_processing_sets (008)'}
    return result


def assess(inventory):
    """Summarize what an operator must settle before migrating; no approval implied."""
    plan = inventory['migrations']
    attention = list(plan['blockers'])
    reader = inventory['reader_impact']
    narrows_readers = '012_publication_readers.sql' in plan['pending']
    if narrows_readers and reader['status'] != 'counted':
        attention.append('reader_impact_not_counted_before_migration_012')
    if narrows_readers and reader.get('legacy_served_filings_with_passed_source_set'):
        attention.append('default_source_readers_lose_rows_for_legacy_served_filings')
    if plan['pending'] and inventory['active_other_sessions']:
        attention.append('other_sessions_active')
    if plan['pending'] and not inventory['reader_role_exists']:
        attention.append('sec_reader_role_absent_so_reader_grants_are_skipped')
    stale = sum(row['processing_sets'] for row in inventory['receipts'] or () if row['stale_for_this_checkout'])
    return {'status': 'blocked' if plan['blockers'] else 'review_required' if attention else
                      'ready_to_migrate' if plan['pending'] else 'current',
            'attention': attention, 'pending_migrations': plan['pending'],
            'stale_processing_sets': stale if inventory['receipts'] is not None else None}
