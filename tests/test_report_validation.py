from dataclasses import replace
import hashlib
import json

import pytest

from sec_pipeline.chunking import chunk_report
from sec_pipeline.report_cleaning import CleanedReport, clean_report
from sec_pipeline.report_validation import audit_manifest, audit_report, main, source_heading_expectations
from uuid import UUID


@pytest.fixture
def original(tmp_path):
    path = tmp_path / "filing.htm"
    path.write_text("""<html><body><p>FORM 10-Q</p>
        <h1 id="part">PART I. FINANCIAL INFORMATION</h1>
        <h2 id="statements">Item 1. Financial Statements</h2>
        <p>Revenue was $100 million this quarter.</p>
        <h2 id="mda">Item 2. Management's Discussion and Analysis</h2>
        <p>Revenue increased due to demand.</p></body></html>""")
    return path


EXPECTED = ["part-i", "part-i-item-1", "part-i-item-2"]


def checks(result):
    return {issue["check"] for issue in result["issues"]}


def test_independent_expected_items_and_original_locators_pass(original):
    result = audit_report(original, form="10-Q", expected_section_ids=EXPECTED)
    assert result["status"] == "passed"
    assert result["source_sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
    assert result["chunk_count"] >= 3


def test_parser_output_cannot_establish_its_own_heading_completeness(original):
    result = audit_report(original, form="10-Q")
    assert result["status"] == "unverified"
    assert result["heading_inventory_status"] == "unverified"
    assert not result["issues"]


def test_missing_heading_is_failure_even_if_all_text_and_chunks_are_covered(original):
    result = audit_report(original, form="10-Q", expected_section_ids=EXPECTED + ["part-i-item-4"])
    assert result["status"] == "failed"
    assert result["issues"] == [{"check": "section_inventory_mismatch", "missing": ["part-i-item-4"], "unexpected": []}]


def test_wrong_existing_original_locator_is_not_accepted(original):
    report = clean_report(original, form="10-Q")
    report.sections[-1]["source_locator"]["xpath"] = "/html/body/p[2]"
    result = audit_report(original, form="10-Q", expected_section_ids=EXPECTED, report=report)
    assert {"heading_source_mismatch", "source_element_id_mismatch"} <= checks(result)


def test_toc_link_cannot_pass_as_body_heading(tmp_path):
    path = tmp_path / "toc.htm"
    path.write_text('<html><body><p><a href="#body">Item 1. Financial Statements</a></p>'
                    '<p id="body">Financial statements begin here.</p></body></html>')
    text = "Item 1. Financial Statements\n\nFinancial statements begin here."
    report = CleanedReport(text, [{"section_id": "part-i-item-1", "title": "Item 1. Financial Statements",
        "original_title": "Item 1. Financial Statements", "section_type": "financial_statements", "item": "1",
        "offset_start": 0, "offset_end": len(text), "source_locator": {"source_file": path.name, "xpath": "/html/body/p[1]"}}])
    result = audit_report(path, form="10-Q", expected_section_ids=["part-i-item-1"], report=report)
    assert "section_starts_in_toc_link" in checks(result)


def test_repeated_pipes_and_chunk_corruption_are_observable(original):
    report = clean_report(original, form="10-Q")
    report.full_text += "\nTotal revenue | | | 100"
    report.sections[-1]["offset_end"] = len(report.full_text)
    chunks = chunk_report(filing_id=UUID(int=1), report_id=UUID(int=2),
                          full_text=report.full_text, sections=report.sections)
    chunks[0] = replace(chunks[0], text="not an exact slice")
    result = audit_report(original, form="10-Q", expected_section_ids=EXPECTED, report=report, chunks=chunks)
    assert {"repeated_pipes", "chunk_not_exact_slice", "chunk_repeated_pipes", "uncovered_chunk_text"} <= checks(result)
    assert result["repeated_pipe_count"] == 1


def test_unknown_taxonomy_of_recognized_annual_item_is_not_unclassified(tmp_path):
    path = tmp_path / "annual.htm"
    path.write_text("<html><body><p>Item 1. Business</p><p>Our operations.</p>"
                    "<p>Item 1B. Unresolved Staff Comments</p><p>None.</p></body></html>")
    result = audit_report(path, form="10-K", expected_section_ids=["part-i-item-1", "part-i-item-1b"])
    assert result["status"] == "passed"
    assert result["unclassified_characters"] == 0


def test_unclassified_source_content_cannot_pass(tmp_path):
    path = tmp_path / "unknown.htm"
    path.write_text("<p>Important unclassified financial results.</p>")
    result = audit_report(path, form="10-Q", expected_section_ids=["part-i-item-1"])
    assert {"unclassified_section", "section_inventory_mismatch"} <= checks(result)


def manifest(tmp_path, original, **overrides):
    record = {"ticker": "AAPL", "form": "10-Q", "accession_number": "0000320193-26-000001",
              "html_path": original.name, "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
              "expected_section_ids": EXPECTED} | overrides
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"filings": [record]}))
    return path


