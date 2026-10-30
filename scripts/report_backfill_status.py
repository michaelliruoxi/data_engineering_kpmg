"""Read-only receiving-database status; never mutate queue, selections or receipts."""
import argparse
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from sec_pipeline.backfill_status import read_receipts, reconcile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--offline', action='store_true', help='DB counts remain unverified; never trust queue imports')
    parser.add_argument('--select', action='append', default=[], metavar='ACCESSION=FAMILY:UUID')
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
    return 0 if report['database_verified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
