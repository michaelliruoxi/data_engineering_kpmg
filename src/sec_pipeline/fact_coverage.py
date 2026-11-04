"""Financial fact coverage across archived 10-K/10-Q filings (offline, read-only).

For every verified source manifest in the supplied archive roots that falls in the
configured company, filing-date, and form scope, this runs the same facts-v1
extraction and inline HTML correspondence that the import adapters use, and reports
whether each filing's facts, periods, and units can be extracted. With the acquisition
`discovery.json`, filings that the SEC lists but no archive holds are reported as
`missing_archive` instead of silently disappearing from the totals.

It opens no database connection and makes no network request. Output goes to a new
directory: coverage.json (everything), coverage.csv (one row per filing), and
coverage.md (the summary to hand to the team).

    uv run --locked python -m sec_pipeline.fact_coverage \
      --config config/shortlist.json --archive-root outputs/shortlist-20261028 \
      --discovery outputs/shortlist-20261028/discovery.json --output outputs/fact-coverage
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Sequence
from uuid import UUID

from lxml import etree

from .acquisition import shortlist_scope_key
from .database import FactInput
from .fact_extraction import (FactExtractionError, HTML_ROLE, XML_ROLE, check_inline_correspondence,
                              extract_facts)

VERSION = "fact-coverage-v1"
DEI_PREFIX = "http://xbrl.sec.gov/dei/"
US_GAAP_PREFIX = "http://fasb.org/us-gaap/"
DEI_FIELDS = ("DocumentType", "DocumentPeriodEndDate", "DocumentFiscalYearFocus",
              "DocumentFiscalPeriodFocus", "CurrentFiscalYearEndDate", "AmendmentFlag",
              "EntityCentralIndexKey")
# Primary-statement anchors checked for the filing's own report period. Each group is
# satisfied by any one listed us-gaap concept, undimensioned and not nil.
CORE_CONCEPTS = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "eps_diluted": ("EarningsPerShareDiluted",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "assets": ("Assets",),
    "liabilities_and_equity": ("LiabilitiesAndStockholdersEquity",),
    "equity": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    "cash": ("CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
}
PLACEHOLDER = UUID(int=0)
STATUSES = ("passed", "review", "failed", "archive_error", "missing_archive")


class CoverageError(ValueError):
    """The scope, inputs, or output location cannot be used."""


# ----------------------------------------------------------------------------- scope

def load_scope(path: Path) -> dict[str, Any]:
    """Read a shortlist-style config: since, until, forms, companies[{ticker, cik}]."""
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        since, until = date.fromisoformat(config["since"]), date.fromisoformat(config["until"])
        companies = config["companies"]
        tickers = [c["ticker"] for c in companies]
        ciks = [c["cik"] for c in companies]
        forms = list(config["forms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CoverageError("The config needs since, until, forms, and companies with ticker and cik.") from exc
    if since > until or not companies or not forms:
        raise CoverageError("The config scope is empty or its date window is reversed.")
    if len(set(ciks)) != len(companies) or len(set(tickers)) != len(companies):
        raise CoverageError("Each company must have a distinct ticker and CIK.")
    return {"since": since.isoformat(), "until": until.isoformat(), "forms": forms, "companies": companies}


def _in_scope(record: dict[str, Any], scope: dict[str, Any]) -> bool:
    return (record.get("cik") in {c["cik"] for c in scope["companies"]}
            and record.get("form") in scope["forms"]
            and scope["since"] <= str(record.get("filing_date", "")) <= scope["until"])


def _manifests(root: Path) -> list[Path]:
    """Each archive root is a data root: data/raw/sec/<accession>/ plus an optional root manifest."""
    if not root.is_dir():
        raise CoverageError(f"Archive root is not a directory: {root}")
    found = sorted((root / "data" / "raw" / "sec").glob("*/source_manifest.json"))
    if (root / "source_manifest.json").is_file():
        found.append(root / "source_manifest.json")
    return found


def expected_filings(discovery: dict[str, Any] | None, scope: dict[str, Any]) -> dict[str, Any] | None:
    """Accession -> SEC discovery row, or None when no complete discovery was supplied."""
    if discovery is None:
        return None
    if not isinstance(discovery, dict) or discovery.get("scope_key") != shortlist_scope_key(scope):
        raise CoverageError("Discovery scope does not match the configured company, form, and date scope.")
    if discovery.get("discovery_complete") is not True or not isinstance(discovery.get("companies"), dict):
        raise CoverageError("Discovery is incomplete; rerun acquisition discovery.")
    rows: dict[str, Any] = {}
    for company in scope["companies"]:
        entry = discovery["companies"].get(company["ticker"])
        if not isinstance(entry, dict) or entry.get("status") != "discovered" or not isinstance(entry.get("filings"), list):
            raise CoverageError(f"Discovery is incomplete for {company['ticker']}; rerun acquisition discovery.")
        for row in entry["filings"]:
            if (not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k]
                    for k in ("accession_number", "cik", "filing_date", "form", "reporting_period_end"))
                    or row["cik"] != company["cik"] or not _in_scope(row, scope)):
                raise CoverageError(f"Invalid or out-of-scope filing in discovery for {company['ticker']}.")
            accession = row["accession_number"]
            if accession in rows:
                raise CoverageError(f"Duplicate accession in discovery: {accession}")
            rows[accession] = row
    return rows


# ------------------------------------------------------------------------ per filing

def _dei(facts: Sequence[FactInput]) -> dict[str, Any]:
    """First undimensioned value of each DEI field, plus the DocumentType context period."""
    found: dict[str, Any] = {}
    for fact in facts:
        if (fact.concept_namespace.startswith(DEI_PREFIX) and not fact.dimensions
                and fact.concept_name in DEI_FIELDS and fact.concept_name not in found and not fact.is_nil):
            found[fact.concept_name] = (fact.raw_value or "").strip()
            if fact.concept_name == "DocumentType":
                found["report_period"] = {"start": fact.period_start and fact.period_start.isoformat(),
                                          "end": (fact.period_end or fact.instant_date)
                                          and (fact.period_end or fact.instant_date).isoformat()}
    return found


def _core(facts: Sequence[FactInput], target: date | None) -> dict[str, str]:
    """For each anchor group: 'exact' at the report period end, 'off_<n>d', or 'missing'."""
    result = {}
    for group, names in CORE_CONCEPTS.items():
        best = None
        for fact in facts:
            if (target is not None and fact.concept_namespace.startswith(US_GAAP_PREFIX)
                    and fact.concept_name in names and not fact.dimensions and not fact.is_nil):
                end = fact.period_end or fact.instant_date
                if end is None:
                    continue
                gap = abs((end - target).days)
                best = gap if best is None else min(best, gap)
        result[group] = "missing" if best is None else "exact" if best == 0 else f"off_{best}d"
    return result


def _unit_text(unit: dict[str, list[str]] | None) -> str:
    def local(measures: list[str]) -> str:
        return "*".join(m.rsplit("}", 1)[-1] for m in measures)
    if not unit:
        return "(none)"
    return local(unit["numerator"]) + ("/" + local(unit["denominator"]) if unit["denominator"] else "")


def assess_filing(manifest: dict[str, Any], root: Path, ticker: str) -> dict[str, Any]:
    """Run facts-v1 extraction and correspondence for one verified archive."""
    by_role = {record["role"]: record for record in manifest["files"]}
    record: dict[str, Any] = {
        "ticker": ticker, "cik": manifest["cik"], "form": manifest["form"],
        "accession_number": manifest["accession_number"], "filing_date": manifest["filing_date"],
        "reporting_period_end": manifest.get("reporting_period_end"),
        "xml_sha256": by_role[XML_ROLE]["sha256"], "html_sha256": by_role[HTML_ROLE]["sha256"],
    }
    try:
        extraction = extract_facts(root / by_role[XML_ROLE]["local_path"], filing_id=PLACEHOLDER,
                                   source_document_id=PLACEHOLDER)
    except FactExtractionError as exc:
        return record | {"status": "failed", "problems": [f"extraction stopped: {exc}"]}
    facts, inventory = extraction.facts, extraction.inventory
    try:
        correspondence = check_inline_correspondence(root / by_role[HTML_ROLE]["local_path"], facts)
    except FactExtractionError as exc:
        return record | {"status": "failed", "problems": [f"inline HTML cannot be checked: {exc}"]}
    dei = _dei(facts)
    report_end = dei.get("DocumentPeriodEndDate")
    try:
        target = date.fromisoformat(report_end) if report_end else None
    except ValueError:
        target = None
    core = _core(facts, target)
    units = Counter(_unit_text(fact.unit) for fact in facts if fact.unit)

    problems, review = [], []
    if extraction.failures:
        problems.append(f"{len(extraction.failures)} occurrence(s) not extracted: "
                        + "; ".join(f"{f.category} {f.reason}" for f in extraction.failures[:3]))
    if correspondence["mismatches"]:
        problems.append(f"{len(correspondence['mismatches'])} fact(s) disagree with the inline HTML: "
                        + "; ".join(m["problem"] for m in correspondence["mismatches"][:3]))
    if correspondence["numeric_values_not_compared"]:
        problems.append(f"{correspondence['numeric_values_not_compared']} numeric value(s) use an "
                        "unsupported inline format and were not compared")
    if correspondence["inline_ids_without_fact"]:
        problems.append(f"{len(correspondence['inline_ids_without_fact'])} inline tag(s) have no XML fact")
    if dei.get("DocumentType") != manifest["form"]:
        review.append(f"dei:DocumentType {dei.get('DocumentType')!r} differs from form {manifest['form']}")
    if manifest.get("reporting_period_end") and dei.get("DocumentPeriodEndDate") != manifest["reporting_period_end"]:
        review.append(f"dei:DocumentPeriodEndDate {dei.get('DocumentPeriodEndDate')!r} differs from the SEC "
                      f"report date {manifest['reporting_period_end']}")
    context = dei.get("report_period")
    if context is not None and context["end"] != report_end:
        review.append(f"DocumentType context end {context['end']!r} differs from "
                      f"dei:DocumentPeriodEndDate {report_end!r}")
    irregular = correspondence.get("irregular_number_display", [])
    if irregular:
        review.append(f"{len(irregular)} number(s) printed with irregular digit grouping, for example "
                      f"{irregular[0]['occurrence_key']} shown as {irregular[0]['displayed']!r}; "
                      "the XML value may be off by a power of ten")
    missing = [group for group, status in core.items() if status != "exact"]
    if missing:
        review.append("no undimensioned fact at the report period end for: " + ", ".join(missing))
    if not inventory["by_period_kind"]:
        problems.append("no fact has a usable period")

    status = "failed" if problems else "review" if review else "passed"
    labels = inventory["labels"]
    return record | {
        "status": status, "problems": problems, "review": review,
        "facts": {key: inventory[key] for key in ("fact_occurrences", "extracted", "failed", "tuples",
                                                  "extracted_numeric", "extracted_nil", "extracted_zero",
                                                  "with_dimensions", "distinct_concepts")}
                 | {"extracted_text": sum(1 for fact in facts if fact.unit is None and not fact.is_nil),
                    "failures_by_category": inventory["failures_by_category"]},
        "periods": {"by_kind": inventory["by_period_kind"], "contexts": inventory["contexts"],
                    "report_period": dei.get("report_period"),
                    "fiscal_year_focus": dei.get("DocumentFiscalYearFocus"),
                    "fiscal_period_focus": dei.get("DocumentFiscalPeriodFocus")},
        "units": {"declared": inventory["units"], "facts_with_unit": sum(units.values()),
                  "by_unit": dict(sorted(units.items(), key=lambda item: (-item[1], item[0])))},
        "dei": {key: dei.get(key) for key in DEI_FIELDS},
        "core_concepts": core,
        "labels": {"facts_with_label": labels["facts_with_label"],
                   "facts_without_label": labels["facts_without_label"],
                   "source": "extension_schema" if labels["facts_with_label"] else
                   "none (labels are in a separate label linkbase that facts-v1 does not read)"},
        "inline_correspondence": {key: correspondence[key] for key in (
            "facts", "inline_elements", "matched", "numeric_values_equal", "numeric_values_not_compared")}
                                 | {"mismatches": len(correspondence["mismatches"]),
                                    "inline_ids_without_fact": len(correspondence["inline_ids_without_fact"]),
                                    "irregular_number_display": irregular},
        "footnotes_not_extracted": inventory["not_extracted"]["footnotes"],
    }


# --------------------------------------------------------------------------- corpus

def assess_archives(scope: dict[str, Any], roots: Iterable[Path],
                    discovery: dict[str, Any] | None = None, *, tickers: Sequence[str] | None = None,
                    progress=None) -> dict[str, Any]:
    from .filing_sources import verify_filing

    expected = expected_filings(discovery, scope)
    if tickers:
        unknown = set(tickers) - {c["ticker"] for c in scope["companies"]}
        if unknown:
            raise CoverageError(f"Tickers outside the config: {', '.join(sorted(unknown))}")
        scope = scope | {"companies": [c for c in scope["companies"] if c["ticker"] in tickers]}
        if expected is not None:
            expected = {a: row for a, row in expected.items() if _in_scope(row, scope)}
    ticker_by_cik = {c["cik"]: c["ticker"] for c in scope["companies"]}
    results: dict[str, dict[str, Any]] = {}
    source_hashes: dict[str, dict[str, str]] = {}
    for root in (Path(r).resolve() for r in roots):
        manifests = _manifests(root)
        if not manifests and expected is None:
            raise CoverageError(f"No source manifests found in archive root: {root}")
        for path in manifests:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise CoverageError(f"Cannot read source manifest {path}: {exc}") from exc
            if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) and raw[k]
                    for k in ("accession_number", "cik", "filing_date", "form")):
                raise CoverageError(f"Source manifest has incomplete filing identity: {path}")
            if not _in_scope(raw, scope):
                continue
            accession = raw["accession_number"]
            try:
                manifest = verify_filing(path, root)
            except (OSError, ValueError, KeyError, TypeError, etree.Error) as exc:
                results.setdefault(accession, {"ticker": ticker_by_cik[raw["cik"]], "cik": raw["cik"],
                                               "form": raw["form"], "accession_number": accession,
                                               "filing_date": raw["filing_date"], "status": "archive_error",
                                               "problems": [f"source verification failed: {str(exc)[:160]}"]})
                continue
            if expected is not None:
                row = expected.get(accession)
                if row is None or any(row[key] != manifest[key] for key in
                                      ("cik", "form", "filing_date", "reporting_period_end")):
                    raise CoverageError(f"Verified archive disagrees with discovery for {accession}.")
            hashes = {r["role"]: r["sha256"] for r in [manifest["index_document"], *manifest["files"]]}
            if accession in source_hashes:
                if hashes != source_hashes[accession] or any(
                        results[accession][key] != manifest[key]
                        for key in ("cik", "form", "filing_date", "reporting_period_end")):
                    raise CoverageError(f"Conflicting verified archives for {accession}.")
                continue
            source_hashes[accession] = hashes
            if progress:
                progress(f"{ticker_by_cik[manifest['cik']]} {manifest['form']} {accession}")
            results[accession] = assess_filing(manifest, root, ticker_by_cik[manifest["cik"]])
    for accession, row in (expected or {}).items():
        if accession not in results:
            results[accession] = {"ticker": ticker_by_cik[row["cik"]], "cik": row["cik"], "form": row["form"],
                                  "accession_number": accession, "filing_date": row["filing_date"],
                                  "reporting_period_end": row.get("reporting_period_end"),
                                  "status": "missing_archive",
                                  "problems": ["listed by SEC discovery but no verified archive was supplied"]}
    filings = sorted(results.values(), key=lambda r: (r["ticker"], r["filing_date"], r["accession_number"]))
    return summarize(scope, filings, discovery_supplied=expected is not None)


def summarize(scope: dict[str, Any], filings: list[dict[str, Any]], *, discovery_supplied: bool) -> dict[str, Any]:
    companies = []
    by_year: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for company in scope["companies"]:
        rows = [f for f in filings if f["ticker"] == company["ticker"]]
        counts = Counter(f["status"] for f in rows)
        usable = [f for f in rows if f["status"] in ("passed", "review")]
        companies.append({
            "ticker": company["ticker"], "cik": company["cik"], "filings": len(rows),
            **{status: counts.get(status, 0) for status in STATUSES},
            "usable_10k": sum(f["form"] == "10-K" for f in usable),
            "usable_10q": sum(f["form"] == "10-Q" for f in usable),
            "has_usable_10k_and_10q": any(f["form"] == "10-K" for f in usable)
                                      and any(f["form"] == "10-Q" for f in usable),
            "facts_extracted": sum(f.get("facts", {}).get("extracted", 0) for f in rows),
        })
        for f in rows:
            by_year[company["ticker"]][f["filing_date"][:4]][f["form"]] += 1
    assessed = [f for f in filings if "facts" in f]
    totals = Counter(f["status"] for f in filings)
    kinds, units = Counter(), Counter()
    for f in assessed:
        kinds.update(f["periods"]["by_kind"])
        units.update(f["units"]["by_unit"])
    overall = "review_required"
    if filings and totals["passed"] == len(filings):
        overall = "complete" if discovery_supplied else "complete_for_supplied_archives"
    return {
        "version": VERSION, "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": scope, "status": overall,
        "discovery_supplied": discovery_supplied,
        "totals": {"filings": len(filings), **{s: totals.get(s, 0) for s in STATUSES},
                   "facts_extracted": sum(f["facts"]["extracted"] for f in assessed),
                   "facts_failed": sum(f["facts"]["failed"] for f in assessed),
                   "numeric_values_equal": sum(f["inline_correspondence"]["numeric_values_equal"] for f in assessed),
                   "numeric_values_not_compared": sum(f["inline_correspondence"]["numeric_values_not_compared"]
                                                      for f in assessed),
                   "filings_without_labels": sum(not f["labels"]["facts_with_label"] for f in assessed),
                   "period_kinds": dict(kinds), "units": dict(units.most_common())},
        "companies": companies,
        "filings_by_year": {t: {y: dict(c) for y, c in sorted(years.items())} for t, years in by_year.items()},
        "filings": filings,
        "limits": [
            "Facts-v1 rules: an occurrence counts as extracted only when its value, period, unit and "
            "dimensions are represented exactly; failures are listed, never dropped.",
            "Core-concept anchors are a sanity check for the report period, not accounting validation; "
            "a company may legitimately use another concept.",
            "Labels come only from the archived extension schema. Older filings keep labels in a separate "
            "label linkbase that the four-file archive does not include.",
            "Without --discovery, missing filings cannot be detected; totals cover supplied archives only.",
        ],
    }


# --------------------------------------------------------------------------- output

CSV_COLUMNS = ("ticker", "form", "filing_date", "accession_number", "status", "fact_occurrences", "extracted",
               "failed", "duration", "instant", "forever", "units_used", "numeric", "nil", "text",
               "with_dimensions", "numeric_values_equal", "numeric_values_not_compared", "inline_mismatches",
               "labels", "dei_document_type", "dei_period_end", "fiscal_year", "fiscal_period",
               "core_missing", "problems")


def _csv_row(f: dict[str, Any]) -> list[Any]:
    facts, periods = f.get("facts", {}), f.get("periods", {}).get("by_kind", {})
    corr, dei = f.get("inline_correspondence", {}), f.get("dei", {})
    return [f["ticker"], f["form"], f["filing_date"], f["accession_number"], f["status"],
            facts.get("fact_occurrences", ""), facts.get("extracted", ""), facts.get("failed", ""),
            periods.get("duration", 0 if facts else ""), periods.get("instant", 0 if facts else ""),
            periods.get("forever", 0 if facts else ""), len(f.get("units", {}).get("by_unit", {})) if facts else "",
            facts.get("extracted_numeric", ""), facts.get("extracted_nil", ""), facts.get("extracted_text", ""),
            facts.get("with_dimensions", ""), corr.get("numeric_values_equal", ""),
            corr.get("numeric_values_not_compared", ""), corr.get("mismatches", ""),
            f.get("labels", {}).get("facts_with_label", ""), dei.get("DocumentType") or "",
            dei.get("DocumentPeriodEndDate") or "", f.get("periods", {}).get("fiscal_year_focus") or "",
            f.get("periods", {}).get("fiscal_period_focus") or "",
            " ".join(g for g, s in f.get("core_concepts", {}).items() if s != "exact"),
            " | ".join(f.get("problems", []) + f.get("review", []))]


def render_markdown(report: dict[str, Any]) -> str:
    t, scope = report["totals"], report["scope"]
    lines = [
        "# Financial fact coverage",
        "",
        f"Scope: {len(scope['companies'])} companies, {', '.join(scope['forms'])}, filing dates "
        f"{scope['since']} to {scope['until']}. Generated {report['generated_at_utc']} by `{report['version']}` "
        f"(facts-v1 extraction and inline correspondence). Status: **{report['status']}**.",
        "",
        f"- Filings: **{t['filings']}** — passed {t['passed']}, review {t['review']}, failed {t['failed']}, "
        f"archive errors {t['archive_error']}, missing archives {t['missing_archive']}.",
        f"- Facts extracted: **{t['facts_extracted']:,}**; occurrences not extracted: {t['facts_failed']}.",
        f"- Numeric values equal to the inline HTML: {t['numeric_values_equal']:,}; "
        f"not compared: {t['numeric_values_not_compared']}.",
        "- Periods: " + ", ".join(f"{k} {v:,}" for k, v in sorted(t["period_kinds"].items())) + ".",
        "- Units: " + ", ".join(f"{k} {v:,}" for k, v in list(t["units"].items())[:8]) + ".",
        f"- Filings with no labels (label linkbase not archived): {t['filings_without_labels']}.",
        "",
        "| Ticker | Filings | Passed | Review | Failed | Missing | Usable 10-K | Usable 10-Q | Facts |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for c in report["companies"]:
        lines.append(f"| {c['ticker']} | {c['filings']} | {c['passed']} | {c['review']} | "
                     f"{c['failed'] + c['archive_error']} | {c['missing_archive']} | {c['usable_10k']} | "
                     f"{c['usable_10q']} | {c['facts_extracted']:,} |")
    flagged = [f for f in report["filings"] if f["status"] != "passed"]
    if flagged:
        lines += ["", "## Filings needing attention", "", "| Ticker | Form | Filed | Accession | Status | Reason |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for f in flagged:
            reason = "; ".join(f.get("problems", []) + f.get("review", [])).replace("|", "/")
            lines.append(f"| {f['ticker']} | {f['form']} | {f['filing_date']} | {f['accession_number']} | "
                         f"{f['status']} | {reason[:220]} |")
    lines += ["", "## Limits", ""] + [f"- {limit}" for limit in report["limits"]]
    return "\n".join(lines) + "\n"


def write_outputs(report: dict[str, Any], output: Path) -> list[Path]:
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise CoverageError(f"{output} is not empty; choose a new output directory.")
    output.mkdir(parents=True, exist_ok=True)
    paths = [output / "coverage.json", output / "coverage.csv", output / "coverage.md"]
    paths[0].write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    with paths[1].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(_csv_row(f) for f in report["filings"])
    paths[2].write_text(render_markdown(report), encoding="utf-8")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sec_pipeline.fact_coverage", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=Path("config/shortlist.json"),
                        help="Scope: since, until, forms, companies (default: config/shortlist.json).")
    parser.add_argument("--archive-root", type=Path, action="append", required=True,
                        help="Data root holding data/raw/sec/<accession>/source_manifest.json; repeatable.")
    parser.add_argument("--discovery", type=Path,
                        help="Acquisition discovery.json, so filings without an archive are reported as missing.")
    parser.add_argument("--ticker", action="append", help="Restrict to these tickers (diagnostic runs).")
    parser.add_argument("--output", type=Path, required=True, help="New or empty output directory.")
    args = parser.parse_args(argv)
    try:
        scope = load_scope(args.config)
        if args.output.exists() and any(args.output.iterdir()):
            raise CoverageError(f"{args.output} is not empty; choose a new output directory.")
        discovery = json.loads(args.discovery.read_text(encoding="utf-8")) if args.discovery else None
        report = assess_archives(scope, args.archive_root, discovery, tickers=args.ticker,
                                 progress=lambda text: print(text, file=sys.stderr, flush=True))
        paths = write_outputs(report, args.output)
    except (CoverageError, OSError, ValueError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    totals = report["totals"]
    print(json.dumps({"status": report["status"], "outputs": [str(p) for p in paths],
                      **{key: totals[key] for key in ("filings", *STATUSES, "facts_extracted")}}, indent=2))
    return 0 if report["status"].startswith("complete") else 2


if __name__ == "__main__":
    raise SystemExit(main())
