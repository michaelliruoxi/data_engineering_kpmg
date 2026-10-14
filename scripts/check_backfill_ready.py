"""Read-only preflight for the existing terminal backfill; never apply migrations."""
import hashlib
from pathlib import Path
import sys

from process_filing import explicit_database_environment, ingestion_settings, check_connection_target
from sec_pipeline.backfill import adapter_profile, open_state
from sec_pipeline.database import connect

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'data/backfill/2026-10-13/queue.json'
UNIVERSE = ROOT / 'data/backfill/2026-10-13/universe.json'
TABLES = ('companies', 'filings', 'source_documents', 'financial_facts', 'financial_tables',
          'financial_table_facts', 'table_cells', 'cell_fact_links', 'reports', 'report_sections',
          'chunks', 'filing_processing_sets', 'issuer_successions', 'cell_source_observations')


def main():
    try:
        if sys.version_info[:2] != (3, 12):
            raise ValueError('Use the existing project Python 3.12 environment.')
        if not STATE.is_file() or not UNIVERSE.is_file():
            raise ValueError('The existing queue and universe are required.')
        if Path(str(STATE) + '.lock').exists():
            raise ValueError('The queue lock exists; establish that its owner stopped before recovering it.')
        state, _ = open_state(STATE, UNIVERSE, '2021-10-13', '2026-10-13')
        profile, _ = adapter_profile('tables-v3')
        if profile.VERSIONS['tables'] != 'tables-v3':
            raise ValueError('The selected parser is unavailable.')
        with explicit_database_environment():
            settings = ingestion_settings(ROOT / '.env.aws', 'sec_filings')
            if settings.get('sslmode') != 'verify-full':
                raise ValueError('The established database configuration must retain verify-full TLS.')
            with connect(ROOT / '.env.aws', **settings) as conn:
                check_connection_target(conn, settings)
                conn.execute('SET TRANSACTION READ ONLY')
                applied = dict(conn.execute('SELECT name,sha256 FROM sec.schema_migrations').fetchall())
                migrations = sorted((ROOT / 'src/sec_pipeline/migrations').glob('*.sql'))
                required = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in migrations if p.name[:3].isdigit() and int(p.name[:3]) <= 9}
                if len(required) != 9 or any(applied.get(name) != digest for name, digest in required.items()):
                    raise ValueError('Applied schema differs from the tested migrations 001 through 009.')
                for table in TABLES:
                    for privilege in ('SELECT', 'INSERT'):
                        if not conn.execute('SELECT has_table_privilege(current_user,%s,%s)', ('sec.' + table, privilege)).fetchone()[0]:
                            raise ValueError('The configured database identity lacks importer privileges.')
                size = conn.execute('SELECT pg_database_size(current_database())').fetchone()[0]
        print(f'Read-only preflight passed: tables-v3; schema 001-009; {len(state["filings"]):,} queued filings; database {size / 1024**3:.2f} GiB.')
        if size >= 14 * 1024**3:
            print('The 14 GiB cutoff is reached. No import was started.')
            return 2
        return 0
    except Exception as exc:
        # Never echo a connection exception or environment-file values.
        print(f'Preflight stopped ({type(exc).__name__}). Check the queue lock, project environment, .env.aws, TLS certificate, network access and database permissions.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
