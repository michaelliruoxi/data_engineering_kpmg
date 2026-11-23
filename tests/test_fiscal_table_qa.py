from copy import deepcopy

import pytest

from sec_pipeline.fiscal_table_qa import compare_lineage, verify_fiscal_outputs
from sec_pipeline.historical_table_validation import GRAPHRAG_COMPANIES, GRAPHRAG_DATES, merge_candidates


@pytest.fixture
def snapshot():
    return {
        "accession": "0001652044-26-000001", "filing": {"id": "filing", "fiscal_year": None},
        "selection": {"id": "set", "contract": "legacy"},
        "selected_processing_set": {"id": "set", "filing_id": "filing",
                                    "fact_source_document_id": "xml", "fact_extraction_version": "v3"},
        "tables": [{"id": "table", "filing_id": "filing", "source_document_id": "html",
                    "structure": {"source_xpath": "/html/body/table"}}],
        "table_cells": [{"id": "cell", "table_id": "table", "source_locator": {"xpath": "/html/body/table/tr/td"}}],
        "cell_fact_links": [{"cell_id": "cell", "fact_id": "fact", "table_id": "table", "evidence": {"scale": 3}}],
        "financial_table_facts": [{"table_id": "table", "fact_id": "fact"}],
        "facts": [{"id": "fact", "filing_id": "filing", "source_document_id": "xml", "extraction_version": "v3",
                   "period_end": "2025-12-31", "source_reference": {"xpath": "/fact"}}],
        "sources": [{"id": "html", "sha256": "test-html"}, {"id": "xml", "sha256": "test-xml"}],
    }


@pytest.fixture
def captures():
    common = {"database_id": "test-db", "deployment_ref": "change-1"}
    return (common | {"phase": "before", "captured_at_utc": "2026-11-22T17:00:00Z"},
            common | {"phase": "after", "captured_at_utc": "2026-11-23T17:00:00Z",
                      "deployed_at_utc": "2026-11-23T12:00:00Z"})


def test_changed_filing_label_preserves_exact_lineage(snapshot, captures):
    after = deepcopy(snapshot)
    after["filing"]["fiscal_year"] = 2025
    after["table_cells"][0]["fiscal_period"] = "FY"
    after["tables"][0]["fiscal_year"] = 2025
    assert compare_lineage(snapshot, after, *captures)["status"] == "lineage_unchanged"


@pytest.mark.parametrize(("relation", "field", "value"), [
    ("tables", "structure", {"source_xpath": "/another/table"}),
    ("table_cells", "table_id", "wrong-parent"),
    ("cell_fact_links", "evidence", {"scale": 0}),
    ("facts", "period_end", "2026-01-01"),
    ("sources", "sha256", "other-bytes"),
])
def test_same_counts_do_not_hide_lineage_changes(snapshot, captures, relation, field, value):
    after = deepcopy(snapshot)
    after[relation][0][field] = value
    result = compare_lineage(snapshot, after, *captures)
    assert result["status"] == "lineage_changed"
    assert result["relations"][relation]["updated"][0]["fields"] == [field]


def test_missing_baseline_or_changed_scope_cannot_pass(snapshot, captures):
    assert compare_lineage(None, snapshot, None, captures[1])["status"] == "baseline_missing"
    before, after = captures
    assert compare_lineage(snapshot, snapshot, before, after | {"database_id": "other"})["status"] == (
        "database_identity_unverified_or_changed")
    assert compare_lineage(snapshot, snapshot, before | {"captured_at_utc": after["captured_at_utc"]}, after)["status"] == (
        "capture_timing_does_not_bracket_deployment")
    changed = deepcopy(snapshot)
    changed["selection"]["id"] = changed["selected_processing_set"]["id"] = "other"
    assert compare_lineage(snapshot, changed, *captures)["status"] == "processing_set_changed"
    changed = deepcopy(snapshot)
    changed["selected_processing_set"]["fact_extraction_version"] = "other-version"
    assert compare_lineage(snapshot, changed, *captures)["status"] == "processing_version_or_document_scope_changed"


def test_fiscal_consistency_is_distinct_from_dei_correctness(snapshot):
    labels = {"filing_id": "filing", "fiscal_year": 2025, "fiscal_period": "FY"}
    scoped = labels | {"processing_set_id": "set", "processing_family": "legacy"}
    exports = {"filings": [labels], "tables": [scoped | {"id": "table"}],
               "cells": [scoped | {"id": "cell", "table_id": "table"}]}
    assert verify_fiscal_outputs(snapshot, None)["consistency"] == "downstream_output_missing"
    result = verify_fiscal_outputs(snapshot, exports)
    assert (result["consistency"], result["correctness"]) == ("passed", "unverified")
    expected = scoped | {"fiscal_year": 2024, "verified_dei": True}
    assert verify_fiscal_outputs(snapshot, exports, expected)["correctness"] == "disagrees_with_verified_dei"
    exports["cells"] = [scoped | {"id": "other", "table_id": "wrong", "fiscal_year": 2026}] * 2
    issues = verify_fiscal_outputs(snapshot, exports)["issues"]
    assert {issue["check"] for issue in issues} == {
        "duplicate_output_row", "missing_output_row", "output_identity_or_parent_mismatch",
        "output_labels_absent_or_inconsistent",
    }


def test_graphrag_scope_uses_cik_and_original_filing_date():
    alphabet = {"ticker": "GOOG", "cik": "0001652044", "form": "10-K", "id": "first",
                "filing_date": "2021-01-01", "accession_number": "0001652044-21-000001"}
    db_rows = [alphabet, alphabet | {"accession_number": "0001652044-20-000001", "filing_date": "2020-12-31"},
               alphabet | {"accession_number": "0001652044-21-000002", "form": "10-K/A"}]
    acquired = {"ticker": "GOOGL", "cik": "0001652044", "form": "10-Q",
                "filing_date": "2026-12-31", "accession_number": "0001652044-26-000003"}
    rows, conflicts, _ = merge_candidates({"filings": db_rows}, [("Acquisition", [acquired])],
                                          GRAPHRAG_COMPANIES, GRAPHRAG_DATES)
    assert not conflicts
    assert [(row["ticker"], row["filing_id"], row["candidate_sources"]) for row in rows] == [
        ("GOOGL", "first", ["shared_database"]), ("GOOGL", None, ["Acquisition"]),
    ]


def test_source_requirements_include_alphabet_with_no_db_ticker(tmp_path):
    from sec_pipeline.historical_table_validation import source_requirements

    filing = {"ticker": None, "cik": "0001652044", "form": "10-Q", "id": "f",
              "filing_date": "2024-03-15", "accession_number": "0001652044-24-000001"}
    receipt = {"filing_id": "f", "local_path": "original.htm", "role": "main_inline_xbrl_filing",
               "source_url": "https://www.sec.gov/original.htm", "sha256": "sha", "id": "html"}
    rows = source_requirements({"filings": [filing], "source_documents": [receipt]},
                               GRAPHRAG_COMPANIES, [tmp_path], date_range=GRAPHRAG_DATES)
    assert [(row["ticker"], row["hash_status"]) for row in rows] == [("GOOGL", "missing")]
