"""Local database setup, metadata ingestion, and status commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

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
    args = parser.parse_args(argv)
    try:
        with connect(args.env_file) as conn:
            if args.command == 'migrate':
                result = {'applied': migrate(conn)}
            elif args.command == 'seed-manifest':
                result = seed_manifest(conn, args.manifest, args.data_root)
                result['scope'] = 'metadata_only'
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
