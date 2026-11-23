"""Read-only fiscal label QA against period-effective EDGAR calendar rules."""
from calendar import monthrange
from datetime import date, timedelta
import json
import re
from uuid import UUID

from .fiscal_table_qa import identity, verify_fiscal_outputs


WEEK_TOLERANCE_DAYS = 10
YEAR_FOCUS = "DocumentFiscalYearFocus"
PERIOD_FOCUS = "DocumentFiscalPeriodFocus"
DEI_FIELDS = (YEAR_FOCUS, PERIOD_FOCUS, "DocumentPeriodEndDate", "DocumentType")


def month_end(year, month, months_before=0):
    year, month_index = divmod(year * 12 + month - 1 - months_before, 12)
    return date(year, month_index + 1, monthrange(year, month_index + 1)[1])


def annual_end(rule, year):
    anchor = month_end(year, rule["year_end_month"])
    if rule["basis"] == "month_end":
        return anchor
    if rule["basis"] not in {"last_weekday", "nearest_weekday"} or rule.get("weekday") not in range(7):
        raise ValueError("Fiscal end weekday or calendar basis is invalid.")
    delta = (rule["weekday"] - anchor.weekday()) % 7
    offset = delta if delta == 0 or (rule["basis"] == "nearest_weekday" and delta <= 3) else delta - 7
    return anchor + timedelta(days=offset)


def expected_end(rule, year, quarter):
    close = annual_end(rule, year)
    nominal = month_end(year, rule["year_end_month"], (4 - quarter) * 3)
    if rule["basis"] == "month_end":
        return nominal, nominal, None
    previous = annual_end(rule, year - 1)
    days = (close - previous).days
    if days not in (364, 371):
        raise ValueError("Week calendar must span 52 or 53 weeks.")
    weeks = days // 7
    if quarter == 4:
        return close, nominal, weeks
    extra_quarter = rule.get("extra_week_quarter", {}).get(str(year))
    if weeks == 53 and extra_quarter not in (1, 2, 3, 4):
        return None, nominal, weeks
    extra = int(weeks == 53 and extra_quarter <= quarter)
    return previous + timedelta(weeks=13 * quarter + extra), nominal, weeks


def calculate_fiscal(row, rules):
    """Use the reporting-period date and evidence-supported quarter boundaries."""
    actual_text = row.get("reporting_period_end")
    result = {"actual_period_end": actual_text, "status": "review_required"}
    try:
        actual = date.fromisoformat(actual_text)
    except (TypeError, ValueError):
        return result | {"reason": "missing_or_invalid_reporting_period_end"}
    form = row.get("form")
    if form not in {"10-K", "10-Q"}:
        return result | {"reason": "unsupported_or_transition_form"}
    scoped = [rule for rule in rules if rule["cik"] == row["CIK"] and rule["evidence"]]
    matches, unresolved, compared = [], [], []
    for rule in scoped:
        tolerance = 0 if rule["basis"] == "month_end" else WEEK_TOLERANCE_DAYS
        for year in range(max(actual.year - 1, rule["first_fy"]), min(actual.year + 1, rule["last_fy"]) + 1):
            for quarter in ((4,) if form == "10-K" else (1, 2, 3)):
                expected, nominal, weeks = expected_end(rule, year, quarter)
                evidence = {"fiscal_year": year, "period": "FY" if quarter == 4 else f"Q{quarter}",
                            "expected_period_end": expected.isoformat() if expected else None,
                            "expected_fiscal_year_end": annual_end(rule, year).isoformat(),
                            "nominal_period_end": nominal.isoformat(),
                            "nominal_deviation_days": (actual - nominal).days, "fiscal_weeks": weeks,
                            "tolerance_days": tolerance, "calendar_basis": rule["basis"],
                            "calendar_evidence": rule["evidence"]}
                if expected is None:
                    previous = annual_end(rule, year - 1)
                    possible = [previous + timedelta(weeks=13 * quarter + offset) for offset in (0, 1)]
                    if any(abs((actual - end).days) <= tolerance for end in possible):
                        unresolved.append(evidence | {"possible_period_ends": [day.isoformat() for day in possible]})
                    continue
                delta = (actual - expected).days
                evidence["deviation_days"] = delta
                compared.append(evidence)
                if abs(delta) <= tolerance:
                    matches.append(evidence)
    if len(matches) == 1 and not unresolved:
        hit = matches[0]
        return result | hit | {"status": "exact" if hit["deviation_days"] == 0 else "within_10_days"}
    if unresolved or len(matches) > 1:
        reason = "53rd_week_quarter_unconfirmed" if unresolved and not matches else "calendar_match_not_unique"
        return result | {"reason": reason, "calendar_candidates": matches + unresolved}
    nearest_delta = min((abs(hit["deviation_days"]) for hit in compared), default=None)
    return result | {"reason": "outside_calendar_tolerance" if compared else "no_period_effective_calendar",
                     "calendar_candidates": [hit for hit in compared if abs(hit["deviation_days"]) == nearest_delta]}


def _labels(year, period):
    year, period = str(year).strip(), str(period).strip().upper()
    return (int(year), period) if re.fullmatch(r"[0-9]{4}", year) and period in {"FY", "Q1", "Q2", "Q3", "Q4"} else None


