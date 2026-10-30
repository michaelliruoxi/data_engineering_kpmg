"""Discover and resume an S&P 500 filing backfill with explicit database writes."""

import argparse
from sec_pipeline.capacity import database_limit_bytes
from contextlib import ExitStack
from datetime import date
import json
from pathlib import Path
import sys

from sec_pipeline.acquisition import AccessBlocked, SecClient
from sec_pipeline.backfill import discover_queue, open_state, process_queue, queue_lock, refresh_discovery, summarize
from sec_pipeline.database import connect
from sec_pipeline.universe import fetch_snapshot
from process_filing import check_connection_target, explicit_database_environment, ingestion_settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ("discover", "download", "dry-run", "ingest", "status"):
        mode.add_argument("--" + name, action="store_true")
    parser.add_argument("--universe", type=Path, required=True, help="Dated JSON snapshot; create from current constituent list if missing.")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--since", type=date.fromisoformat, required=True, help="Inclusive filing date, YYYY-MM-DD.")
    parser.add_argument("--until", type=date.fromisoformat, required=True, help="Inclusive filing date, YYYY-MM-DD.")
    parser.add_argument("--data-root", type=Path, default=Path("."))
    parser.add_argument("--company-limit", type=int, help="Maximum undiscovered companies this invocation.")
    parser.add_argument("--limit", type=int, help="Maximum filing attempts this invocation.")
    parser.add_argument("--offline", action="store_true", help="Process only already archived sources, without network access.")
    parser.add_argument("--replay", action="store_true", help="Revalidate and replay even existing sealed database imports.")
    parser.add_argument("--retry-failed-only", action="store_true", help="Retry failed/blocked filings after fixing their cause; leave pending and successful entries alone.")
    parser.add_argument("--validated-only", action="store_true", help="Ingest only entries with a successful dry run; reverify their sources and every import check before commit.")
    parser.add_argument("--max-database-gib", type=int, help="Stop before the next filing import once this database reaches the size limit; retain the queue for resumption.")
    parser.add_argument("--tables-version", choices=("tables-v1", "tables-v2", "tables-v3", "tables-v4"), default="tables-v1", help="Select the table extraction profile and its separate processing-set version; v4 includes complete inline document sets and explicit source-quality issues.")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--database")
    parser.add_argument("--issuer-history", type=Path, help="Verified predecessor mapping; add it with --discover, then the queue remembers its path and hash.")
    parser.add_argument("--refresh-discovery", action="store_true", help="Begin a fresh SEC submissions pass while preserving downloaded and imported filings; resume interrupted passes without this flag.")
    args = parser.parse_args(argv)
    if any(value is not None and value < 1 for value in (args.limit, args.company_limit)):
        parser.error("Limits must be positive integers.")
    if args.ingest and (args.env_file is None or not args.database):
        parser.error("--ingest requires --env-file and --database.")
    if args.offline and args.discover:
        parser.error("Discovery requires SEC access.")
    if args.refresh_discovery and not args.discover:
        parser.error("--refresh-discovery requires --discover.")
    if args.validated_only and (not args.ingest or args.retry_failed_only):
        parser.error("--validated-only requires --ingest and cannot be combined with --retry-failed-only.")
    if args.max_database_gib is not None and (not args.ingest or args.max_database_gib < 1):
        parser.error("--max-database-gib requires --ingest and a positive integer.")
    try:
        with ExitStack() as stack:
            if not args.status:
                stack.enter_context(queue_lock(args.state))
            elif not args.state.is_file():
                raise ValueError("The queue must exist before querying status.")
            if not args.universe.exists():
                if args.offline or args.status:
                    raise ValueError("The membership snapshot must exist for offline/status use.")
                fetch_snapshot(args.universe)
            state, universe = open_state(args.state, args.universe, args.since, args.until,
                issuer_history_path=args.issuer_history, allow_history_extension=args.discover)
            client = None
            if not args.status and not args.offline:
                client = stack.enter_context(SecClient())
            history_records = []
            if not args.status and universe.get("issuer_history"):
                from sec_pipeline.issuer_history import archive_history_evidence
                history_records = archive_history_evidence(client, universe["issuer_history"], args.data_root)
            if args.discover:
                if args.refresh_discovery:
                    refresh_discovery(args.state, state)
                discover_queue(args.state, state, universe, client, company_limit=args.company_limit)
            elif not args.status:
                connection, destination = None, None
                if args.ingest:
                    stack.enter_context(explicit_database_environment())
                    settings = ingestion_settings(args.env_file, args.database)
                    connection = connect(args.env_file, **settings)
                    stack.callback(connection.close)
                    check_connection_target(connection, settings)
                    destination = {key: settings[key] for key in ("host", "port", "dbname")}
                    if history_records:
                        from sec_pipeline.issuer_history_storage import store_issuer_history
                        with connection.transaction():
                            stored_history = store_issuer_history(connection, history_records)
                        print(json.dumps({"event": "issuer_history_stored", "relationships": len(stored_history)}))
                operation = "ingest" if args.ingest else "download" if args.download else "dry-run"
                process_queue(args.state, state, client, args.data_root, mode=operation,
                    connection=connection, destination=destination, limit=args.limit, replay=args.replay,
                    retry_failed_only=args.retry_failed_only, validated_only=args.validated_only,
                    max_database_bytes=(database_limit_bytes(args.max_database_gib) if args.max_database_gib is not None else None),
                    tables_version=args.tables_version)
            summary = summarize(state, universe)
            print(json.dumps(summary, indent=2))
            if args.status:
                return 0
            complete = summary["companies"].get("discovered", 0) == summary["companies_expected"]
            complete = complete and summary["predecessor_jobs"].get("discovered", 0) == summary["predecessor_jobs_expected"]
            if not args.discover:
                complete = complete and state.get("last_run", {}).get("complete") is True
            return 0 if complete else 2
    except KeyboardInterrupt:
        print("Import interrupted. Completed filings remain committed; resume from the saved checkpoint.", file=sys.stderr)
        return 130
    except AccessBlocked:
        print("SEC access was blocked; the checkpoint was preserved. No further SEC requests were made.", file=sys.stderr)
        return 3
    except Exception as exc:
        # Raw connection/HTTP exceptions may contain private configuration.
        print(f"Backfill stopped ({type(exc).__name__}); check configuration and checkpoint status.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
