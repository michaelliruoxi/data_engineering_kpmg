"""Confirmed scope, original evidence and incomplete layers; synthetic unit data."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from sec_pipeline.database import DatabaseError, FinancialTableInput
from sec_pipeline import shortlist_table_validation as scope
from sec_pipeline.table_source_reconstruction import reconstruct_original_tables
from sec_pipeline import tables


ACCESSION = "0000000001-26-000001"
MISSING = "0000000001-26-000002"


def scope_files(tmp_path):
    config = {"scope_status": "user_confirmed", "since": "2021-01-01", "until": "2026-12-31", "forms": ["10-K", "10-Q"],
        "amendments_included": False, "companies": [{"ticker": "AAPL", "cik": "0000320193"}]}
    audit = {"scope": {"tickers": ["AAPL"], "forms": ["10-K", "10-Q"], "filing_date_from": "2021-01-01", "filing_date_to": "2026-12-31",
        "snapshot_latest_filing_date": "2026-10-28", "amendments_included": False}, "filings_checked": 2,
        "filings": [{"ticker": "AAPL", "form": "10-Q", "accession_number": a, "filing_date": "2026-10-28", "status": "passed",
            "source_sha256": "a" * 64, "source_url": "https://www.sec.gov/Archives/edgar/data/1/" + a.replace("-", "") + "/source.htm"}
            for a in (ACCESSION, MISSING)]}
    paths = tmp_path / "config.json", tmp_path / "sections.json"
    for p, value in zip(paths, (config, audit)):
        p.write_text(json.dumps(value))
    return paths


def test_confirmed_snapshot_does_not_assert_future_or_table_acceptance(tmp_path):
    config, audit = scope_files(tmp_path)
    result = scope.load_scope(config, audit)
    assert len(result["filings"]) == 2
    assert result["snapshot_latest_filing_date"] == "2026-10-28"
    assert "snapshot_only" in result["scope_status"]


@pytest.mark.parametrize("defect", ["date", "form", "duplicate", "count", "traversal", "scope"])
def test_scope_conflicts_never_drop_expected_accessions_silently(tmp_path, defect):
    config, audit = scope_files(tmp_path)
    data = json.loads(audit.read_text())
    if defect == "date": data["filings"][0]["filing_date"] = "2020-12-31"
    elif defect == "form": data["filings"][0]["form"] = "10-Q/A"
    elif defect == "duplicate": data["filings"][1] = deepcopy(data["filings"][0])
    elif defect == "count": data["filings_checked"] = 233
    elif defect == "traversal": data["filings"][0]["accession_number"] = "../../../private"
    else: data["scope"]["filing_date_from"] = "2021-10-13"
    audit.write_text(json.dumps(data))
    with pytest.raises(DatabaseError): scope.load_scope(config, audit)


def test_not_ingested_and_missing_xml_receipts_remain_visible(tmp_path):
    config, audit = scope_files(tmp_path)
    definition = scope.load_scope(config, audit)
    rows = scope.source_reconciliation(definition, {"filings": []}, [tmp_path], tickers={"AAPL"})
    assert len(rows) == 4
    xml = [r for r in rows if r["file_type"] == "extracted_xbrl_instance"]
    assert all(r["source_url"] is None and r["expected_sha256"] is None and r["source_document_id"] is None for r in xml)
    assert all(r["source_status"] == "receipt_unknown" for r in xml)


def test_existing_bytes_require_matching_hash_and_different_receipts_stay_explicit(tmp_path):
    config, audit = scope_files(tmp_path)
    definition = scope.load_scope(config, audit)
    directory = tmp_path / "data/raw/sec" / ACCESSION
    directory.mkdir(parents=True)
    (directory / "source.htm").write_bytes(b"a different public source version")
    rows = scope.source_reconciliation(definition, {"filings": []}, [tmp_path], tickers={"AAPL"})
    html = next(r for r in rows if r["accession"] == ACCESSION and r["file_type"] == "main_inline_xbrl_filing")
    assert html["local_exists"] and html["source_status"] == "hash_mismatch"
    assert (directory / "source.htm").read_bytes() == b"a different public source version"


def test_conflicting_recorded_hashes_are_not_raw_byte_or_financial_value_failures(tmp_path):
    config, audit = scope_files(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()
    definition = json.loads(audit.read_text())
    source_url = definition["filings"][0]["source_url"]
    (capture / "shared-inventory.json").write_text(json.dumps({"filings": [{"id": "filing", "ticker": "AAPL", "cik": "0000320193",
        "accession_number": ACCESSION, "form": "10-Q", "filing_date": "2026-10-28"}], "filing_processing_sets": [],
        "source_processing_sets": [], "source_documents": [{"id": "source", "filing_id": "filing", "role": "main_inline_xbrl_filing",
            "sha256": "b" * 64, "source_url": source_url, "local_path": "missing.htm"}]}))
    output = tmp_path / "out"
    assert scope.run_scoped_validation(config, audit, capture, [tmp_path], output) == 2
    import csv
    conflicts = list(csv.DictReader((output / "validation_provenance_conflicts.csv").open()))
    errors = list(csv.DictReader((output / "validation_errors.csv").open()))
    gaps = list(csv.DictReader((output / "validation_evidence_gaps.csv").open()))
    assert len(conflicts) == 1 and errors == []
    assert conflicts[0]["issue_group"] == "provenance_conflict"
    assert any(g["check"] == "original_main_inline_xbrl_filing_missing_original" for g in gaps)


def test_paragraph_table_ids_are_not_promoted_to_actual_table_chunks():
    snapshot = {"accession": ACCESSION, "chunks": [
        {"id": "paragraph", "metadata": {"table_ids": ["not-a-producer-contract"]}},
        {"id": "explicit", "metadata": {"chunk_type": "table", "chunk_id": "producer", "accession": ACCESSION}}]}
    records = scope.explicit_table_chunks(snapshot)
    assert len(records) == 1 and records[0]["chunk_id"] == "producer"
    assert "table_ids" not in records[0] and "synthetic" not in records[0]


def context_snapshot(xml):
    return {"accession": ACCESSION, "selection": None, "facts": [{"id": "fact", "source_document_id": "xml", "period_kind": "instant",
        "instant_date": "2026-04-25", "occurrence_key": "F1", "source_reference": {"element_id": "F1", "context_ref": "C1"}}],
        "table_cells": [{"id": "cell", "cell_key": "r001c001"}], "sources": [{"id": "xml", "sha256": hashlib.sha256(xml).hexdigest()}],
        "cell_source_fact_bindings": [{"id": "binding", "fact_id": "fact", "cell_id": "cell", "table_id": "table"}],
        "source_quality_issues": [{"binding_id": "binding", "code": "opening_balance_date_not_prior_day"}]}


XML = b'''<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:g="urn:synthetic"><context id="C1"><period><instant>2026-04-25</instant></period></context><g:Cash id="F1" contextRef="C1">2075000000</g:Cash></xbrl>'''


def test_original_context_recheck_does_not_correct_opening_date():
    snapshot = context_snapshot(XML)
    original = deepcopy(snapshot)
    [result] = scope.review_contexts(snapshot, {"xml": XML})
    assert result["status"] == "original_XML_context_rechecked_not_adjudicated"
    assert "2026-04-25" in result["original_context_xml"] and "2026-04-24" not in result["original_context_xml"]
    assert not result["dates_modified"] and snapshot == original


def test_no_original_context_and_changed_hash_are_not_passes():
    snapshot = context_snapshot(XML)
    assert scope.review_contexts(snapshot, {})[0]["status"] == "original_XML_context_unavailable"
    assert scope.review_contexts(snapshot, {"xml": XML + b" "})[0]["status"] == "original_XML_hash_mismatch"


def test_reconstruction_compares_original_headings_not_just_stored_json(monkeypatch):
    source, xml = b"<html><body><table><tr><td>100</td></tr></table></body></html>", b"<x/>"
    cell = {"cell_id": "r000c000", "display_text": "100", "column_headings": ["2026"], "column_period": {"instant": "2026-06-30"}}
    table = FinancialTableInput(filing_id=UUID(int=1), source_document_id=UUID(int=2), extraction_version="tables-v1", table_key="income_statement",
        statement_type="income_statement", readable_text="100", structure={"source_xpath": "/html/body/table", "rows": [[cell]],
        "fact_scope": {"xml_document_id": str(UUID(int=3)), "facts_version": "facts-v1"},
        "html_sha256": hashlib.sha256(source).hexdigest(), "xml_sha256": hashlib.sha256(xml).hexdigest()})
    from sec_pipeline.table_sections import canonical
    from dataclasses import asdict
    snapshot = {"tables": [canonical(asdict(table))]}
    sources = {str(UUID(int=2)): source, str(UUID(int=3)): xml}
    monkeypatch.setattr(tables, "extract_tables", lambda *a, **kw: [table])
    assert reconstruct_original_tables(snapshot, sources)["status"] == "passed"
    different = deepcopy(table.structure)
    different["rows"][0][0]["column_headings"] = ["2025"]
    monkeypatch.setattr(tables, "extract_tables", lambda *a, **kw: [replace(table, structure=different)])
    result = reconstruct_original_tables(snapshot, sources)
    assert result["status"] == "failed"
    assert result["groups"][0]["differences"][0]["fields"] == ["column_headings"]


def test_full_reconciliation_keeps_uningested_rows_and_no_fake_counts(tmp_path):
    config, audit = scope_files(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()
    (capture / "shared-inventory.json").write_text(json.dumps({"filings": [], "filing_processing_sets": [], "source_processing_sets": [],
        "identity": {"queried_at": "synthetic-unit-fixture-not-live"}, "sources": []}))
    output = tmp_path / "out"
    assert scope.run_scoped_validation(config, audit, capture, [tmp_path], output) == 2
    import csv
    rows = list(csv.DictReader((output / "validation_summary.csv").open()))
    assert len(rows) == 2 and all(r["validation_status"] == "not_ingested" and r["cell_count"] == "" for r in rows)
    assert all(r["section_audit_reported_status"] == "passed" and r["original_source_semantics_status"] == "unverified" for r in rows)
    assert json.loads((output / "run_status.json").read_text())["future_2026_completeness_asserted"] is False
