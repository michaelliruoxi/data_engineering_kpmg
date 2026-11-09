"""Read receiving-database outputs and verify them against unchanged originals."""

from dataclasses import asdict
import hashlib
from pathlib import Path
from types import SimpleNamespace

from .extraction_profiles import CORRECTED_PROFILE, versions_for
from .report_cleaning import clean_report
from .report_validation import audit_report, source_heading_expectations
from .sections import normalize_sections
from .table_relations import audit_snapshot, read_snapshot


def audit_corrected_set(conn, accession, processing_set_id, data_root):
    """Require a caller-owned READ ONLY transaction; never change selection."""
    snapshot = read_snapshot(conn, accession, selection={"contract": "source-v4", "id": str(processing_set_id)})
    root = Path(data_root).resolve()
    errors, sources, paths, reports = [], {}, {}, []
    selected = snapshot["selected_processing_set"]
    if selected["versions"] != versions_for(CORRECTED_PROFILE):
        errors.append({"check": "wrong_processing_versions"})
    if selected["validation_status"] != "passed" or selected["source_validation_status"] != "passed":
        errors.append({"check": "processing_set_not_passed"})
    if selected["semantic_checks"].get("primary_section_boundaries") is not True:
        errors.append({"check": "missing_original_heading_validation"})
    # Check receipt paths rather than probing or rewriting source registrations.
    for record in snapshot["sources"]:
        path = (root / record["local_path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            errors.append({"check": "original_source_unavailable", "source_document_id": record["id"]})
            continue
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != record["sha256"] or len(content) != record["size_bytes"]:
            errors.append({"check": "original_source_receipt_mismatch", "source_document_id": record["id"]})
            continue
        sources[record["id"]], paths[record["id"]] = content, path
    members = [m for m in snapshot["source_document_set_members"] if m["document_set_id"] == selected["document_set_id"]]
    primary = {m["source_document_id"] for m in members if m["is_primary"]}
    if len(primary) != 1 or {r["source_document_id"] for r in snapshot["reports"]} != {m["source_document_id"] for m in members}:
        errors.append({"check": "report_membership_mismatch"})
    for report in snapshot["reports"]:
        path = paths.get(report["source_document_id"])
        if path is None:
            continue
        original = clean_report(path, form=snapshot["filing"]["form"])
        if original.full_text != report["full_text"] or original.sections != report["sections"]:
            errors.append({"check": "stored_report_differs_from_original_reconstruction", "report_id": report["id"]})
        descriptor = SimpleNamespace(full_text=report["full_text"], sections=report["sections"])
        normalized = [asdict(value) for value in normalize_sections(descriptor)]
        actual = [s for s in snapshot["sections"] if s["report_id"] == report["id"]]
        actual_descriptors = [{key: s[key] for key in normalized[0]} for s in actual] if normalized else actual
        if actual_descriptors != normalized:
            errors.append({"check": "stored_relational_sections_differ", "report_id": report["id"]})
        by_id = {s["id"]: s for s in actual}
        chunks = [c for c in snapshot["chunks"] if c["report_id"] == report["id"]]
        if any(c["section_id"] not in by_id or by_id[c["section_id"]]["section_key"] != c["section"] for c in chunks):
            errors.append({"check": "stored_chunk_section_link_mismatch", "report_id": report["id"]})
        expected = source_heading_expectations(path, form=snapshot["filing"]["form"]) if report["source_document_id"] in primary else None
        audit = audit_report(path, form=snapshot["filing"]["form"], report=descriptor,
                             chunks=[SimpleNamespace(**c) for c in chunks],
                             expected_section_ids=expected["expected_section_ids"] if expected else None)
        if audit["issues"] or (expected is not None and audit["status"] != "passed"):
            errors.append({"check": "stored_report_audit_failed", "report_id": report["id"], "issues": audit["issues"]})
        reports.append({"report_id": report["id"], "source_document_id": report["source_document_id"],
            "original_heading_expectations": expected, **{key: value for key, value in audit.items() if key != "sections"},
            "actual_section_keys": [s["section_id"] for s in report["sections"]]})
    relations = audit_snapshot(snapshot, sources)
    if relations["status"] != "passed":
        errors.append({"check": "stored_table_relationships_not_passed"})
    return {"scope": "receiving_database_reports_sections_chunks_and_table_links", "accession_number": accession,
        "filing_id": snapshot["filing"]["id"], "processing_set_id": str(processing_set_id),
        "status": "failed" if errors else "passed", "errors": errors, "reports": reports,
        "table_relationships": relations,
        "retrieval_index_status": "not_verified", "actual_table_chunk_producer_status": "not_verified"}
