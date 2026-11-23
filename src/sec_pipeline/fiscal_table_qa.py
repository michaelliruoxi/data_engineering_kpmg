"""Compare receiving lineage and independently exported fiscal outputs."""
from collections import Counter
from datetime import datetime


BASE_RELATIONS = {
    "tables": ("id",),
    "table_cells": ("id",),
    "cell_fact_links": ("cell_id", "fact_id"),
    "financial_table_facts": ("table_id", "fact_id"),
    "facts": ("id",),
    "sources": ("id",),
}
SOURCE_RELATIONS = {
    "cell_source_observations": ("id",),
    "cell_source_fact_bindings": ("id",),
    "fact_source_occurrences": ("id",),
    "source_document_sets": ("id",),
    "source_document_set_members": ("id",),
}


def identity(snapshot):
    filing, selection, processing = (snapshot[key] for key in ("filing", "selection", "selected_processing_set"))
    if selection["id"] != processing["id"] or filing["id"] != processing["filing_id"]:
        raise ValueError("Selection must identify the receiving filing and processing set.")
    if selection["contract"] not in {"legacy", "source-v4"}:
        raise ValueError("Unknown processing family.")
    return snapshot["accession"], filing["id"], selection["contract"], selection["id"]


def lineage_rows(snapshot):
    _, filing_id, family, _ = identity(snapshot)
    processing = snapshot["selected_processing_set"]
    document_id = processing.get("document_set_id")
    if family == "source-v4":
        document = next(row for row in snapshot["source_document_sets"] if row["id"] == document_id)
        fact_source, facts_version = document["xml_document_id"], processing["versions"]["facts"]
    else:
        fact_source, facts_version = processing["fact_source_document_id"], processing["fact_extraction_version"]
    if not fact_source or not facts_version or not snapshot["tables"]:
        raise ValueError("The selected set must have a fact scope and receiving tables.")
    linked_ids = {row["fact_id"] for name in ("cell_fact_links", "financial_table_facts", "cell_source_fact_bindings")
                  for row in snapshot.get(name, [])}
    facts = [row for row in snapshot["facts"] if row["id"] in linked_ids or
             (row["filing_id"], row["source_document_id"], row["extraction_version"])
             == (filing_id, fact_source, facts_version)]
    source_ids = {row["source_document_id"] for row in snapshot["tables"] + facts} | {fact_source}
    relations = dict(BASE_RELATIONS)
    # V3 source observations may also exist under the legacy processing contract.
    if "cell_source_observations" in snapshot:
        relations["cell_source_observations"] = ("id",)
    if family == "source-v4":
        relations.update(SOURCE_RELATIONS)
    result = {}
    for name, keys in relations.items():
        rows = snapshot[name]
        if name == "facts":
            rows = facts
        elif name == "sources":
            rows = [row for row in rows if row["id"] in source_ids]
        elif name == "source_document_sets":
            rows = [row for row in rows if row["id"] == document_id]
        elif name in {"fact_source_occurrences", "source_document_set_members"}:
            rows = [row for row in rows if row["document_set_id"] == document_id]
        indexed = {}
        for row in rows:
            key = tuple(row[field] for field in keys)
            if not all(key) or key in indexed:
                raise ValueError(f"Missing or duplicate receiving identity in {name}.")
            metadata_fields = {"created_at"}
            if name in {"tables", "table_cells"}:
                metadata_fields |= {"fiscal_year", "fiscal_period"}
            indexed[key] = {field: value for field, value in row.items() if field not in metadata_fields}
        result[name] = indexed
    if (not result["table_cells"] or not result["cell_fact_links"]
            or not linked_ids <= {row["id"] for row in facts}
            or not source_ids <= {key[0] for key in result["sources"]}):
        raise ValueError("The snapshot lacks the table, cell, linked fact or source evidence required for lineage.")
    return result


