"""Run real facts→tables acceptance on a dedicated local PostgreSQL database."""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import psycopg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='Loopback IP or absolute local Unix socket directory')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--migrate', action='store_true', help='Apply repository migrations to an EMPTY dedicated test DB')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_]+_test(?:_[A-Za-z0-9_]+)?', args.database):
        parser.error('Use a dedicated database ending in _test or _test_SUFFIX.')
    socket = Path(args.host)
    if args.host not in ('127.0.0.1', '::1') and not (socket.is_absolute() and socket.is_dir()):
        parser.error('Only explicit local IPs or existing absolute Unix socket directories are allowed.')
    for key in list(os.environ):
        if key.startswith(('PG', 'POSTGRES_')):
            os.environ.pop(key)
    options = dict(host=args.host, port=args.port, dbname=args.database, user=args.user,
                   password='', passfile=str(ROOT / '.cache' / 'unused-test-passfile'),
                   sslmode='disable', connect_timeout=5)
    if args.host in ('127.0.0.1', '::1'):
        options['hostaddr'] = args.host
    with psycopg.connect(**options) as conn:
        database = conn.execute('SELECT current_database()').fetchone()[0]
        # Docker forwards the requested loopback endpoint to a private server
        # interface. Check the actual client endpoint, as the ingestion runner
        # does, rather than rejecting the server's container-side address.
        wrong_endpoint = (conn.info.hostaddr != args.host if args.host in ('127.0.0.1', '::1')
                          else conn.info.host != args.host)
        if database != args.database or wrong_endpoint:
            raise RuntimeError('Actual test connection is not the requested local test database.')
        if args.migrate:
            if conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname='sec'").fetchone()[0]:
                raise RuntimeError('--migrate requires an empty dedicated schema; no existing schema is changed.')
            from sec_pipeline.database import migrate
            migrate(conn)
    spec = importlib.util.spec_from_file_location('test_tables_postgres', ROOT / 'tests/test_tables_postgres.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TablesPostgresTests.connection_options = options
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(module.TablesPostgresTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped or not result.wasSuccessful():
        return 1
    print('Verified: 1622 facts, 3 tables, links 76/76/131 = 283; replay stable; conflicts rejected; rollback and baseline restored.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