def test_corpus_cannot_pass_when_requested_ticker_or_form_is_absent(tmp_path, original):
    path = manifest(tmp_path, original)
    result = audit_manifest(path, tickers=["AAPL", "MSFT"])
    assert result["status"] == "incomplete"
    assert result["filings"][0]["status"] == "passed"
    assert result["missing_ticker_forms"] == [
        {"ticker": "AAPL", "form": "10-K"}, {"ticker": "MSFT", "form": "10-K"}, {"ticker": "MSFT", "form": "10-Q"}]


def test_missing_bytes_do_not_count_as_coverage(tmp_path, original):
    path = manifest(tmp_path, original, html_path="unavailable.htm")
    result = audit_manifest(path, tickers=["AAPL"], forms=["10-Q"])
    assert result["status"] == "incomplete"
    assert result["missing_ticker_forms"] == [{"ticker": "AAPL", "form": "10-Q"}]
    assert checks(result["filings"][0]) == {"missing_source_bytes"}


def test_hash_mismatch_fails_before_parsing(tmp_path, original):
    path = manifest(tmp_path, original, sha256="0" * 64)
    result = audit_manifest(path, tickers=["AAPL"], forms=["10-Q"])
    assert result["status"] == "failed"
    assert checks(result["filings"][0]) == {"source_hash_mismatch"}


def test_cli_writes_reviewable_artifact_and_nonzero_for_incomplete_scope(tmp_path, original):
    path = manifest(tmp_path, original)
    output = tmp_path / "audit" / "report.json"
    assert main(["--manifest", str(path), "--output", str(output)]) == 2
    result = json.loads(output.read_text())
    assert result["status"] == "incomplete"
    assert len(result["missing_ticker_forms"]) == 19


def test_invalid_expectations_and_duplicate_filings_fail(tmp_path, original):
    assert "invalid_heading_expectations" in checks(audit_report(original, form="10-Q", expected_section_ids=[]))
    path = manifest(tmp_path, original)
    content = json.loads(path.read_text())
    content["filings"].append(content["filings"][0])
    path.write_text(json.dumps(content))
    result = audit_manifest(path, tickers=["AAPL"], forms=["10-Q"])
    assert result["status"] == "failed"
    assert checks(result["filings"][1]) == {"duplicate_filing"}


@pytest.mark.parametrize("layout", ["standard", "part_and_item_share_row", "number_column"])
def test_expected_inventory_reads_original_toc_independently(tmp_path, monkeypatch, layout):
    path = tmp_path / "source.htm"
    path.write_text('''<html><body><table>
      <tr><td>PART I. FINANCIAL INFORMATION</td></tr>
      <tr><td><a href="#a">Item 1. Financial Statements</a></td><td>3</td></tr>
      <tr><td><a href="#b">Item 2. Management's Discussion</a></td><td>14</td></tr>
      <tr><td>PART II. OTHER INFORMATION</td></tr>
      <tr><td><a href="#c">Item 6. Exhibits</a></td><td>23</td></tr>
      </table><h2 id="a">Item 1. Financial Statements</h2><p>Statements.</p>
      <h2 id="b">Item 2. Management's Discussion</h2><p>Discussion.</p>
      <h2 id="c">Item 6. Exhibits</h2><p>Exhibits.</p></body></html>''')
    source = path.read_text()
    if layout == "part_and_item_share_row":
        source = source.replace('<tr><td>PART I. FINANCIAL INFORMATION</td></tr>', '')
        source = source.replace('<tr><td><a href="#a">', '<tr><td>PART I</td><td><a href="#a">')
    elif layout == "number_column":
        source = source.replace('<table>', '<table><tr><td>Item</td><td>Description</td><td>Page</td></tr>')
        for item, target in [(1, "a"), (2, "b"), (6, "c")]:
            source = source.replace(f'<td><a href="#{target}">Item {item}. ', f'<td>{item}.</td><td><a href="#{target}">')
    path.write_text(source)
    def forbidden(*args, **kwargs):
        raise AssertionError("The report parser cannot supply expected headings")
    monkeypatch.setattr("sec_pipeline.report_validation.clean_report", forbidden)
    result = source_heading_expectations(path, form="10-Q")
    assert result["status"] == "derived_from_original_toc"
    assert result["expected_section_ids"] == ["part-i", "part-i-item-1", "part-i-item-2", "part-ii", "part-ii-item-6"]
    assert result["rows"][1]["source_xpath"].startswith("/html/body/table/tr[")
    assert result["rows"][1]["navigation_targets"] == ["a"]


def test_partial_or_unresolving_toc_never_establishes_full_coverage(tmp_path):
    path = tmp_path / "source.htm"
    path.write_text('''<table><tr><td>PART I. FINANCIAL INFORMATION</td></tr>
      <tr><td><a href="#a">Item 1. Financial Statements</a></td><td>3</td></tr>
      <tr><td><a href="#b">Item 2. Management's Discussion</a></td><td>14</td></tr>
      </table><p id="a">Financial statements</p><p id="b">Discussion</p>''')
    assert source_heading_expectations(path, form="10-Q")["reason"] == "original_toc_does_not_cover_core_parts"
    path.write_text(path.read_text().replace('id="b"', 'id="different"'))
    assert source_heading_expectations(path, form="10-Q")["reason"] == "no_unambiguous_linked_original_toc"