def compare_lineage(before, after, before_capture, after_capture):
    if before is None or before_capture is None:
        return {"status": "baseline_missing"}
    if after is None or after_capture is None:
        return {"status": "after_capture_missing"}
    try:
        old_identity, new_identity = identity(before), identity(after)
        if old_identity[:2] != new_identity[:2]:
            return {"status": "filing_identity_mismatch"}
        if old_identity[2:] != new_identity[2:]:
            return {"status": "processing_set_changed", "before": old_identity, "after": new_identity}
        scope_fields = ("document_set_id", "versions", "fact_source_document_id", "table_source_document_id",
                        "fact_extraction_version", "table_extraction_version")
        old_scope = {key: before["selected_processing_set"].get(key) for key in scope_fields}
        new_scope = {key: after["selected_processing_set"].get(key) for key in scope_fields}
        if old_scope != new_scope:
            return {"status": "processing_version_or_document_scope_changed", "before": old_scope, "after": new_scope}
        if not before_capture.get("database_id") or before_capture["database_id"] != after_capture.get("database_id"):
            return {"status": "database_identity_unverified_or_changed"}
        if (before_capture.get("phase"), after_capture.get("phase")) != ("before", "after") or (
                not before_capture.get("deployment_ref")
                or before_capture["deployment_ref"] != after_capture.get("deployment_ref")):
            return {"status": "deployment_evidence_missing_or_mismatched"}
        old_time = datetime.fromisoformat(before_capture["captured_at_utc"])
        new_time = datetime.fromisoformat(after_capture["captured_at_utc"])
        deployed = datetime.fromisoformat(after_capture["deployed_at_utc"])
        if not all(t.utcoffset() is not None for t in (old_time, new_time, deployed)) or not old_time < deployed <= new_time:
            return {"status": "capture_timing_does_not_bracket_deployment"}
        old, new = lineage_rows(before), lineage_rows(after)
    except (KeyError, TypeError, ValueError, StopIteration) as error:
        return {"status": "invalid_or_incomplete_evidence", "reason": str(error)}
    if old.keys() != new.keys():
        return {"status": "capture_schema_changed"}
    changes = {}
    for relation in old:
        left, right = old[relation], new[relation]
        updated = [{"key": key, "fields": sorted(field for field in left[key].keys() | right[key].keys()
                    if field not in left[key] or field not in right[key] or left[key][field] != right[key][field])}
                   for key in sorted(left.keys() & right.keys()) if left[key] != right[key]]
        changes[relation] = {"before_count": len(left), "after_count": len(right),
                             "removed": sorted(left.keys() - right.keys()),
                             "added": sorted(right.keys() - left.keys()), "updated": updated}
    changed = any(group["removed"] or group["added"] or group["updated"] for group in changes.values())
    return {"status": "lineage_changed" if changed else "lineage_unchanged",
            "scope": old_identity, "relations": changes}


def verify_fiscal_outputs(snapshot, exports, expected=None):
    """Audit one pinned set's exports made independently by the downstream owner.

    Export rows use canonical field names after renaming from the owner contract.
    The independent expected label is a verified filing-level DEI assertion.
    """
    _, filing_id, family, set_id = identity(snapshot)
    if exports is None or any(name not in exports for name in ("filings", "tables", "cells")):
        return {"consistency": "downstream_output_missing", "correctness": "unverified"}
    filings = [row for row in exports["filings"] if row.get("filing_id") == filing_id]
    issues = []
    if len(filings) != 1:
        issues.append({"check": "filing_identity_not_unique", "count": len(filings)})
    def labels(row):
        return row.get("fiscal_year"), row.get("fiscal_period")

    filing_labels = labels(filings[0]) if len(filings) == 1 else (None, None)
    if not all(filing_labels):
        issues.append({"check": "filing_fiscal_labels_missing"})
    if not snapshot["tables"] or not snapshot["table_cells"]:
        return {"consistency": "no_receiving_tables_or_cells", "correctness": "unverified", "issues": issues}
    for layer, snapshot_key in (("tables", "tables"), ("cells", "table_cells")):
        parents = {row["id"]: row.get("table_id") for row in snapshot[snapshot_key]}
        rows = exports[layer]
        counts = Counter(row.get("id") for row in rows)
        for row_id in sorted(parents.keys() - counts.keys()):
            issues.append({"check": "missing_output_row", "layer": layer, "id": row_id})
        for row in rows:
            row_id = row.get("id")
            if counts[row_id] > 1:
                issues.append({"check": "duplicate_output_row", "layer": layer, "id": row_id})
            if (row_id not in parents or row.get("filing_id") != filing_id
                    or row.get("processing_set_id") != set_id or row.get("processing_family") != family
                    or (layer == "cells" and row.get("table_id") != parents.get(row_id))):
                issues.append({"check": "output_identity_or_parent_mismatch", "layer": layer, "id": row_id})
            if not all(labels(row)) or labels(row) != filing_labels:
                issues.append({"check": "output_labels_absent_or_inconsistent", "layer": layer, "id": row_id})
    verified = expected and expected.get("verified_dei") is True and all(labels(expected)) and (
        expected.get("filing_id"), expected.get("processing_family"), expected.get("processing_set_id")
    ) == (filing_id, family, set_id)
    correctness = (("matched_verified_dei" if labels(expected) == filing_labels else "disagrees_with_verified_dei")
                   if verified and all(filing_labels) else "unverified")
    return {"consistency": "failed" if issues else "passed", "correctness": correctness, "issues": issues}
