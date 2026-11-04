"""Confirmed-shortlist orchestration over preserved read-only DB snapshots."""
import argparse
import json
from pathlib import Path

from sec_pipeline.historical_table_validation import parse_selections
from sec_pipeline.shortlist_table_validation import run_scoped_validation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/shortlist.json"))
    parser.add_argument("--section-audit", type=Path, default=Path("docs/validation/report-sections-2021-2026.json"))
    parser.add_argument("--snapshots", type=Path, required=True, help="Explicit read-only capture directory; no credential values")
    parser.add_argument("--source-root", type=Path, action="append", default=[])
    parser.add_argument("--manifest", type=Path, action="append", default=[])
    parser.add_argument("--ticker", nargs="+")
    parser.add_argument("--processing-set", action="append", default=[])
    parser.add_argument("--table-chunks", type=Path, help="Actual producer records, never paragraph chunk substitutes")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_scoped_validation(args.config, args.section_audit, args.snapshots, args.source_root, args.output,
        tickers=args.ticker, additional_inputs=[(str(p), json.loads(p.read_text())) for p in args.manifest],
        selections=parse_selections(args.processing_set), producer_records=json.loads(args.table_chunks.read_text()) if args.table_chunks else [])
    print((args.output / "run_status.json").read_text())
    return result


if __name__ == "__main__":
    raise SystemExit(main())
