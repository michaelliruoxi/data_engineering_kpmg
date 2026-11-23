"""Read-only batch runner. Credentials are obtained only through existing libpq settings or hidden input."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from sec_pipeline.historical_table_validation import (COMPANIES, GRAPHRAG_COMPANIES, GRAPHRAG_DATES, connection_failure, download_required_sources, parse_selections,
    read_inventory, run_validation, source_requirements, utc_now)
from sec_pipeline.table_relations import read_snapshot
from sec_pipeline.table_relations_cli import save_json, shared_options


def run_receipt(output, date_range, companies, manifest, source_roots, replay):
    repository = Path(__file__).resolve().parents[1]
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True)
    sources = ("scripts/validate_historical_tables.py", "src/sec_pipeline/historical_table_validation.py",
               "src/sec_pipeline/table_relations.py", "src/sec_pipeline/table_source_reconstruction.py",
               "src/sec_pipeline/tables.py")
    save_json(output / "run_metadata.json", {"started_at_utc": utc_now(), "git_head": git.stdout.strip() if not git.returncode else None,
        "code_sha256": {name: hashlib.sha256((repository / name).read_bytes()).hexdigest() for name in sources},
        "date_range": date_range, "companies": companies, "manifest_paths": manifest,
        "source_roots": source_roots, "evidence_mode": "replay" if replay else "read_only_live"})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graphrag-priority", action="store_true", help="GraphRAG six-company original 10-K/10-Q scope, filing dates 2021-01-01 through 2026-12-31")
    parser.add_argument("--ticker", nargs="+", choices=sorted(COMPANIES | GRAPHRAG_COMPANIES))
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
    parser.add_argument("--include-prior-sample", action="store_true", help="Independently revalidate the old MSFT sample when the declared full inventory includes it")
    args = parser.parse_args(argv)
    selections = parse_selections(args.processing_set)
    scope = GRAPHRAG_COMPANIES if args.graphrag_priority else COMPANIES | GRAPHRAG_COMPANIES
    tickers = args.ticker or sorted(GRAPHRAG_COMPANIES if args.graphrag_priority else COMPANIES)
    if set(tickers) - scope.keys():
        parser.error("--graphrag-priority limits --ticker to the six GraphRAG companies.")
    companies = {ticker: scope[ticker] for ticker in tickers}
    date_range = GRAPHRAG_DATES if args.graphrag_priority else None
    inputs = [(str(path), json.loads(path.read_text())) for path in args.manifest]
    if args.download_missing and not args.download_root:
        parser.error("--download-missing requires an explicit --download-root.")
    def run(inventory, reader):
        if args.download_missing:
            status = download_required_sources(source_requirements(inventory, companies, args.source_root,
                                               include_prior_sample=args.include_prior_sample, date_range=date_range), args.download_root,
                                               identity_approved=args.identity_approved)
            args.output.mkdir(parents=True, exist_ok=True)
            save_json(args.output / "source-download-status.json", status)
            args.source_root.append(args.download_root)
        return run_validation(inventory, inputs, companies, args.source_root, args.output, reader, selections,
                              include_prior_sample=args.include_prior_sample, date_range=date_range)
    if date_range:
        if args.output.exists() and any(args.output.iterdir()):
            parser.error("GraphRAG capture requires a new, empty output directory.")
        args.output.mkdir(parents=True, exist_ok=True)
        run_receipt(args.output, date_range, companies, args.manifest, args.source_root, args.replay)
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
                inventory = read_inventory(conn, companies, date_range)
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
