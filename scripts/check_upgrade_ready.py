"""Read-only receiving-database inventory before migrating or reprocessing.

Records the migration ledger against this checkout, what ``sec-db migrate``
would apply, stored processing sets by recorded parser revision, the served
selections, and the filings whose default source-analysis rows narrow under
migration 012. It never applies a migration and never changes a selection.

Exit 0: nothing pending, or the pending batch has no recorded concern.
Exit 2: an operator decision or a blocker is recorded in the evidence file.
Exit 1: the inventory could not be read; nothing is claimed.
"""

import argparse
import json
from pathlib import Path
import sys

import psycopg

from process_filing import check_connection_target, explicit_database_environment, ingestion_settings
from sec_pipeline.database import connect
from sec_pipeline.upgrade_readiness import assess, read_inventory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--output', type=Path, required=True, help='New evidence file; an existing file is never replaced.')
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('--output must be a new file; earlier evidence is never overwritten.')
    try:
        with explicit_database_environment():
            settings = ingestion_settings(args.env_file, args.database)
            with connect(args.env_file, **settings) as conn:
                check_connection_target(conn, settings)
                with conn.transaction():
                    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    inventory = read_inventory(conn)
        assessment = assess(inventory)
        evidence = {'scope': 'read_only_inventory_not_migration_or_owner_approval',
                    'destination': {key: settings[key] for key in ('host', 'port', 'dbname')},
                    'assessment': assessment, **inventory}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as output:
            output.write(json.dumps(evidence, indent=2, default=str) + '\n')
    except Exception as error:
        # Raw connection exceptions may contain private configuration.
        stopped = {'status': 'stopped', 'error_type': type(error).__name__}
        if isinstance(error, psycopg.Error):
            stopped['sqlstate'] = error.sqlstate
        print(json.dumps(stopped), file=sys.stderr)
        return 1
    print(json.dumps({**assessment, 'output': str(args.output)}, indent=2))
    return 0 if assessment['status'] in {'current', 'ready_to_migrate'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
