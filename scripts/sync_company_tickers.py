"""Refresh SEC ticker metadata and optionally fill existing NULL company tickers."""

import argparse
import json
from pathlib import Path

from sec_pipeline.acquisition import SecClient
from sec_pipeline.company_tickers import (
    SOURCE_URL, DEFAULT_REFERENCE, build_reference, write_reference,
    load_reference, missing_ticker_plan, fill_missing_tickers,
)
from sec_pipeline.database import connect
from process_filing import explicit_database_environment, ingestion_settings, check_connection_target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--universe", type=Path, help="Optional dated index snapshot for share-class preference.")
    parser.add_argument("--refresh", action="store_true", help="Fetch current SEC symbols using EDGAR_IDENTITY.")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--database")
    parser.add_argument("--apply", action="store_true", help="Fill NULL tickers; preserve populated tickers and other company fields.")
    args = parser.parse_args(argv)
    if bool(args.env_file) != bool(args.database) or (args.apply and not args.database):
        parser.error("Database work requires both --env-file and --database.")
    try:
        if args.refresh:
            preferred = {}
            if args.universe:
                universe = json.loads(args.universe.read_text(encoding="utf-8"))
                preferred = {c["cik"]: c["tickers"] for c in universe["companies"]}
            with SecClient() as client:
                data, receipt = client.get(SOURCE_URL)
            receipt["source_url"] = SOURCE_URL
            reference = build_reference(data, receipt, preferred=preferred)
            raw_path = args.reference.parent / ("company_tickers_exchange-" + receipt["sha256"] + ".json")
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            if raw_path.exists() and raw_path.read_bytes() != data:
                raise ValueError("Ticker source archive differs from its expected hash.")
            if not raw_path.exists():
                raw_path.write_bytes(data)
            write_reference(args.reference, reference)
        reference = load_reference(args.reference)
        if reference is None:
            raise ValueError("No ticker reference found; use --refresh first.")
        result = {"mapped_issuers": len(reference["companies"]), "source": reference["source"]}
        if args.database:
            with explicit_database_environment():
                settings = ingestion_settings(args.env_file, args.database)
                with connect(args.env_file, **settings) as conn:
                    check_connection_target(conn, settings)
                    conn.execute("SET LOCAL lock_timeout='3s'")
                    conn.execute("SET LOCAL statement_timeout='30s'")
                    if not args.apply:
                        conn.execute("SET TRANSACTION READ ONLY")
                    result = fill_missing_tickers(conn, reference) if args.apply else missing_ticker_plan(conn, reference)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"Ticker synchronization stopped ({type(exc).__name__}); check the source and explicit database settings.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
