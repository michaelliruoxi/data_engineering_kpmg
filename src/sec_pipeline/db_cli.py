"""Local database setup, metadata ingestion, and status commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from uuid import UUID

import psycopg

from .database import DatabaseError, connect, migrate, seed_manifest, status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path('.env'), help='Local configuration (default: .env).')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('migrate', help='Apply packaged SQL migrations.')
    seed = commands.add_parser('seed-manifest', help='Verify and register the four local sample files; metadata only.')
    seed.add_argument('--manifest', type=Path, default=Path('source_manifest.json'))
    seed.add_argument('--data-root', type=Path, default=Path('.'))
    commands.add_parser('status', help='Show migration state and table counts.')
    processing_set = commands.add_parser('processing-set', help='Record an immutable selection of existing outputs.')
    processing_set.add_argument('--spec', type=Path, required=True, help='ProcessingSetInput fields as JSON; explicit coverage and checks required.')
    processing_set.add_argument('--prefer', action='store_true', help='Select the set for browsing after its checks pass.')
    preferred = commands.add_parser('prefer-processing-set', help='Select an existing validated processing set for browsing.')
    preferred.add_argument('--filing-id', type=UUID, required=True)
    preferred.add_argument('--set-id', type=UUID, required=True)
    args = parser.parse_args(argv)
    try:
        record = None
        if args.command == 'processing-set':
            from .catalog import ProcessingSetInput
            spec = json.loads(args.spec.read_text(encoding='utf-8'))
            if not isinstance(spec, dict):
                raise DatabaseError('Processing-set specification must be a JSON object.')
            for name in ('filing_id', 'fact_source_document_id', 'table_source_document_id', 'report_id'):
                if spec.get(name) is not None:
                    if not isinstance(spec[name], str):
                        raise DatabaseError(f'{name} must be a UUID string.')
                    spec[name] = UUID(spec[name])
            record = ProcessingSetInput(**spec)
        with connect(args.env_file) as conn:
            if args.command == 'migrate':
                result = {'applied': migrate(conn)}
            elif args.command == 'seed-manifest':
                result = seed_manifest(conn, args.manifest, args.data_root)
                result['scope'] = 'metadata_only'
            elif args.command == 'processing-set':
                from .catalog import store_processing_set, select_preferred_processing_set
                set_id = store_processing_set(conn, record)
                if args.prefer:
                    select_preferred_processing_set(conn, record.filing_id, set_id)
                result = {'processing_set_id': set_id, 'preferred': args.prefer}
            elif args.command == 'prefer-processing-set':
                from .catalog import select_preferred_processing_set
                select_preferred_processing_set(conn, args.filing_id, args.set_id)
                result = {'processing_set_id': args.set_id, 'preferred': True}
            else:
                result = status(conn)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get('ready', True) else 1
    except psycopg.Error as exc:
        # Connection exceptions and server details may contain credentials or input values.
        print(f'Database operation failed ({type(exc).__name__}, SQLSTATE {exc.sqlstate or "unavailable"}). '
              'Check Docker readiness, configuration, and the documented input contract.', file=sys.stderr)
    except (DatabaseError, OSError, ValueError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, DatabaseError) else type(exc).__name__
        print(f'Stopped: {message}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
