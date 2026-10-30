"""Receipt revisions for parsing changes within existing storage contracts."""


def policy_metadata(versions):
    revisions = {}
    if versions.get('reports') == 'report-v1':
        revisions['reports'] = 'visible-table-rows-v2'
    if versions.get('tables') in {'tables-v2', 'tables-v3', 'tables-v4'}:
        revisions['tables'] = 'logical-headers-v1'
    return {'parser_revisions': revisions} if revisions else {}


def require_current_receipt(conn, filing_id, set_key, versions, *, document_set=False):
    """Reject older same-set receipts before any parser stage writes records.

    A new receipt policy does not create new storage identities. Existing data
    needs an explicit versioned migration; this guard never deletes or updates it.
    """
    expected = policy_metadata(versions)
    if not expected or set_key is None:
        return
    table = 'source_processing_sets' if document_set else 'filing_processing_sets'
    row = conn.execute(f'SELECT configuration FROM sec.{table} WHERE filing_id=%s AND set_key=%s',
                       (filing_id, set_key)).fetchone()
    if row is not None and (not isinstance(row[0], dict)
                            or row[0].get('parser_revisions') != expected['parser_revisions']):
        from .processing import ParserRevisionConflict
        raise ParserRevisionConflict(
            'Stored processing set uses an older parser revision. Existing report/table records were not changed; '
            'reprocessing requires explicit new storage versions or a reviewed data migration.')
