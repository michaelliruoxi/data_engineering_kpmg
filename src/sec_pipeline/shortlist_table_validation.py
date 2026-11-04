"""Reconcile a confirmed discovery snapshot with table evidence, without writes.

The section audit supplies an accession denominator and HTML receipts only. It
never supplies table passes, receiving UUIDs, XML receipts, or future filings.
Existing table validators remain the sole relationship-check implementation.
"""
from collections import Counter, defaultdict
from contextlib import redirect_stdout
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import subprocess

from lxml import etree

from .database import DatabaseError
from . import historical_table_validation as batch
from .table_chunk_links import resolve_table_chunks
from .table_relations_cli import save_json
from .table_sections import canonical


def load_scope(config_path, section_audit_path):
    config = json.loads(config_path.read_text())
    audit = json.loads(section_audit_path.read_text())
    companies = {row["ticker"]: row["cik"] for row in config["companies"]}
    if len(companies) != len(config["companies"]) or config.get("scope_status") != "user_confirmed":
        raise DatabaseError("This workflow requires an explicit, distinct, user-confirmed shortlist.")
    start, end = config["since"], config["until"]
    if date.fromisoformat(start) > date.fromisoformat(end) or set(config["forms"]) != {"10-K", "10-Q"} or config["amendments_included"] is not False:
        raise DatabaseError("Invalid original-filing date/form scope.")
    declared = audit["scope"]
    if (set(declared["tickers"]) != set(companies) or set(declared["forms"]) != set(config["forms"])
            or (declared["filing_date_from"], declared["filing_date_to"]) != (start, end)
            or declared["amendments_included"] is not False):
        raise DatabaseError("Section discovery snapshot and shortlist scopes disagree.")
    records, accessions = [], set()
    for row in audit["filings"]:
        accession = row["accession_number"]
        if (not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or
                accession in accessions or row["ticker"] not in companies or row["form"] not in config["forms"] or not start <= row["filing_date"] <= end):
            raise DatabaseError("Duplicate or out-of-scope accession in the discovery snapshot.")
        accessions.add(accession)
        records.append(dict(row, cik=companies[row["ticker"]]))
    if len(records) != audit["filings_checked"]:
        raise DatabaseError("Discovery snapshot accession count does not reconcile.")
    return {"config": config, "companies": companies, "filings": records,
        "snapshot_latest_filing_date": declared["snapshot_latest_filing_date"],
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "section_audit_sha256": hashlib.sha256(section_audit_path.read_bytes()).hexdigest(),
        "scope_status": "user_confirmed_window_discovery_snapshot_only"}


def restrict_inventory(inventory, scope, tickers):
    inventory = canonical(inventory)
    config = scope["config"]
    rows = [f for f in inventory.get("filings", []) if f.get("ticker") in tickers
            and config["since"] <= f["filing_date"] <= config["until"] and f["form"] in config["forms"]]
    ids = {f["id"] for f in rows}
    excluded = [f["accession_number"] for f in inventory.get("filings", []) if f["id"] not in ids]
    inventory["filings"] = rows
    for key in ("source_documents", "filing_processing_sets", "preferred_processing_sets", "source_processing_sets", "source_document_sets", "finance_filing"):
        inventory[key] = [r for r in inventory.get(key, []) if str(r["filing_id"]) in ids]
    inventory["excluded_outside_requested_scope"] = excluded
    return inventory


def local_manifests(roots):
    """Read only known archive layouts; do not scan private configuration."""
    found = {}
    for root in roots:
        paths = [root / "source_manifest.json", *root.glob("data/raw/sec/*/source_manifest.json")]
        for path in paths:
            if path.is_file():
                value = json.loads(path.read_text())
                if "accession_number" in value:
                    found.setdefault(value["accession_number"], []).append((root.resolve(), value))
    return found


