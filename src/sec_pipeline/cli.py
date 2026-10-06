"""Export an income statement with provenance from the same selected 10-K."""

from __future__ import annotations

import argparse
import csv
import logging
import os
from pathlib import Path
import re
import sys

import httpx


class ExportError(RuntimeError):
    """The requested filing cannot be safely exported."""


def approved_identity(approved: bool) -> str:
    if not approved:
        raise ExportError("SEC access requires explicit --identity-approved authorization.")
    identity = os.environ.get("EDGAR_IDENTITY", "").strip()
    if (not identity.isascii()
            or any(ord(char) < 32 for char in identity)
            or not re.fullmatch(r"[^@]+\S +[^\s@]+@[^\s@]+\.[^\s@]+", identity)
            or identity.endswith("@example.com")):
        raise ExportError("Set EDGAR_IDENTITY to the approved name and contact email.")
    return identity


def export_income_statement(ticker: str, output: Path, identity: str) -> dict[str, str]:
    # Reject an existing destination before importing the network client.
    if output.exists():
        raise ExportError(f"Output already exists; choose a new path: {output}")

    os.environ["EDGAR_LOCAL_DATA_DIR"] = str(Path.cwd() / ".cache" / "edgar")
    os.environ["EDGAR_RATE_LIMIT_PER_SEC"] = "1"
    os.environ["EDGAR_VERIFY_SSL"] = "true"
    from edgar import Company, set_identity
    from edgar.core import log
    from edgar.financials import Financials

    log.setLevel(logging.WARNING)
    set_identity(identity)
    filings = Company(ticker).get_filings(form="10-K")
    if len(filings) == 0:
        raise ExportError(f"No 10-K filings found for {ticker}.")
    filing = filings[0]
    # Bind the statement to this filing, rather than fetching company-level
    # financials independently and risking mismatched source metadata.
    financials = Financials.extract(filing)
    if financials is None:
        raise ExportError("The selected 10-K has no financial statements.")
    income = financials.income_statement()
    if income is None:
        raise ExportError("The selected 10-K has no income statement.")
    frame = income.to_dataframe()
    if frame.empty:
        raise ExportError("The selected income statement contains no rows.")
    source_info = {
        "accession_number": filing.accession_number,
        "filing_url": filing.url,
        "form_type": filing.form,
        "company": ticker,
    }
    if set(source_info).intersection(frame.columns):
        raise ExportError("Statement columns conflict with source metadata.")
    records = frame.to_dict(orient="records")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also protects a file created during the SEC lookup.
    with output.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*source_info, *frame.columns])
        writer.writeheader()
        for row in records:
            writer.writerow({**row, **source_info})
    return source_info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="AAPL", help="Company ticker (default: AAPL).")
    parser.add_argument("--output", type=Path,
                        help="New CSV path (default: data/processed/<TICKER>-income.csv).")
    parser.add_argument("--identity-approved", action="store_true",
                        help="Confirm approval of the runtime EDGAR_IDENTITY for this SEC lookup.")
    args = parser.parse_args(argv)
    ticker = args.ticker.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", ticker):
        parser.error("--ticker must be a valid company ticker, such as AAPL.")
    output = args.output if args.output is not None else Path("data/processed") / f"{ticker}-income.csv"
    try:
        identity = approved_identity(args.identity_approved)
        source_info = export_income_statement(ticker, output, identity)
    except (ExportError, OSError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"Stopped: SEC request failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    print(f"Exported {ticker} {source_info['form_type']} {source_info['accession_number']} to {output}")
    return 0
