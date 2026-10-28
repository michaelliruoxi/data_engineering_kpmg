"""Read-only batch runner. Credentials are obtained only through existing libpq settings or hidden input."""
import argparse
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from sec_pipeline.historical_table_validation import (COMPANIES, connection_failure, download_required_sources, parse_selections,
    read_inventory, run_validation, source_requirements)
from sec_pipeline.table_relations import read_snapshot
from sec_pipeline.table_relations_cli import save_json, shared_options


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", nargs="+", choices=sorted(COMPANIES), default=sorted(COMPANIES))
    parser.add_argument("--manifest", type=Path, action="append", default=[], help="Explicit filing array, source manifest, Jace discovery, inventory or backfill queue JSON; repeatable")
    parser.add_argument("--source-root", type=Path, action="append", default=[], help="Existing archive root containing receipt local_path; never downloads files")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay", type=Path, help="Prior private output directory; labelled replay, not live shared verification")
    parser.add_argument("--ca", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--passfile", type=Path)
    parser.add_argument("--prompt-password", action="store_true")
    parser.add_argument("--processing-set", action="append", default=[], help="Pin accession=legacy:UUID or accession=source-v4:UUID; repeatable. Default: check all stored candidates separately.")
    parser.add_argument("--download-missing", action="store_true", help="Only missing HTML/XML receipts, through the existing sequential SEC client; no ingest")
    parser.add_argument("--download-root", type=Path, help="Explicit new private archive root; existing bytes are never overwritten")
    parser.add_argument("--identity-approved", action="store_true", help="Authorize using configured EDGAR_IDENTITY; no value is logged")
    args = parser.parse_args(argv)
    selections = parse_selections(args.processing_set)
    companies = {ticker: COMPANIES[ticker] for ticker in args.ticker}
    inputs = [(str(path), json.loads(path.read_text())) for path in args.manifest]
    if args.download_missing and not args.download_root:
        parser.error("--download-missing requires an explicit --download-root.")
    def run(inventory, reader):
        if args.download_missing:
            status = download_required_sources(source_requirements(inventory, companies, args.source_root), args.download_root,
                                               identity_approved=args.identity_approved)
            args.output.mkdir(parents=True, exist_ok=True)
            save_json(args.output / "source-download-status.json", status)
            args.source_root.append(args.download_root)
        return run_validation(inventory, inputs, companies, args.source_root, args.output, reader, selections)
    if args.replay:
        inventory = json.loads((args.replay / "shared-inventory.json").read_text())
        inventory["evidence_origin"] = "saved_shared_snapshot_replay_not_live"
        args.output.mkdir(parents=True, exist_ok=True)
        save_json(args.output / "shared-inventory.json", inventory)
        def reader(accession, selection):
            path = args.replay / accession / f"{selection['contract']}--{selection['id']}" / "snapshot.json"
            return json.loads(path.read_text())
        return run(inventory, reader)
    if not args.ca:
        parser.error("Live shared reads require an existing AWS CA bundle (--ca).")
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with psycopg.connect(**shared_options(args), autocommit=True, row_factory=dict_row) as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                inventory = read_inventory(conn, companies)
                inventory["evidence_origin"] = "live_shared_repeatable_read_readonly"
                save_json(args.output / "shared-inventory.json", inventory)
                def reader(accession, selection):
                    # A failed candidate query rolls back its savepoint, not
                    # the entire repeatable-read candidate inventory.
                    with conn.transaction():
                        return read_snapshot(conn, accession, selection=selection)
                return run(inventory, reader)
    except (psycopg.Error, ValueError) as error:
        save_json(args.output / "connection-status.json", connection_failure(error))
        print("Shared batch incomplete; see connection-status.json. No shared writes attempted.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