def source_reconciliation(scope, inventory, roots, *, tickers):
    manifests = local_manifests(roots)
    filings = {f["accession_number"]: f for f in inventory.get("filings", [])}
    receipts = defaultdict(list)
    for row in inventory.get("source_documents", []):
        receipts[str(row["filing_id"])].append(row)
    result = []
    for filing in scope["filings"]:
        if filing["ticker"] not in tickers:
            continue
        accession = filing["accession_number"]
        db = filings.get(accession, {})
        stored = receipts.get(str(db.get("id")), [])
        for role in ("main_inline_xbrl_filing", "extracted_xbrl_instance"):
            selected = [r for r in stored if r["role"] == role]
            if not selected:
                archived = [r for _, m in manifests.get(accession, []) for r in m.get("files", []) if r["role"] == role]
                selected = archived or [{"role": role, "source_url": filing.get("source_url") if role == "main_inline_xbrl_filing" else None,
                    "sha256": filing.get("source_sha256") if role == "main_inline_xbrl_filing" else None}]
            for receipt in selected:
                paths = []
                for root in roots:
                    if receipt.get("local_path"):
                        path = (root / receipt["local_path"]).resolve()
                        if path.is_relative_to(root.resolve()):
                            paths.append(path)
                    if receipt.get("source_url"):
                        basename = receipt["source_url"].rsplit("/", 1)[-1]
                        paths.extend([root / "data/raw/sec" / accession / basename,
                                      root / "data/section-validation/sources" / accession / basename])
                existing = sorted({p.resolve() for p in paths if p.is_file() and any(p.resolve().is_relative_to(r.resolve()) for r in roots)}, key=str)
                hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in existing}
                expected = receipt.get("sha256")
                snapshot_hash = filing.get("source_sha256") if role == "main_inline_xbrl_filing" and receipt.get("source_url") == filing.get("source_url") else None
                status = ("receipt_unknown" if not expected else "missing_original" if not hashes else
                          "hash_verified" if expected in hashes.values() else "hash_mismatch")
                conflict = bool(expected and snapshot_hash and expected != snapshot_hash)
                result.append({"ticker": filing["ticker"], "accession": accession, "file_type": role,
                    "source_document_id": receipt.get("id"), "source_url": receipt.get("source_url"),
                    "recorded_local_path": receipt.get("local_path"), "expected_sha256": expected,
                    "section_snapshot_html_sha256": snapshot_hash, "receipt_conflict": conflict,
                    "local_exists": bool(existing), "local_paths": [str(p) for p in existing], "observed_sha256": hashes,
                    "source_status": status, "shared_filing_present": bool(db),
                    "receipt_origin": "shared_database" if receipt.get("id") else "local_manifest" if receipt.get("local_path") else "section_discovery_snapshot_HTML_only"})
    return result


def review_contexts(snapshot, sources):
    """Re-read original XML contexts if present; never correct their dates."""
    facts = {f["id"]: f for f in snapshot.get("facts", [])}
    cells = {c["id"]: c for c in snapshot.get("table_cells", [])}
    receipts = {r["id"]: r for r in snapshot.get("sources", [])}
    results = []
    for binding in snapshot.get("cell_source_fact_bindings", []):
        fact = facts.get(binding["fact_id"], {})
        cell = cells.get(binding["cell_id"], {})
        source_id = fact.get("source_document_id")
        source = sources.get(source_id)
        issues = [q for q in snapshot.get("source_quality_issues", []) if q["binding_id"] == binding["id"]]
        record = {"accession": snapshot["accession"], "selection": snapshot.get("selection"), "table_id": binding["table_id"],
            "cell_id": binding["cell_id"], "cell_key": cell.get("cell_key"), "fact_id": fact.get("id"),
            "recorded_context_ref": fact.get("source_reference", {}).get("context_ref"),
            "recorded_period": {k: fact.get(k) for k in ("period_kind", "instant_date", "period_start", "period_end")},
            "quality_issues": issues, "status": "original_XML_context_unavailable", "owner": "Bryce/Financial Data", "dates_modified": False}
        if source is not None:
            digest = hashlib.sha256(source).hexdigest()
            record["observed_xml_sha256"] = digest
            if digest != receipts.get(source_id, {}).get("sha256"):
                record["status"] = "original_XML_hash_mismatch"
            else:
                tree = etree.fromstring(source, etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))
                element_id = fact.get("source_reference", {}).get("element_id") or fact.get("occurrence_key")
                nodes = tree.xpath("//*[@id=$identifier]", identifier=element_id)
                contexts = tree.xpath("/x:xbrl/x:context[@id=$identifier]", namespaces={"x": "http://www.xbrl.org/2003/instance"},
                                      identifier=nodes[0].get("contextRef") if len(nodes) == 1 else None)
                if len(nodes) == len(contexts) == 1:
                    record.update(status="original_XML_context_rechecked_not_adjudicated",
                        fact_xpath=tree.getroottree().getpath(nodes[0]), context_xpath=tree.getroottree().getpath(contexts[0]),
                        original_context_xml=etree.tostring(contexts[0], encoding="unicode"))
                else:
                    record["status"] = "original_fact_or_context_not_unique"
        results.append(record)
    return results


