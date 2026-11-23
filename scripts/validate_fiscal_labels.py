"""Compare calendar-derived FY/period, stored XBRL DEI, and official output by accession."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from sec_pipeline.fiscal_calendar_validation import (
    calculate_fiscal, compare_labels, inspect_dei, inspect_publication,
)
from sec_pipeline.historical_table_validation import utc_now, write_csv
from sec_pipeline.table_relations_cli import save_json


def load(path):
    return json.loads(path.read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("calendar", "inventory", "dei", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--published", type=Path, help="Independent, officially released export manifest.")
    parser.add_argument("--snapshots", type=Path, help="Receiving snapshots for exact publication-scope comparison.")
    parser.add_argument("--live-catalog", type=Path, help="Optional current read-only schema evidence.")
    args = parser.parse_args(argv)
    if bool(args.published) != bool(args.snapshots):
        parser.error("--published and --snapshots must be supplied together.")
    if args.output.exists() and any((args.output / name).exists() for name in (
            "fiscal-labels.csv", "fiscal-label-evidence.json", "fiscal-label-run.json")):
        parser.error("Choose a new directory for the fiscal QA output files.")
    rules, inventory = load(args.calendar)["rules"], load(args.inventory)
    tickers = {rule["cik"]: rule["ticker"] for rule in rules}
    scopes, published = defaultdict(list), defaultdict(list)
    for item in load(args.dei):
        scopes[item["accession"]].append(item)
    if args.published:
        for export in load(args.published):
            if not export.get("evidence"):
                parser.error("Each official export needs an attributable evidence reference.")
            published[export["accession"]].append(export)
    receipts, summaries, seen = [], [], set()
    for row in inventory["filings"]:
        cik = str(row["cik"]).zfill(10)
        if cik not in tickers:
            continue
        accession = row["accession_number"]
        if accession in seen:
            parser.error(f"Duplicate accession in receiving inventory: {accession}")
        seen.add(accession)
        filing = {"accession": accession, "filing_id": row["id"], "CIK": cik,
                  "ticker": tickers[cik], "form": row["form"], "reporting_period_end": row["reporting_period_end"]}
        calculated = calculate_fiscal(filing, rules)
        dei = inspect_dei(filing, scopes[accession])
        released = inspect_publication(filing, published[accession], args.snapshots)
        comparisons = {"calculated_vs_dei": compare_labels(calculated, dei),
                       "calculated_vs_published": compare_labels(calculated, released),
                       "dei_vs_published": compare_labels(dei, released)}
        summary = filing | {
            "calculated_fy": calculated.get("fiscal_year"), "calculated_period": calculated.get("period"),
            "calendar_status": calculated["status"], "calendar_review_reason": calculated.get("reason"),
            "expected_period_end": calculated.get("expected_period_end"),
            "expected_fiscal_year_end": calculated.get("expected_fiscal_year_end"),
            "deviation_days": calculated.get("deviation_days"), "nominal_period_end": calculated.get("nominal_period_end"),
            "nominal_deviation_days": calculated.get("nominal_deviation_days"),
            "fiscal_weeks": calculated.get("fiscal_weeks"), "tolerance_days": calculated.get("tolerance_days"),
            "dei_fy": dei.get("fiscal_year"), "dei_period": dei.get("period"), "dei_status": dei["status"],
            "published_fy": released.get("fiscal_year"), "published_period": released.get("period"),
            "published_status": released["status"], "table_inheritance": released["table_inheritance"],
            "cell_inheritance": released["cell_inheritance"], **comparisons,
        }
        for scope in dei["scopes"]:
            if scope["label"]:
                year, period = scope["label"]
                scope["calculated_vs_dei"] = compare_labels(calculated, {"fiscal_year": year, "period": period})
        for export in released["exports"]:
            if export["label"]:
                year, period = export["label"]
                published_label = {"fiscal_year": year, "period": period}
                export["calculated_vs_published"] = compare_labels(calculated, published_label)
                export["dei_vs_published"] = compare_labels(dei, published_label)
        for key, evidence in (("calculated_vs_dei", dei["scopes"]),
                              ("calculated_vs_published", released["exports"]),
                              ("dei_vs_published", released["exports"])):
            if any(item.get(key) == "mismatch" for item in evidence):
                comparisons[key] = summary[key] = "mismatch"
        receipts.append(filing | {"calculated": calculated, "dei": dei, "publication": released, **comparisons})
        summaries.append(summary)
    if not summaries:
        parser.error("No receiving accessions match the supplied historical calendar CIKs.")
    if args.published and set(published) - seen:
        parser.error("Official exports contain accession(s) outside the receiving inventory.")
    args.output.mkdir(parents=True, exist_ok=True)
    fields = ("calendar_status", "dei_status", "published_status", "table_inheritance", "cell_inheritance",
              "calculated_vs_dei", "calculated_vs_published", "dei_vs_published")
    counts = {name: dict(Counter(row[name] for row in summaries)) for name in fields}
    write_csv(args.output / "fiscal-labels.csv", summaries, list(summaries[0]))
    save_json(args.output / "fiscal-label-evidence.json", receipts)
    save_json(args.output / "fiscal-label-run.json", {"ran_at_utc": utc_now(), "filings": len(summaries), "counts": counts,
              "input_inventory_identity": inventory.get("identity"), "inputs": {name: getattr(args, name) for name in (
                  "inventory", "dei", "published", "snapshots", "calendar", "live_catalog")},
              "live_catalog": load(args.live_catalog) if args.live_catalog else None})
    print(json.dumps(counts, sort_keys=True))
    if any("mismatch" in counts[name] for name in fields if "_vs_" in name) or any(
            "failed" in counts[name] for name in ("table_inheritance", "cell_inheritance")):
        return 1
    return 0 if all(
        row["calculated_vs_dei"] == row["calculated_vs_published"] == "match"
        and row["table_inheritance"] == row["cell_inheritance"] == "passed" for row in summaries) else 2


if __name__ == "__main__":
    raise SystemExit(main())
