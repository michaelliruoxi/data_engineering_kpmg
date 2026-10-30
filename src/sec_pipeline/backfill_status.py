"""Read-only queue/receiving-database reconciliation; one row per accession.

Sealed receipt metadata is not independent original-source acceptance. A finance
view selection is an explicit browsing policy, never owner approval.
"""
from collections import Counter
from datetime import datetime, timezone

RECEIPTS_SQL = """
SELECT f.accession_number, 'legacy' AS processing_family, p.id AS processing_set_id,
 p.set_key, p.validation_status, NULL::text AS source_validation_status,
 p.coverage, p.counts,
 jsonb_build_object('facts',p.fact_extraction_version,'tables',p.table_extraction_version,
                    'reports',r.extraction_version,'chunks',p.chunking_version) AS versions
FROM sec.filing_processing_sets p JOIN sec.filings f ON f.id=p.filing_id
LEFT JOIN sec.reports r ON r.id=p.report_id
UNION ALL
SELECT f.accession_number, 'source', p.id, p.set_key, p.validation_status,
 p.source_validation_status, p.coverage, p.counts, p.versions
FROM sec.source_processing_sets p JOIN sec.filings f ON f.id=p.filing_id
ORDER BY accession_number, processing_family, processing_set_id
"""


def read_receipts(conn):
    """Caller must open a read-only repeatable-read transaction before any query."""
    if conn.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'] != 'on':
        raise ValueError('Read-only transaction required')
    observed = conn.execute('SELECT transaction_timestamp() AS observed_at').fetchone()['observed_at']
    receipts = conn.execute(RECEIPTS_SQL).fetchall()
    selections = conn.execute('SELECT accession_number, processing_family, processing_set_id, selection_reason FROM finance.filing').fetchall()
    return receipts, selections, observed.isoformat()


def receipt_status(receipt):
    if receipt['validation_status'] != 'passed' or receipt.get('source_validation_status') not in (None, 'passed'):
        return 'review_required'
    counts, coverage = receipt.get('counts', {}), receipt.get('coverage', {})
    present = lambda key: type(counts.get(key)) is int and counts[key] > 0
    if all(present(key) for key in ('facts', 'reports', 'chunks')):
        if (coverage.get('tables') == 'absent' and counts.get('tables') == 0
                and all(coverage.get(key) == 'complete' for key in ('facts', 'reports', 'chunks'))):
            return 'text_only'
        if present('tables') and all(coverage.get(key) == 'complete' for key in ('facts', 'tables', 'reports', 'chunks')):
            return 'fully_imported'
    return 'review_required'


def reconcile(queue, receipts=None, selections=(), *, observed_at=None, overrides=None):
    """None receipts means unavailable DB, not an empty receiving database."""
    entries = queue.get('filings', {})
    by_accession = {}
    for receipt in receipts or ():
        by_accession.setdefault(receipt['accession_number'], []).append(receipt)
    selected = {s['accession_number']: s for s in selections}
    overrides = overrides or {}
    rows = []
    for accession in sorted(set(entries) | set(by_accession)):
        entry = entries.get(accession, {})
        candidates = by_accession.get(accession, [])
        choice = selected.get(accession, {})
        selection_reason = choice.get('selection_reason', 'no_browsing_selection')
        if accession in overrides:
            family, identifier = overrides[accession]
            choice = {'processing_family': family, 'processing_set_id': identifier}
            selection_reason = 'explicit_report_override_not_approval'
        matches = [r for r in candidates if (r['processing_family'], str(r['processing_set_id'])) ==
                   (choice.get('processing_family'), str(choice.get('processing_set_id')))]
        if accession in overrides and len(matches) != 1:
            raise ValueError('Selected processing set does not uniquely belong to accession')
        receipt = matches[0] if len(matches) == 1 else None
        issues = []
        if receipts is None:
            status = 'unverified'
        elif receipt:
            status = receipt_status(receipt)
        elif candidates:
            status = 'review_required'
            issues.append('stored_candidates_without_unique_selection')
        elif entry.get('status') in ('failed', 'access_blocked'):
            status = 'failed'
        else:
            status = 'pending'
        if receipts is not None and entry.get('status', '').startswith('ingested') and not candidates:
            issues.append('queue_claims_import_but_no_database_receipt')
        if candidates and accession not in entries:
            issues.append('database_receipt_not_in_queue')
        rows.append({'accession_number': accession, 'status': status,
                     'queue_status': entry.get('status'), 'selection_reason': selection_reason,
                     'selected_receipt': receipt, 'processing_candidates': candidates, 'issues': issues})
    unknown_overrides = set(overrides) - {row['accession_number'] for row in rows}
    if unknown_overrides:
        raise ValueError('Selection accession absent from queue and database')
    return {'observed_at_utc': observed_at or datetime.now(timezone.utc).isoformat(),
            'database_verified': receipts is not None,
            'evidence_kind': 'sealed_receipt_metadata_not_original_source_acceptance',
            'selection_policy': 'finance.filing_browsing_policy_or_explicit_override_not_owner_approval',
            'count_grain': 'distinct_accession_not_processing_version',
            'queue_filings': len(entries),
            'database_filings_with_receipts': len(by_accession) if receipts is not None else None,
            'counts': dict(sorted(Counter(row['status'] for row in rows).items())), 'filings': rows}