def test_original_emphasized_outline_covers_source_without_toc(tmp_path):
    path = tmp_path / "source.htm"
    path.write_text('''<div><b>PART I - FINANCIAL INFORMATION</b></div>
      <div><b>ITEM 1. FINANCIAL STATEMENTS</b></div><p>Statements.</p>
      <div><b>ITEM 2. MANAGEMENT'S DISCUSSION</b></div><p>Results.</p>
      <div><b>PART II - OTHER INFORMATION</b></div>
      <p>Item 1 of this report discusses the statements.</p>
      <div style="text-decoration:underline">ITEM 6. Exhibits.</div><p>Exhibits.</p>''')
    result = source_heading_expectations(path, form="10-Q")
    assert result["status"] == "derived_from_original_body"
    assert result["expected_section_ids"] == ["part-i", "part-i-item-1", "part-i-item-2", "part-ii", "part-ii-item-6"]
    assert result["body_headings"][-1]["source_xpath"] == "/html/body/div[5]"


def test_legitimate_body_heading_omitted_from_toc_is_retained(tmp_path):
    path = tmp_path / "source.htm"
    path.write_text('''<table><tr><td>PART I</td></tr>
      <tr><td><a href="#one">Item 1. Financial Statements</a></td><td>3</td></tr>
      <tr><td><a href="#two">Item 2. Management's Discussion</a></td><td>10</td></tr>
      <tr><td>PART II</td></tr>
      <tr><td><a href="#six">Item 6. Exhibits</a></td><td>15</td></tr></table>
      <h1>PART I</h1><h2 id="one">Item 1. Financial Statements</h2><p>Statements.</p>
      <h2 id="two">Item 2. Management's Discussion</h2><p>Results.</p>
      <h1>PART II</h1><h2>Item 5. Other Information</h2><p>New disclosure.</p>
      <h2 id="six">Item 6. Exhibits</h2><p>Exhibits.</p>''')
    result = source_heading_expectations(path, form="10-Q")
    assert result["status"] == "derived_from_original_toc"
    assert "part-ii-item-5" in result["expected_section_ids"]
    assert result["body_headings_absent_from_toc"] == [{"section_id": "part-ii-item-5", "title": "Item 5. Other Information", "source_xpath": "/html/body/h2[3]"}]


def test_historical_scope_needs_complete_discovery_and_every_accession(tmp_path, original):
    path = manifest(tmp_path, original, filing_date="2026-01-15")
    data = json.loads(path.read_text()) | {
        "filing_date_from": "2021-01-01", "filing_date_to": "2026-12-31", "forms": ["10-Q"],
        "companies": {"AAPL": {"discovery_status": "complete", "discovered_count": 23}}}
    path.write_text(json.dumps(data))
    result = audit_manifest(path, tickers=["AAPL"], forms=["10-Q"])
    assert result["status"] == "incomplete"
    assert result["discovery_issues"] == [{"ticker": "AAPL", "check": "discovery_inventory_count_mismatch", "expected": 23, "actual": 1}]
    data["companies"]["AAPL"]["discovered_count"] = 1
    path.write_text(json.dumps(data))
    assert audit_manifest(path, tickers=["AAPL"], forms=["10-Q"])["status"] == "passed"


def test_future_or_other_date_range_rows_do_not_count_as_history_verification(tmp_path, original):
    path = manifest(tmp_path, original, filing_date="2020-12-15")
    data = json.loads(path.read_text()) | {
        "filing_date_from": "2021-01-01", "filing_date_to": "2026-12-31", "forms": ["10-Q"],
        "companies": {"AAPL": {"discovery_status": "complete", "discovered_count": 1}}}
    path.write_text(json.dumps(data))
    result = audit_manifest(path, tickers=["AAPL"], forms=["10-Q"])
    assert result["status"] == "incomplete"
    assert result["discovery_issues"][0]["check"] == "filing_outside_declared_date_scope"


def test_independent_toc_xpath_rejects_page_only_navigation_link(tmp_path, monkeypatch):
    original = tmp_path / "source.htm"
    original.write_text('<table><tr><td>Item 1. Financial Statements</td>'
                        '<td><a href="#a">3</a></td></tr></table><p id="a">Body.</p>')
    text = "Item 1. Financial Statements\nBody."
    report = CleanedReport(text, [{"section_id": "part-i-item-1", "item": "1",
        "title": "Item 1. Financial Statements", "section_type": "financial_statements",
        "offset_start": 0, "offset_end": len(text),
        "source_locator": {"xpath": "/html/body/table/tr", "source_file": original.name}}])
    monkeypatch.setattr("sec_pipeline.report_validation.clean_report", lambda *args, **kwargs: report)
    path = manifest(tmp_path, original, expected_section_ids=["part-i-item-1"],
                    expectation_evidence={"tables": [{"source_xpath": "/html/body/table"}]})
    result = audit_manifest(path, tickers=["AAPL"], forms=["10-Q"])
    assert result["status"] == "failed"
    assert "section_starts_in_original_toc" in checks(result["filings"][0])