def inspect_dei(filing, scopes):
    """Keep every raw assertion, but compare only period- and issuer-scoped pairs."""
    expected_end = filing["reporting_period_end"]
    records, labels = [], set()
    for scope in scopes:
        if scope["filing_id"] != filing["filing_id"]:
            raise ValueError("DEI evidence has a different filing ID for this accession.")
        source = scope["source"]
        values, facts = {}, []
        for fact in scope["facts"]:
            name = fact["concept_name"]
            if name not in DEI_FIELDS:
                continue
            ref = fact.get("source_reference") or {}
            issuer = (ref.get("entity") or {}).get("identifier")
            context_end = fact.get("period_end") if fact.get("period_kind") == "duration" else fact.get("instant_date")
            qualified = bool(source and fact.get("filing_id") == filing["filing_id"]
                and fact.get("source_document_id") == source["id"]
                and fact.get("concept_namespace", "").startswith(("http://xbrl.sec.gov/dei/", "https://xbrl.sec.gov/dei/"))
                and not fact.get("is_nil") and not fact.get("dimensions") and issuer
                and str(issuer).zfill(10) == filing["CIK"] and context_end == expected_end
                and isinstance(fact.get("raw_value"), str))
            if qualified:
                values.setdefault(name, set()).add(fact["raw_value"])
            facts.append({"fact_id": fact.get("id"), "name": name, "raw_value": fact.get("raw_value"),
                          "context_end": context_end, "context_ref": ref.get("context_ref"), "xpath": ref.get("xpath"),
                          "qualified": qualified})
        unique = all(len(values.get(field, ())) == 1 for field in DEI_FIELDS)
        pair = None
        if unique and values["DocumentPeriodEndDate"] == {expected_end} and values["DocumentType"] == {filing["form"]}:
            pair = _labels(next(iter(values[YEAR_FOCUS])), next(iter(values[PERIOD_FOCUS])))
        if pair:
            labels.add(pair)
        records.append({"selection": scope["selection"], "source": source, "label": pair, "facts": facts})
    if len(labels) == 1 and all(record["label"] for record in records):
        year, period = next(iter(labels))
        return {"status": "stored_context_qualified", "fiscal_year": year, "period": period, "scopes": records}
    return {"status": "review_required" if records else "evidence_missing", "scopes": records}


def compare_labels(left, right):
    first = _labels(left.get("fiscal_year"), left.get("period"))
    second = _labels(right.get("fiscal_year"), right.get("period"))
    return ("match" if first == second else "mismatch") if first and second else "unverified"


def inspect_publication(filing, publications, snapshots):
    """Compare independent official exports to exact receiving sets."""
    if not publications:
        return {"status": "unverified_official_output_unavailable", "table_inheritance": "unverified",
                "cell_inheritance": "unverified", "exports": []}
    logs, pairs = [], set()
    for publication in publications:
        selection = publication["selection"]
        family, set_id = selection["contract"], selection["id"]
        # UUID syntax and a fixed family keep path construction inside the capture.
        if (family not in {"legacy", "source-v4"} or str(UUID(set_id)) != set_id
                or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", filing["accession"])):
            raise ValueError("Published processing set has an invalid receiving identity.")
        if not snapshots:
            raise ValueError("A receiving snapshot directory is required for publication checks.")
        path = snapshots / filing["accession"] / f"{family}--{set_id}" / "snapshot.json"
        snapshot = json.loads(path.read_text())
        if identity(snapshot) != (filing["accession"], filing["filing_id"], family, set_id):
            raise ValueError("Published scope differs from the selected receiving snapshot.")
        exports = publication["exports"]
        matching = [row for row in exports.get("filings", []) if row.get("filing_id") == filing["filing_id"]]
        raw_labels = [{"fiscal_year": row.get("fiscal_year"), "period": row.get("fiscal_period")} for row in matching]
        label = _labels(raw_labels[0]["fiscal_year"], raw_labels[0]["period"]) if len(raw_labels) == 1 else None
        if label:
            pairs.add(label)
        checked = verify_fiscal_outputs(snapshot, exports)
        issues = checked.get("issues", [])
        layers = {}
        for layer, source_layer in (("table", "tables"), ("cell", "cells")):
            layers[layer] = ("unverified" if checked["consistency"] in {
                "downstream_output_missing", "no_receiving_tables_or_cells"}
                else "failed" if any(issue.get("layer") in (None, source_layer) for issue in issues)
                else "passed")
        anomalous = {}
        for layer in ("tables", "cells"):
            ids = {issue["id"] for issue in issues if issue.get("layer") == layer}
            anomalous[layer] = [row for row in exports.get(layer, []) if row.get("id") in ids]
        logs.append({"selection": selection, "publication_evidence": publication["evidence"],
                     "raw_filing_labels": raw_labels, "label": label, "inheritance": layers,
                     "issues": issues, "anomalous_rows": anomalous})
    result = {"exports": logs}
    unique = len(pairs) == 1 and all(log["label"] for log in logs)
    if unique:
        result["fiscal_year"], result["period"] = next(iter(pairs))
    for layer in ("table", "cell"):
        statuses = {log["inheritance"][layer] for log in logs}
        result[f"{layer}_inheritance"] = ("failed" if "failed" in statuses else
                                          "passed" if unique and statuses == {"passed"} else "unverified")
    return result | {"status": "published_label_available" if unique else "review_required"}
