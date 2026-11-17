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
        # Complete *ingestion* is distinct from exhaustive semantic coverage:
        # the parser intentionally reports primary-statement tables as partial
        # and section classification as unknown even for fully stored outputs.
        if (present('tables') and coverage.get('facts') == 'complete'
                and coverage.get('chunks') == 'complete'
                and coverage.get('tables') in {'complete', 'partial'}
                and coverage.get('reports') in {'complete', 'unknown'}
                and not any(counts.get(key, 0) for key in
                            ('quality_issues', 'review_required_cells', 'unaccounted_cells'))):
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
        if entry.get('receipt_status') == 'stale_receipt' or entry.get('status') == 'stale_receipt':
            issues.append('stale_receipt_requires_versioned_upgrade')
            # A historical passed receipt proves prior ingestion, not acceptance
            # by the current parser. Offline reports remain explicitly unverified.
            if receipts is not None:
                status = 'review_required'
        if receipts is not None and entry.get('status', '').startswith('ingested') and not candidates:
            issues.append('queue_claims_import_but_no_database_receipt')
        if candidates and accession not in entries:
            issues.append('database_receipt_not_in_queue')
        rows.append({'accession_number': accession, 'status': status,
                     'queue_status': entry.get('status'), 'queue_receipt_status': entry.get('receipt_status'),
                     'selection_reason': selection_reason,
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


STORED_STATUSES = ('fully_imported', 'text_only', 'review_required')
MISSING_STATUSES = ('pending', 'failed')


def coverage_summary(queue, report):
    """Roll a reconciliation report up by filing year, form and issuer.

    The scope is the queue. A filing counts as stored when the receiving
    database holds a receipt for it, whatever its review status; `pending` and
    `failed` filings are missing. An accession the database holds outside the
    queue is counted on its own line and never as queue coverage. An offline
    report has no stored or missing filings, only `unverified` ones.
    """
    entries = queue.get('filings', {})
    statuses = {row['accession_number']: row['status'] for row in report['filings']}
    if set(entries) - set(statuses):
        raise ValueError('The report does not cover every queued accession')
    totals, periods, issuers = Counter(), {}, {}
    for accession, entry in entries.items():
        filing, status = entry['filing'], statuses[accession]
        totals[status] += 1
        periods.setdefault((filing['filing_date'][:4], filing['form']), Counter())[status] += 1
        issuer = issuers.setdefault(filing['cik'], {'company': filing.get('company'), 'counts': Counter()})
        issuer['counts'][status] += 1

    def line(counts):
        return {'queued': sum(counts.values()),
                'stored': sum(counts[status] for status in STORED_STATUSES),
                'missing': sum(counts[status] for status in MISSING_STATUSES),
                **{status: counts[status] for status in sorted(counts)}}

    issuer_rows = sorted(({'cik': cik, 'company': issuer['company'], **line(issuer['counts'])}
                          for cik, issuer in issuers.items()),
                         key=lambda row: (-row['missing'], row['cik']))
    verified = report['database_verified']
    count = lambda matches: sum(matches(row) for row in issuer_rows) if verified else None
    return {'observed_at_utc': report['observed_at_utc'], 'database_verified': verified,
            'scope': {key: queue.get('configuration', {}).get(key) for key in ('since', 'until', 'forms')},
            'count_grain': report['count_grain'],
            'filings': line(totals),
            'by_filing_year_and_form': [{'filing_year': int(year), 'form': form, **line(counts)}
                                        for (year, form), counts in sorted(periods.items())],
            'issuers': {'with_queued_filings': len(issuer_rows),
                        'all_stored': count(lambda row: row['stored'] == row['queued']),
                        'partly_stored': count(lambda row: 0 < row['stored'] < row['queued']),
                        'none_stored': count(lambda row: row['stored'] == 0)},
            'issuer_rows': issuer_rows,
            'database_filings_outside_queue': (sum('database_receipt_not_in_queue' in row['issues']
                                                   for row in report['filings']) if verified else None)}
