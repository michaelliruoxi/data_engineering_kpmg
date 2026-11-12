"""Read-only preflight for a terminal backfill; never apply migrations.

Without options it checks the established October 13 ``tables-v3`` queue. Pass
the same ``--tables-version``, queue, universe and filing-date window that the
import will use to check another profile, for example the reviewed
``tables-v6`` scope. Each profile is checked against the migrations and tables
it actually writes, so a passing result means the import can start.
"""
import argparse
from datetime import date
import ipaddress
from pathlib import Path
import sys

from process_filing import explicit_database_environment, ingestion_settings, check_connection_target
from sec_pipeline.backfill import adapter_profile, open_state
from sec_pipeline.database import connect, migration_checksum_matches
from sec_pipeline.capacity import DEFAULT_MAX_DATABASE_GIB, database_limit_bytes, database_at_limit

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'data/backfill/2026-10-13/queue.json'
UNIVERSE = ROOT / 'data/backfill/2026-10-13/universe.json'
SINCE, UNTIL = '2021-10-13', '2026-10-13'
ENV_FILE = ROOT / '.env.aws'
DATABASE = 'sec_filings'
TABLES_VERSIONS = ('tables-v1', 'tables-v2', 'tables-v3', 'tables-v4', 'tables-v5', 'tables-v6', 'tables-v7', 'tables-v8')
DOCUMENT_SET_VERSIONS = frozenset({'tables-v4', 'tables-v5', 'tables-v6', 'tables-v7', 'tables-v8'})
LEGACY_SCHEMA = 9
TABLES = ('companies', 'filings', 'source_documents', 'financial_facts', 'financial_tables',
          'financial_table_facts', 'table_cells', 'cell_fact_links', 'reports', 'report_sections',
          'chunks', 'filing_processing_sets', 'issuer_successions', 'cell_source_observations')
# Written only by the document-set profiles (migration 008 onward).
SOURCE_SET_TABLES = ('source_document_sets', 'source_document_set_members', 'fact_source_occurrences',
                     'cell_source_fact_bindings', 'source_quality_issues', 'source_processing_sets',
                     'source_processing_tables', 'source_processing_reports', 'source_processing_issues')


class PreflightStop(ValueError):
    """A stop whose message is written by this script and is safe to print."""


def _iso_date(value):
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError('Dates must be YYYY-MM-DD.')
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--max-database-gib', type=int, default=DEFAULT_MAX_DATABASE_GIB,
                        help='Same database-size ceiling used by ingestion (default: 14 GiB).')
    parser.add_argument('--tables-version', choices=TABLES_VERSIONS, default='tables-v3',
                        help='Profile the import will select (default: tables-v3).')
    parser.add_argument('--state', type=Path, help='Queue file (default: the October 13 queue).')
    parser.add_argument('--universe', type=Path, help='Universe snapshot bound to that queue.')
    parser.add_argument('--since', type=_iso_date, help='Inclusive filing date of the queue window.')
    parser.add_argument('--until', type=_iso_date, help='Inclusive filing date of the queue window.')
    parser.add_argument('--env-file', type=Path, help='Database environment file (default: .env.aws).')
    parser.add_argument('--database', help='Destination database name (default: sec_filings).')
    args = parser.parse_args(argv)
    try:
        database_limit_bytes(args.max_database_gib)
    except ValueError as error:
        parser.error(str(error))
    scope = (args.state, args.universe, args.since, args.until)
    if any(value is not None for value in scope) and any(value is None for value in scope):
        parser.error('--state, --universe, --since and --until select one queue; give all four or none.')
    return args


def required_schema(profile):
    """Highest packaged migration number the selected profile depends on."""
    return max([LEGACY_SCHEMA, *(int(name[:3]) for name in getattr(profile, 'MIGRATIONS', ()))])


def _loopback(host):
    if str(host).lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv=None):
    args = parse_args(argv)
    version = args.tables_version
    # Read the module attributes at call time so the defaults stay one definition.
    state_path, universe_path = args.state or STATE, args.universe or UNIVERSE
    since, until = args.since or SINCE, args.until or UNTIL
    env_file, database = args.env_file or ENV_FILE, args.database or DATABASE
    try:
        if sys.version_info[:2] != (3, 12):
            raise PreflightStop('Use the existing project Python 3.12 environment.')
        if not state_path.is_file() or not universe_path.is_file():
            raise PreflightStop('The existing queue and universe are required.')
        if Path(str(state_path) + '.lock').exists():
            raise PreflightStop('The queue lock exists; establish that its owner stopped before recovering it.')
        state, _ = open_state(state_path, universe_path, since, until)
        profile, _ = adapter_profile(version)
        if profile.VERSIONS['tables'] != version:
            raise PreflightStop('The selected parser is unavailable.')
        schema = required_schema(profile)
        tables = TABLES + (SOURCE_SET_TABLES if version in DOCUMENT_SET_VERSIONS else ())
        with explicit_database_environment():
            settings = ingestion_settings(env_file, database)
            if settings.get('sslmode') != 'verify-full' and not _loopback(settings.get('host')):
                raise PreflightStop('The established database configuration must retain verify-full TLS.')
            with connect(env_file, **settings) as conn:
                check_connection_target(conn, settings)
                conn.execute('SET TRANSACTION READ ONLY')
                applied = dict(conn.execute('SELECT name,sha256 FROM sec.schema_migrations').fetchall())
                migrations = sorted((ROOT / 'src/sec_pipeline/migrations').glob('*.sql'))
                required = {p.name: p.read_bytes() for p in migrations if p.name[:3].isdigit() and int(p.name[:3]) <= schema}
                if len(required) != schema:
                    raise PreflightStop(f'This checkout does not package migrations 001 through {schema:03d}.')
                pending = sorted(name for name in required if name not in applied)
                changed = sorted(name for name, content in required.items()
                                 if name in applied and not migration_checksum_matches(applied[name], content))
                if changed:
                    raise PreflightStop('Applied migrations differ from this checkout: ' + ', '.join(changed)
                                        + '. Do not import until the difference is explained.')
                if pending:
                    raise PreflightStop(f'{version} needs migrations 001 through {schema:03d}; not yet applied: '
                                        + ', '.join(pending) + '. No import was started. Apply the pending batch with '
                                        'sec-db migrate only after the documented backup and rehearsal '
                                        '(docs/aws-deployment.md).')
                for table in tables:
                    for privilege in ('SELECT', 'INSERT'):
                        if not conn.execute('SELECT has_table_privilege(current_user,%s,%s)', ('sec.' + table, privilege)).fetchone()[0]:
                            raise PreflightStop('The configured database identity lacks importer privileges.')
                size = conn.execute('SELECT pg_database_size(current_database())').fetchone()[0]
        if database_at_limit(size, database_limit_bytes(args.max_database_gib)):
            print(f'The {args.max_database_gib} GiB cutoff is reached (database {size / 1024**3:.2f} GiB). No import was started.')
            return 2
        print(f'Read-only preflight passed: {version}; schema 001-{schema:03d}; {len(state["filings"]):,} queued filings; database {size / 1024**3:.2f} GiB; limit {args.max_database_gib} GiB.')
        print('Instance free storage is NOT measured by this database-size guard; verify recent RDS FreeStorageSpace before launch (docs/automated-backfill.md).')
        return 0
    except PreflightStop as stop:
        print(f'Preflight stopped: {stop}', file=sys.stderr)
        return 1
    except Exception as exc:
        # Never echo a connection exception or environment-file values.
        print(f'Preflight stopped ({type(exc).__name__}). Check the queue lock, project environment, .env.aws, TLS certificate, network access and database permissions.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
