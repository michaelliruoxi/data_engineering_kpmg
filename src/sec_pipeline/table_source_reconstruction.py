"""Independently reconstruct version-pinned cells from unchanged original bytes.

No storage writes or receiving identity assignment. This reuses the actual
V1/V2 extractors and V3/V4 plus corrected V5/V6 transforms; it never equates a section pass with a
table pass or guesses facts by their amount.
"""
from collections import defaultdict
from dataclasses import asdict
import hashlib
from pathlib import Path
import tempfile
from uuid import UUID

from .table_sections import canonical


CELL_FIELDS = ("cell_id", "row", "column", "rowspan", "colspan", "display_text", "source_xpath",
               "row_heading", "column_headings", "column_period", "classification", "fact_references",
               "source_fact_references", "quality_issue_keys", "quality_issues")


def reconstruct_original_tables(snapshot, sources):
    from .fact_extraction import extract_facts
    from .tables import extract_tables
    groups = defaultdict(list)
    for table in snapshot.get("tables", []):
        scope = table["structure"].get("fact_scope", {})
        groups[(table["source_document_id"], scope.get("xml_document_id"), table["extraction_version"],
                scope.get("facts_version"))].append(table)
    results = []
    for (html_id, xml_id, version, facts_version), stored in groups.items():
        record = {"source_document_id": html_id, "xml_document_id": xml_id, "table_version": version,
                  "facts_version": facts_version, "status": "unverified", "cells_compared": 0}
        source, instance = sources.get(html_id), sources.get(xml_id)
        structure = stored[0]["structure"]
        if source is None or instance is None:
            record["reason"] = "original_HTML_or_XML_missing"
        elif (hashlib.sha256(source).hexdigest() != structure.get("html_sha256")
              or hashlib.sha256(instance).hexdigest() != structure.get("xml_sha256")):
            record["reason"] = "original_version_hash_mismatch"
        elif version not in {"tables-v1", "tables-v2", "tables-v3", "tables-v4", "tables-v5", "tables-v6"}:
            record["reason"] = "unsupported_pinned_table_version"
        else:
            try:
                with tempfile.TemporaryDirectory(prefix="readonly-table-reconstruction-") as directory:
                    html_path, xml_path = Path(directory) / "source.htm", Path(directory) / "source.xml"
                    html_path.write_bytes(source)
                    xml_path.write_bytes(instance)
                    ids = {"filing_id": UUID(stored[0]["filing_id"]), "html_document_id": UUID(html_id), "xml_document_id": UUID(xml_id)}
                    rebuilt = extract_tables(html_path, xml_path, **ids, facts_version=facts_version,
                                             tables_version=version if version in {"tables-v1", "tables-v2"} else "tables-v2")
                    if version in {"tables-v3", "tables-v4", "tables-v5", "tables-v6"}:
                        facts = extract_facts(xml_path, filing_id=ids["filing_id"], source_document_id=ids["xml_document_id"],
                                              extraction_version=facts_version).require_complete().facts
                        from .source_cells_v3 import transform_tables as transform_v3
                        rebuilt = transform_v3(rebuilt, source, facts)
                        if version != "tables-v3":
                            from .source_cells_v4 import transform_tables as transform_v4
                            rebuilt = transform_v4(rebuilt, source, facts)
                        if version in {"tables-v5", "tables-v6"}:
                            from .extraction_profiles import CORRECTED_PROFILE, REVIEWED_PROFILE, corrected_tables
                            profile = CORRECTED_PROFILE if version == "tables-v5" else REVIEWED_PROFILE
                            rebuilt = corrected_tables(rebuilt, profile=profile)
                expected = {t.table_key: canonical(asdict(t)) for t in rebuilt}
                actual = {t["table_key"]: t for t in stored}
                differences = []
                if set(expected) != set(actual):
                    differences.append({"check": "physical_table_inventory", "stored": sorted(actual), "reconstructed": sorted(expected)})
                for key in sorted(set(expected) & set(actual)):
                    wanted, observed = expected[key]["structure"], actual[key]["structure"]
                    if wanted["source_xpath"] != observed.get("source_xpath"):
                        differences.append({"table_key": key, "check": "table_xpath"})
                    expected_cells = {c["cell_id"]: c for row in wanted["rows"] for c in row}
                    stored_cells = {c["cell_id"]: c for row in observed["rows"] for c in row}
                    if set(expected_cells) != set(stored_cells):
                        differences.append({"table_key": key, "check": "source_cell_inventory"})
                    for cell_key in sorted(set(expected_cells) & set(stored_cells)):
                        record["cells_compared"] += 1
                        fields = [f for f in CELL_FIELDS if expected_cells[cell_key].get(f) != stored_cells[cell_key].get(f)]
                        if fields:
                            differences.append({"table_key": key, "cell_key": cell_key, "check": "original_cell_evidence",
                                "fields": fields, "stored": {f: stored_cells[cell_key].get(f) for f in fields},
                                "reconstructed": {f: expected_cells[cell_key].get(f) for f in fields}})
                record.update(status="failed" if differences else "passed", differences=differences,
                              method="fresh_same-version_extractor_and_transforms; not_stored_JSON_self_comparison")
            except (ValueError, KeyError, TypeError, OSError) as error:
                record.update(reason="pinned_extractor_or_source_requires_review", error_type=type(error).__name__)
        results.append(record)
    return {"status": "failed" if any(r["status"] == "failed" for r in results) else
            "passed" if results and all(r["status"] == "passed" for r in results) else "unverified", "groups": results}
