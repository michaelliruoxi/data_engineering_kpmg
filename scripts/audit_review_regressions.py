"""Replay archived report, statement-section and numeric-boundary evidence offline.

Run with PYTHONPATH pointing at the implementation to measure. Source files are
resolved against --source-root; the input inventory supplies hashes and scope.
No source bytes, database rows, queue entries or external indexes are modified.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import inspect
import json
from pathlib import Path
from uuid import UUID

from lxml import etree, html

from sec_pipeline.chunking import chunk_report
from sec_pipeline.fact_extraction import IX, _numeric_tag_boundary
from sec_pipeline.report_cleaning import clean_report
from sec_pipeline.report_validation import audit_report, source_heading_expectations
from sec_pipeline.source_locators import source_xpath
from sec_pipeline.table_sections import map_table_section
from sec_pipeline.tables import TableExtractionError, select_statements


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def check_filing(task):
    row, source_root = task
    path = Path(source_root) / row["path"]
    source = path.read_bytes()
    sha256 = hashlib.sha256(source).hexdigest()
    if sha256 != row["sha256"]:
        raise ValueError(f"Source hash mismatch: {row['acc']}")
    report = clean_report(path, form=row["form"])
    expected = source_heading_expectations(path, form=row["form"])
    chunks = chunk_report(filing_id=UUID(int=1), report_id=UUID(int=2),
                          full_text=report.full_text, sections=report.sections,
                          chunking_version="paragraph-v2")
    result = {key: row[key] for key in ("acc", "ticker", "form", "path")}
    result.update(sha256=sha256, section_output_sha256=_digest(report.sections),
                  cleaned_text_sha256=hashlib.sha256(report.full_text.encode()).hexdigest(),
                  section_ids=[section["section_id"] for section in report.sections])

    tree = html.document_fromstring(source, parser=html.HTMLParser(encoding="utf-8", no_network=True)).getroottree()
    selected_tables = None
    try:
        selected = select_statements(tree)
    except TableExtractionError as exc:
        result["primary_statement_sections"] = {"status": "selection_unavailable", "reason": str(exc)}
    else:
        selected_tables = [
            {"table_key": kind, "statement_type": kind,
             "structure": {"source_xpath": source_xpath(tree, table), "html_sha256": sha256}}
            for kind, table in selected.items()
        ]
        section_types = {section["section_id"]: section["section_type"] for section in report.sections}
        mappings = []
        for kind, table in selected.items():
            mapping = map_table_section(
                {"table_key": kind, "structure": {"source_xpath": source_xpath(tree, table), "html_sha256": sha256}},
                {}, report.sections, source, accession=row["acc"])
            mappings.append({"statement_type": kind, "table_xpath": source_xpath(tree, table),
                             "mapping_status": mapping["mapping_status"], "section_key": mapping["section_key"],
                             "section_type": section_types.get(mapping["section_key"])})
        result["primary_statement_sections"] = {
            "status": "passed" if all(m["mapping_status"] == "mapped" and m["section_type"] == "financial_statements"
                                      for m in mappings) else "failed",
            "mappings": mappings,
        }

    # Baseline implementations predate the gate argument. Current runs feed the
    # actual selected table descriptors to the same invariant as the parser.
    gate_supported = "selected_tables" in inspect.signature(audit_report).parameters
    audit_options = {"selected_tables": selected_tables} if gate_supported else {}
    audited = audit_report(path, form=row["form"], report=report, chunks=chunks,
                           expected_section_ids=expected["expected_section_ids"], **audit_options)
    result.update({key: audited[key] for key in ("status", "issues", "section_count", "chunk_count")})
    result["selected_primary_gate_exercised"] = gate_supported and selected_tables is not None

    # Inspect every numeric tag, independently of whether an XML instance exists
    # or the current table selector supports this source's statement layout.
    xml_tree = etree.fromstring(source, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    numeric_tags = list(xml_tree.iter(f"{{{IX}}}nonFraction"))
    numeric_issues = []
    for element in numeric_tags:
        boundary = _numeric_tag_boundary(element, "".join(element.itertext()).strip(" \t\r\n\xa0"))
        if boundary is not None:
            numeric_issues.append({"inline_id": element.get("id"), "concept": element.get("name"), **boundary})
    result["numeric_tag_boundary_scan"] = {"tag_count": len(numeric_tags), "issues": numeric_issues}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    rows = json.loads(args.inventory.read_text())["filings"]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(check_filing, [(row, str(args.source_root)) for row in rows]))
    summary = {"filing_count": len(results),
               "section_audit_status_counts": {status: sum(row["status"] == status for row in results)
                                               for status in ("passed", "failed", "unverified")},
               "section_count": sum(row["section_count"] for row in results),
               "chunk_count": sum(row["chunk_count"] for row in results),
               "primary_statement_status_counts": {status: sum(row["primary_statement_sections"]["status"] == status
                                                                 for row in results)
                                                   for status in ("passed", "failed", "selection_unavailable")},
               "numeric_tag_count": sum(row["numeric_tag_boundary_scan"]["tag_count"] for row in results),
               "numeric_boundary_hit_count": sum(len(row["numeric_tag_boundary_scan"]["issues"]) for row in results),
               "numeric_boundary_accessions": [row["acc"] for row in results if row["numeric_tag_boundary_scan"]["issues"]],
               "primary_statement_failure_accessions": [row["acc"] for row in results
                                                        if row["primary_statement_sections"]["status"] == "failed"]}
    summary["selected_primary_gate_filings"] = sum(row["selected_primary_gate_exercised"] for row in results)
    summary["statement_selection_unavailable_accessions"] = [row["acc"] for row in results
                                                             if row["primary_statement_sections"]["status"] == "selection_unavailable"]
    summary["take_two_annuals"] = [{"accession": row["acc"], "section_audit": row["status"],
                                   "selected_primary_gate": row["primary_statement_sections"]["status"]}
                                  for row in results if row["ticker"] == "TTWO" and row["form"] == "10-K"]
    if args.baseline:
        baseline = {row["acc"]: row for row in json.loads(args.baseline.read_text())["filings"]}
        summary["changed_section_accessions"] = [row["acc"] for row in results
                                                if row["section_output_sha256"] != baseline[row["acc"]]["section_output_sha256"]]
        summary["changed_cleaned_text_accessions"] = [row["acc"] for row in results
                                                     if row["cleaned_text_sha256"] != baseline[row["acc"]]["cleaned_text_sha256"]]
        summary["new_section_audit_failure_accessions"] = [row["acc"] for row in results
                                                          if row["status"] == "failed" and baseline[row["acc"]]["status"] != "failed"]
        summary["new_failure_from_previously_passed_accessions"] = [row["acc"] for row in results
                                                                  if row["status"] == "failed" and baseline[row["acc"]]["status"] == "passed"]
        summary["changed_section_count"] = len(summary["changed_section_accessions"])
        for row in results:
            before = baseline[row["acc"]]
            row["baseline"] = {key: before[key] for key in ("status", "section_count", "chunk_count", "section_output_sha256")}
    evidence = {"scope": "Offline archived-source report/chunk audit, selected-primary-table mapping and all numeric tags; "
                         "not full financial-value or database acceptance",
                "chunking_version": "paragraph-v2", "summary": summary, "filings": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
