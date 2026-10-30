#!/usr/bin/env python3
"""Archive original primary HTML for report validation, without database writes.

The SEC identity is read only from EDGAR_IDENTITY. Discovery receipts and source
hashes are retained; failed/missing downloads stay visible in the manifest.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from lxml import html

from sec_pipeline.acquisition import SecClient, discover_filings


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


class RecordedClient(SecClient):
    def __init__(self, output):
        super().__init__()
        self.output = output

    def get_json(self, url):
        data, metadata = self.get(url)
        name = url.rsplit("/", 1)[-1]
        target = self.output / "discovery" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        save(target.with_suffix(".receipt.json"), {"source_url": url, **metadata})
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("SEC discovery response is not an object")
        return value


def acquire(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = output / "corpus_manifest.json"
    config = json.loads(args.config.read_text())
    requested = config.get("companies", [])
    if not requested or len({item["ticker"] for item in requested}) != len(requested):
        raise ValueError("Supply a nonempty, distinct company shortlist")
    state = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "filings": []}
    old_scope = (state.get("filing_date_from"), state.get("filing_date_to"))
    if any(old_scope) and old_scope != (args.since, args.until):
        raise ValueError("Use a new output directory for a changed date window")
    if state.get("requested_tickers") and set(state["requested_tickers"]) != {item["ticker"] for item in requested}:
        raise ValueError("Use a new output directory for a changed company shortlist")
    for company in requested:
        cached = [row for row in state["filings"] if row.get("ticker") == company["ticker"]]
        previous_cik = state.get("companies", {}).get(company["ticker"], {}).get("cik")
        if previous_cik not in {None, company["cik"]} or any(row.get("cik") != company["cik"] for row in cached):
            raise ValueError("The company CIK differs from the saved discovery scope")
    state.update(requested_tickers=[company["ticker"] for company in config["companies"]],
                 forms=["10-K", "10-Q"], filing_date_from=args.since,
                 filing_date_to=args.until, amendments_included=False,
                 discovery_complete=False, coverage_status="incomplete", updated_at_utc=now())
    companies = state.setdefault("companies", {})
    existing = {item["accession_number"]: item for item in state["filings"]}
    log_path = output / "request_log.json"
    previous_requests = json.loads(log_path.read_text()) if log_path.exists() else []
    save(path, state)
    with RecordedClient(output) as client:
        try:
            for company in config["companies"]:
                ticker = company["ticker"]
                info = companies.setdefault(ticker, {})
                info["cik"] = company["cik"]
                if info.get("discovery_status") != "complete":
                    print(f"Discovering {ticker}", flush=True)
                    rows = discover_filings(client, company["cik"], args.since, args.until)
                    for row in rows:
                        record = existing.setdefault(row["accession_number"], {})
                        record.update(row, ticker=ticker, requested_shortlist=True,
                                      source_url=row["filing_index_url"].rsplit("/", 1)[0] + "/" + row["primary_document"])
                    info.update(discovery_status="complete", discovered_count=len(rows), discovered_at_utc=now())
                    state["filings"] = sorted(existing.values(), key=lambda row: (row["ticker"], row["filing_date"], row["accession_number"]))
                    save(path, state)
                selected = [item for item in state["filings"] if item["ticker"] == ticker]
                if len(selected) != info["discovered_count"]:
                    raise ValueError("Saved filing inventory differs from its discovery count")
                for index, row in enumerate(selected, 1):
                    relative = Path("sources") / row["accession_number"] / row.get("primary_document", row["source_url"].rsplit("/", 1)[-1])
                    target = output / relative
                    if target.exists():
                        if hashlib.sha256(target.read_bytes()).hexdigest() != row.get("sha256"):
                            raise ValueError("Cached source hash mismatch; original bytes were not overwritten")
                        row.update(html_path=str(relative), acquisition_status="available")
                        continue
                    print(f"Downloading {ticker} {index}/{len(selected)} {row['form']} {row['accession_number']}", flush=True)
                    data, metadata = client.get(row["source_url"])
                    document = html.document_fromstring(data, parser=html.HTMLParser(encoding="utf-8", no_network=True))
                    if not document.xpath("//body"):
                        raise ValueError("Primary filing has no HTML body")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    row.update(html_path=str(relative), acquisition_status="available",
                               acquisition_method="SEC submissions primaryDocument", **metadata)
                    save(path, state)
                info["available_count"] = sum(item.get("acquisition_status") == "available" for item in selected)
                save(path, state)
                print(f"Ready {ticker}: {len(selected)} original filings", flush=True)
            state["discovery_complete"] = all(companies.get(company["ticker"], {}).get("discovery_status") == "complete" for company in config["companies"])
            state["coverage_status"] = "acquired" if state["discovery_complete"] and all(item.get("acquisition_status") == "available" for item in state["filings"] if item.get("requested_shortlist")) else "incomplete"
            state.pop("acquisition_blocker", None)
        except Exception as exc:
            # Error types are sufficient to diagnose transport failures without
            # accidentally persisting a runtime identity or request headers.
            state["acquisition_blocker"] = type(exc).__name__
            raise
        finally:
            state["updated_at_utc"] = now()
            save(path, state)
            save(log_path, previous_requests + client.request_log)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/shortlist.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    args = parser.parse_args()
    state = acquire(args)
    print(json.dumps({"discovery_complete": state["discovery_complete"], "coverage_status": state["coverage_status"], "filings": len(state["filings"])}))


if __name__ == "__main__":
    main()
