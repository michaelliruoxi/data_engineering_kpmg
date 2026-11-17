"""Read-only receiving-database status; never mutate queue, selections or receipts."""
import argparse
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from sec_pipeline.backfill_status import coverage_summary, read_receipts, reconcile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--offline', action='store_true', help='DB counts remain unverified; never trust queue imports')
    parser.add_argument('--select', action='append', default=[], metavar='ACCESSION=FAMILY:UUID')
    parser.add_argument('--summary', type=Path,
                        help='Also write stored and missing counts by filing year, form and issuer, and print the totals.')
    args = parser.parse_args(argv)
    queue = json.loads(args.queue.read_text())
    overrides = {}
    for value in args.select:
        accession, scope = value.split('=', 1)
        family, identifier = scope.split(':', 1)
        if accession in overrides or family not in {'legacy', 'source'} or not identifier:
            parser.error('Selections must be unique ACCESSION=legacy|source:UUID values')
        overrides[accession] = (family, identifier)
    if args.offline and overrides:
        parser.error('Selection ownership cannot be verified offline')
    if args.offline:
        report = reconcile(queue)
    else:
        try:
            # Standard libpq PG* / service / passfile configuration; no secret flags.
            with psycopg.connect(row_factory=dict_row, connect_timeout=10,
                                 options='-c default_transaction_read_only=on -c statement_timeout=60000') as conn:
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                receipts, selections, observed = read_receipts(conn)
                report = reconcile(queue, receipts, selections, observed_at=observed, overrides=overrides)
        except psycopg.Error as error:
            report = reconcile(queue)
            report['blocker'] = {'error_type': type(error).__name__, 'sqlstate': error.sqlstate,
                                 'detail': 'Database read failed; connection details withheld'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + '\n')
    if args.summary:
        summary = coverage_summary(queue, report)
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, indent=2, default=str) + '\n')
        print(json.dumps({key: summary[key] for key in
                          ('observed_at_utc', 'database_verified', 'scope', 'filings', 'issuers',
                           'database_filings_outside_queue')}, indent=2))
    return 0 if report['database_verified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