def explicit_table_chunks(snapshot, producer_records=()):
    records = [r for r in producer_records if r.get("accession") == snapshot["accession"]]
    for row in snapshot.get("chunks", []):
        if (row.get("metadata") or {}).get("chunk_type") == "table":
            # Do not fill missing ID/scope/synthetic fields from paragraph IDs.
            records.append(dict(row["metadata"]))
    return records


def run_scoped_validation(config_path, section_audit_path, snapshot_root, roots, output, *, tickers=None,
                          additional_inputs=(), selections=None, producer_records=()):
    scope = load_scope(config_path, section_audit_path)
    tickers = set(tickers or scope["companies"])
    if tickers - set(scope["companies"]):
        raise DatabaseError("Requested ticker is outside the confirmed shortlist.")
    companies = {t: c for t, c in scope["companies"].items() if t in tickers}
    inventory = restrict_inventory(json.loads((snapshot_root / "shared-inventory.json").read_text()), scope, tickers)
    output.mkdir(parents=True, exist_ok=True)
    save_json(output / "shared-inventory.json", inventory)
    def reader(accession, selection):
        return json.loads((snapshot_root / accession / f"{selection['contract']}--{selection['id']}" / "snapshot.json").read_text())
    inputs = [(str(section_audit_path), {"filings": scope["filings"]}), *additional_inputs]
    with (output / "validation_progress.jsonl").open("w", encoding="utf-8") as progress, redirect_stdout(progress):
        batch.run_validation(inventory, inputs, companies, roots, output, reader, selections,
                             include_prior_sample=True)
    import csv
    with (output / "validation_summary.csv").open() as stream:
        summaries = list(csv.DictReader(stream))
    with (output / "validation_issues.csv").open() as stream:
        issues = list(csv.DictReader(stream))
    expected = {r["accession_number"]: r for r in scope["filings"] if r["ticker"] in tickers}
    source_rows = source_reconciliation(scope, inventory, roots, tickers=tickers)
    grouped = defaultdict(list)
    for record in source_rows:
        grouped[record["accession"]].append(record)
    for row in summaries:
        row.update(scope_status=scope["scope_status"], in_section_discovery_snapshot=row["accession"] in expected,
            section_audit_reported_status=expected.get(row["accession"], {}).get("status"),
            section_audit_is_table_acceptance=False, prior_sample_included=row["accession"] == batch.PRIOR_SAMPLE,
            database_inventory_as_of=(inventory.get("identity") or {}).get("queried_at"),
            local_originals_status="hash_verified" if grouped[row["accession"]] and all(r["source_status"] == "hash_verified" and not r["receipt_conflict"] for r in grouped[row["accession"]]) else "incomplete_or_receipt_conflict")
        # filing_id is not exported by the original runner's summary schema.
        row["current_database_presence"] = "present" if row["accession"] in {f["accession_number"] for f in inventory["filings"]} else "absent_from_readonly_inventory"
    def gap(accession, check, owner, evidence):
        return {"accession": accession, "check": check, "owner": owner, "actual_value": evidence,
            "issue_type": "missing_evidence", "fix_status": "not_written_to_shared_database", "retest_result": "unverified"}
    for row in summaries:
        if row["current_database_presence"].startswith("absent"):
            issues.append(gap(row["accession"], "discovered_filing_not_in_current_shared_database", "Michael + Acquisition", "Discovery is not ingestion; no processing IDs or counts invented"))
    for row in source_rows:
        if row["source_status"] != "hash_verified":
            issue = gap(row["accession"], "original_" + row["file_type"] + "_" + row["source_status"], "Acquisition", row)
            if row["source_status"] == "hash_mismatch":
                issue.update(issue_type="source_version_mismatch", owner="Acquisition + Michael", retest_result="failed")
            issues.append(issue)
        if row["receipt_conflict"]:
            issues.append({**gap(row["accession"], "database_vs_section_snapshot_source_receipt_disagreement", "Acquisition + Michael", row),
                "issue_type": "source_receipt_conflict", "retest_result": "different_recorded_hashes_confirmed; original_bytes_unavailable_for_adjudication"})
    contexts, chunk_mappings, traces = [], [], []
    chunk_records_seen, paragraph_ids = set(), set()
    for path in sorted(output.glob("*/*/snapshot.json")):
        snapshot = json.loads(path.read_text())
        sources, _ = batch.available_sources(snapshot, roots)
        contexts.extend(review_contexts(snapshot, sources))
        records = explicit_table_chunks(snapshot, producer_records)
        chunk_records_seen.update(json.dumps(r, sort_keys=True) for r in records)
        paragraph_ids.update(c["id"] for c in snapshot.get("chunks", []) if (c.get("metadata") or {}).get("chunk_type") != "table")
        choice = snapshot.get("selected_processing_set") or {}
        tables = [t | {"accession": snapshot["accession"], "processing_set": snapshot.get("selection"),
                      "source/document_set": choice.get("document_set_id")} for t in snapshot["tables"]]
        resolved = resolve_table_chunks(records, tables, sources) if records else []
        chunk_mappings.extend([r | {"accession": snapshot["accession"], "selection": snapshot.get("selection")} for r in resolved])
        check = json.loads((path.parent / "checks.json").read_text()) if (path.parent / "checks.json").is_file() else {}
        for mapping in resolved:
            if mapping["mapping_status"] == "rejected":
                issues.append({"accession": snapshot["accession"], "processing_family": snapshot["selection"]["contract"],
                    "processing_set_id": snapshot["selection"]["id"], "table_id": mapping.get("table_id"),
                    "check": "table_chunk_mapping_rejected", "actual_value": mapping,
                    "expected_evidence": "Receiving financial_tables.id with exact filing/source/version/processing scope",
                    "issue_type": "validation_failure", "owner": "Ruby + Michael", "fix_status": "not_written_to_shared_database", "retest_result": "failed"})
        for trace in check.get("trace_examples", []):
            mappings = [m for m in check["section_mappings"] if m["table_id"] == trace["table_id"] and m["mapping_status"] == "mapped"]
            traces.append(trace | {"section": mappings[0] if len(mappings) == 1 else None,
                "section_associations": mappings, "section_association_status": "unique_mapped" if len(mappings) == 1 else "unverified_or_ambiguous",
                "table_chunks": [r for r in resolved if r.get("table_id") == trace["table_id"] and check.get("status") == "passed" and r["mapping_status"] != "rejected"],
                "table_chunk_acceptance": "draft_contract_unconfirmed" if records else "no_actual_table_chunk_supplied"})
    errors, gaps, provenance = [], [], []
    for issue in issues:
        issue["issue_group"] = ("provenance_conflict" if issue.get("issue_type") == "source_receipt_conflict" else
            "error" if issue.get("issue_type") in {"validation_failure", "scope_or_identity_conflict", "source_version_mismatch"} else "missing_evidence_or_review")
        (errors if issue["issue_group"] == "error" else provenance if issue["issue_group"] == "provenance_conflict" else gaps).append(issue)
    issue_columns = (*batch.ISSUE_COLUMNS, "issue_group")
    batch.write_csv(output / "validation_issues.csv", issues, issue_columns)
    batch.write_csv(output / "validation_errors.csv", errors, issue_columns)
    batch.write_csv(output / "validation_evidence_gaps.csv", gaps, issue_columns)
    batch.write_csv(output / "validation_provenance_conflicts.csv", provenance, issue_columns)
    batch.write_csv(output / "source_reconciliation.csv", source_rows, tuple(source_rows[0]) if source_rows else ("accession",))
    batch.write_csv(output / "missing_sources_minimal.csv", [r for r in source_rows if r["source_status"] != "hash_verified" or r["receipt_conflict"]], tuple(source_rows[0]) if source_rows else ("accession",))
    for row in summaries:
        row["issue_count"] = sum(i["accession"] == row["accession"] for i in issues)
    summary_columns = (*batch.SUMMARY_COLUMNS, "in_section_discovery_snapshot", "section_audit_reported_status", "section_audit_is_table_acceptance",
        "prior_sample_included", "current_database_presence", "database_inventory_as_of", "local_originals_status")
    batch.write_csv(output / "validation_summary.csv", summaries, summary_columns)
    save_json(output / "review_context_evidence.json", contexts)
    save_json(output / "table_chunk_mappings.json", {"actual_records_checked": len(chunk_records_seen), "mapping_rows": len(chunk_mappings),
        "ordinary_paragraph_chunks_observed_unique": len(paragraph_ids), "mappings": chunk_mappings,
        "contract_status": "pending_Michael_Ruby_confirmation", "paragraph_chunks_not_promoted_to_table_chunks": True})
    save_json(output / "actual_trace_examples.json", traces)
    candidate_manifest = json.loads((output / "candidate_manifest.json").read_text())
    candidate_manifest.update(scope_status=scope["scope_status"], year_filter={"since": scope["config"]["since"], "until": scope["config"]["until"]},
        discovery_snapshot_count=len(expected), snapshot_latest_filing_date=scope["snapshot_latest_filing_date"], section_pass_is_table_pass=False)
    save_json(output / "candidate_manifest.json", candidate_manifest)
    snapshot_rows = {r["accession"] for r in summaries if r["accession"] in expected}
    if snapshot_rows != set(expected):
        raise DatabaseError("An expected discovery accession disappeared from the final summary.")
    counts = dict(Counter(r["validation_status"] for r in summaries))
    result = {"finished_at_utc": batch.utc_now(), "scope_status": scope["scope_status"], "discovery_snapshot_count": len(expected),
        "summary_filings": len(summaries), "status_counts": counts, "current_shared_filings": len(inventory["filings"]),
        "expected_not_in_shared_database": sum(r["current_database_presence"].startswith("absent") for r in summaries),
        "processing_sets_checked": len(json.loads((output / "processing_candidates.json").read_text())),
        "validation_error_rows": len(errors), "evidence_or_review_rows": len(gaps),
        "source_receipt_conflicts": len(provenance),
        "database_relationship_status_counts": dict(Counter(r["database_relationship_status"] for r in summaries)),
        "original_source_semantics_status_counts": dict(Counter(r["original_source_semantics_status"] for r in summaries)),
        "shared_database_updated": False, "prior_sample_included": True,
        "database_identity": inventory.get("identity"), "SSL": inventory.get("ssl"),
        "section_snapshot_latest_filing_date": scope["snapshot_latest_filing_date"], "future_2026_completeness_asserted": False}
    save_json(output / "run_status.json", result)
    project = Path(__file__).resolve().parents[2]
    git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
    source_files = ("src/sec_pipeline/shortlist_table_validation.py", "src/sec_pipeline/historical_table_validation.py",
        "src/sec_pipeline/table_source_reconstruction.py", "scripts/validate_shortlist_tables.py",
        "src/sec_pipeline/table_relations.py", "src/sec_pipeline/tables.py", "src/sec_pipeline/fact_extraction.py", "src/sec_pipeline/inline_value_transforms.py")
    save_json(output / "run_metadata.json", {"run_started_from_git_head": git_head, "scope": {k: scope[k] for k in
        ("scope_status", "config_sha256", "section_audit_sha256", "snapshot_latest_filing_date")},
        "code_sha256": {name: hashlib.sha256((project / name).read_bytes()).hexdigest() for name in source_files},
        "database_capture_directory": str(snapshot_root.resolve()), "archive_roots": [str(r.resolve()) for r in roots],
        "credential_values_recorded": False})
    company_rows = [{"ticker": t, "snapshot_filings": sum(r["ticker"] == t for r in scope["filings"]),
        "shared_filings": sum(f["ticker"] == t for f in inventory["filings"]),
        "not_ingested": sum(r["ticker"] == t and r["validation_status"] == "not_ingested" for r in summaries),
        "original_source_semantics_passes": sum(r["ticker"] == t and r["original_source_semantics_status"] == "passed" for r in summaries),
        "scope_status": scope["scope_status"]} for t in sorted(tickers)]
    batch.write_csv(output / "company_coverage.csv", company_rows, tuple(company_rows[0]))
    report = f"""# Confirmed-shortlist table validation: source and database results remain separate

## Executive summary

- Confirmed filing-date window: {scope['config']['since']} through {scope['config']['until']}, original 10-K/10-Q, {len(tickers)} companies.
- Discovery snapshot: {len(expected)} filings, latest filing date {scope['snapshot_latest_filing_date']}. This is NOT future-2026 completeness or table acceptance.
- Current read-only inventory: {len(inventory['filings'])} filings; {result['expected_not_in_shared_database']} discovered filings absent from that inventory.
- Table-validation statuses: `{json.dumps(counts, sort_keys=True)}`. Errors and evidence gaps are separate outputs.
- Provenance conflicts: {len(provenance)} different recorded HTML hashes for identical URLs; this does not identify which bytes are correct and is NOT a proven financial-value defect.
- Stored relationship status: `{json.dumps(result['database_relationship_status_counts'], sort_keys=True)}`.
- Original-source semantics status: `{json.dumps(result['original_source_semantics_status_counts'], sort_keys=True)}`; no independent acceptance is asserted.

## Execution and limits

Actual identity/query timestamp: `{json.dumps(inventory.get('identity'), sort_keys=True)}`. TLS: `{json.dumps(inventory.get('ssl'), sort_keys=True)}`.
All receiving queries were READ ONLY; this report replays the preserved snapshots. No shared rows, dates, permissions, versions or preferred sets were changed.
Every available processing candidate was explicitly pinned; finance browsing defaults were not treated as approved selections.
Exact set IDs and versions are in processing_candidates.csv; tested source hashes and Git baseline are in run_metadata.json.
The prior Microsoft sample is explicitly INCLUDED for an independent source/relationship recheck, not silently excluded.

The section audit's reported pass supplies only discovery membership and original HTML receipts. Table/cell/fact relationships,
numeric transformations, statement continuations and table-section links are checked separately by the existing table validators.
Missing originals never become passes. A missing XML receipt has no fabricated URL, hash or UUID. Un-ingested filings remain in the denominator.
Original context evidence and retained review issues are in review_context_evidence.json; no NTAP dates were changed.
Actual traces are in actual_trace_examples.json. Missing/ambiguous sections stay explicit, and paragraph chunks are never converted into table chunks.
Table-chunk protocol remains draft unless Ruby/Michael supply actual producer evidence and confirm its contract.

## Handoff

- Jazzy: validator fixes, all-accession reconciliation, explicit old-sample recheck, tests and reproducible snapshots/results.
- Michael: missing ingestion/processing receipts, official per-accession set choices, reviewed deployment; no shared write was attempted here.
- Acquisition: missing_sources_minimal.csv supplies exact known HTML URLs/hashes and marks unknown XML receipts honestly.
- Bryce: original context/period/unit and unsupported historical numeric transformation evidence; do not silently shift dates.
- Cleaning: missing/ambiguous original-DOM section boundaries. Section UUIDs are receiving evidence, not synthesized keys.
- Ruby/Michael: actual table chunks with stable chunk IDs, financial_tables.id, filing/source/version/set and original locators.
- salu: independent acceptance is NOT asserted.

This is a reproducible table-side handoff with explicit incomplete items, not a claim that all {len(expected)} original tables passed.
"""
    (output / "validation_report.md").write_text(report, encoding="utf-8")
    return 1 if errors else 2
