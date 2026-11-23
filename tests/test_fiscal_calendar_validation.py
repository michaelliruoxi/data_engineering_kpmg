from copy import deepcopy
import json
from pathlib import Path

import pytest

from sec_pipeline.fiscal_calendar_validation import (
    calculate_fiscal, compare_labels, inspect_dei, inspect_publication,
)

RULES = json.loads((Path(__file__).resolve().parents[1] / "config/graphrag_fiscal_calendars.json").read_text())["rules"]


@pytest.mark.parametrize(("cik", "actual", "form", "label", "weeks"), [
    ("0000104169", "2026-07-31", "10-Q", (2027, "Q2"), None),
    ("0000721371", "2021-09-30", "10-Q", (2022, "Q1"), None),
    ("0000050863", "2022-04-02", "10-Q", (2022, "Q1"), 53),
    ("0000050863", "2023-04-01", "10-Q", (2023, "Q1"), 52),
    ("0000106040", "2025-10-03", "10-Q", (2026, "Q1"), 53),
    ("0000106040", "2026-01-02", "10-Q", (2026, "Q2"), 53),
    ("0000106040", "2026-07-03", "10-K", (2026, "FY"), 53),
    ("0000106040", "2022-07-01", "10-K", (2022, "FY"), 52),
])
def test_historical_calendar_uses_reporting_period(cik, actual, form, label, weeks):
    result = calculate_fiscal({"CIK": cik, "reporting_period_end": actual, "form": form,
                               "filing_date": "2099-01-01"}, RULES)
    assert (result["fiscal_year"], result["period"]) == label
    assert (result["expected_period_end"], result["deviation_days"], result["fiscal_weeks"]) == (actual, 0, weeks)


def test_week_tolerance_is_inclusive_and_stops_at_11_days():
    row = {"CIK": "0000050863", "reporting_period_end": "2022-04-12", "form": "10-Q"}
    result = calculate_fiscal(row, RULES)
    assert (result["status"], result["deviation_days"]) == ("within_10_days", 10)
    result = calculate_fiscal(row | {"reporting_period_end": "2022-04-13"}, RULES)
    assert result["reason"] == "outside_calendar_tolerance"
    assert result["calendar_candidates"][0]["deviation_days"] == 11


def test_no_extrapolation_or_tie_breaking():
    row = {"CIK": "0000050863", "reporting_period_end": "2022-04-02", "form": "10-Q"}
    rule = next(r for r in RULES if r["cik"] == row["CIK"])
    assert calculate_fiscal(row, [rule, deepcopy(rule)])["reason"] == "calendar_match_not_unique"
    actual = calculate_fiscal(row, [rule | {"extra_week_quarter": {}}])
    assert actual["reason"] == "53rd_week_quarter_unconfirmed"
    assert actual["calendar_candidates"][0]["possible_period_ends"] == ["2022-03-26", "2022-04-02"]
    later = calculate_fiscal(row | {"reporting_period_end": "2030-03-30"}, RULES)
    assert later["reason"] == "no_period_effective_calendar"
    assert calculate_fiscal(row | {"form": "10-QT"}, RULES)["reason"] == "unsupported_or_transition_form"


def test_distinct_historical_rules_for_the_same_cik():
    issuer = {"CIK": "0000000001", "form": "10-K"}
    calendar = {"cik": issuer["CIK"], "basis": "month_end", "evidence": [{"url": "fixture"}]}
    rules = [calendar | {"year_end_month": 12, "first_fy": 2021, "last_fy": 2023},
             calendar | {"year_end_month": 6, "first_fy": 2025, "last_fy": 2026}]
    assert calculate_fiscal(issuer | {"reporting_period_end": "2023-12-31"}, rules)["fiscal_year"] == 2023
    assert calculate_fiscal(issuer | {"reporting_period_end": "2025-06-30"}, rules)["fiscal_year"] == 2025
    assert calculate_fiscal(issuer | {"reporting_period_end": "2024-06-30"}, rules)["status"] == "review_required"


@pytest.fixture
def filing():
    return {"accession": "0000106040-25-047613", "CIK": "0000106040", "filing_id": "filing",
            "form": "10-Q", "reporting_period_end": "2025-10-03"}


def test_dei_selects_same_context_end_and_preserves_raw_conflicts(filing):
    common = {"filing_id": "filing", "source_document_id": "source", "concept_namespace": "http://xbrl.sec.gov/dei/2025",
              "period_kind": "duration", "period_end": "2025-10-03", "is_nil": False, "dimensions": [],
              "source_reference": {"entity": {"identifier": "106040"}, "xpath": "/x", "context_ref": "q1"}}
    values = {"DocumentFiscalYearFocus": "2026", "DocumentFiscalPeriodFocus": "Q1",
              "DocumentPeriodEndDate": "2025-10-03", "DocumentType": "10-Q"}
    scope = {"filing_id": "filing", "selection": {"contract": "legacy", "id": "set"}, "source": {"id": "source"},
             "facts": [common | {"concept_name": name, "raw_value": value} for name, value in values.items()]}
    stray = common | {"concept_name": "DocumentFiscalYearFocus", "raw_value": "2025", "period_end": "2024-09-27"}
    scope["facts"].append(stray)
    result = inspect_dei(filing, [scope])
    assert compare_labels(calculate_fiscal(filing, RULES), result) == "match"
    assert result["scopes"][0]["facts"][-1]["raw_value"] == "2025"
    assert result["scopes"][0]["facts"][-1]["qualified"] is False
    scope["facts"][-1]["period_end"] = "2025-10-03"
    assert inspect_dei(filing, [scope])["status"] == "review_required"


def test_official_publication_checks_table_and_cell_inheritance_separately(filing, tmp_path):
    set_id = "cb9527af-9730-42d6-9249-3dd992f64011"
    selection = {"contract": "legacy", "id": set_id}
    path = tmp_path / filing["accession"] / f"legacy--{set_id}"
    path.mkdir(parents=True)
    (path / "snapshot.json").write_text(json.dumps({
        "accession": filing["accession"], "filing": {"id": "filing"}, "selection": selection,
        "selected_processing_set": {"id": set_id, "filing_id": "filing"},
        "tables": [{"id": "t"}], "table_cells": [{"id": "c", "table_id": "t"}],
    }))
    official = {"filing_id": "filing", "fiscal_year": "2026", "fiscal_period": "Q2"}
    row = official | {"processing_family": "legacy", "processing_set_id": set_id}
    release = {"selection": selection, "evidence": "fixture-only",
               "exports": {"filings": [official], "tables": [row | {"id": "t"}],
                           "cells": [row | {"id": "c", "table_id": "t", "fiscal_period": "Q1"}]}}
    result = inspect_publication(filing, [release], tmp_path)
    assert compare_labels(calculate_fiscal(filing, RULES), result) == "mismatch"
    assert (result["table_inheritance"], result["cell_inheritance"]) == ("passed", "failed")
    assert result["exports"][0]["raw_filing_labels"] == [{"fiscal_year": "2026", "period": "Q2"}]
    assert result["exports"][0]["anomalous_rows"]["cells"][0]["fiscal_period"] == "Q1"
    assert inspect_publication(filing, [], None)["table_inheritance"] == "unverified"
