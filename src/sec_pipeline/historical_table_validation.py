"""Candidate historical table validation, without database writes or scope guesses.

One filing is the reporting grain. Every stored processing candidate is checked
separately; counts in filing summaries are DISTINCT database identities across
those candidates, never a sum of overlapping versions. Outputs are private.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from lxml import html
import psycopg

from .database import DatabaseError
from .source_locators import source_xpath
from .table_relations import _fact_input, audit_snapshot, read_snapshot
from .table_relations_cli import save_json, source_bytes
from .tables import select_statement_segments


COMPANIES = dict(AAPL="0000320193", CASY="0000726958", MCK="0000927653", MDT="0001613103",
                 MSFT="0000789019", MU="0000723125", NKE="0000320187", NTAP="0001002047",
                 PH="0000076334", TTWO="0000946581")
PRIOR_SAMPLE = "0001193125-26-191507"
SUMMARY_COLUMNS = ("ticker", "CIK", "accession", "form", "filing_date", "reporting_period",
    "processing_family", "processing_set_id", "table_version", "facts_version", "report_version", "chunk_version",
    "source_document_set", "table_coverage", "table_count", "cell_count", "cell_fact_link_count",
    "unique_membership_count", "unresolved_count", "review_required_count", "source_only_count",
    "validation_status", "issue_count", "evidence_path", "scope_status", "candidate_sources", "source_status",
    "selection_status", "count_grain", "candidate_checks", "database_relationship_status", "original_source_semantics_status")
ISSUE_COLUMNS = ("accession", "processing_family", "processing_set_id", "table_id", "table_key", "cell_id",
    "cell_key", "check", "actual_value", "expected_evidence", "issue_type", "owner", "fix_status", "retest_result")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def assert_readonly(conn):
    if conn.info.transaction_status.name != "INTRANS":
        raise DatabaseError("An explicit read-only transaction is required.")
    if conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] != "on":
        raise DatabaseError("A read-write transaction is prohibited.")


def read_inventory(conn, companies):
    """No year filter: observed DB holdings are not a complete discovery list."""
    assert_readonly(conn)
    identity = conn.execute("SELECT current_user,current_database(),now() AS queried_at,current_setting('transaction_read_only') AS read_only").fetchone()
    ssl = conn.execute("SELECT ssl,version,cipher FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone()
    if identity["current_user"] != "sec_jazzy" or not ssl or not ssl["ssl"]:
        raise DatabaseError("Expected sec_jazzy with a verified SSL session.")
    relations = conn.execute("SELECT table_schema,table_name FROM information_schema.tables WHERE table_schema IN ('sec','finance') ORDER BY 1,2").fetchall()
    result = {"status": "connected_readonly_inventory_completed", "attempted_at_utc": utc_now(), "identity": identity,
        "ssl": ssl, "sslmode": "verify-full", "scope_status": "pending_owner_confirmation", "year_filter": None,
        "relations": relations, "read_only": True,
        "schema": conn.execute("SELECT table_schema,table_name,column_name,data_type FROM information_schema.columns WHERE table_schema IN ('sec','finance') ORDER BY 1,2,ordinal_position").fetchall(),
        "permissions": conn.execute("SELECT n.nspname AS schema,c.relname,has_table_privilege(c.oid,'SELECT') AS can_select,has_table_privilege(c.oid,'INSERT,UPDATE,DELETE') AS can_write FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('sec','finance') AND c.relkind IN ('r','v') ORDER BY 1,2").fetchall()}
    result["companies"] = conn.execute("SELECT * FROM sec.companies WHERE cik=ANY(%s) ORDER BY cik", (list(companies.values()),)).fetchall()
    result["filings"] = conn.execute("SELECT f.*,c.cik,c.ticker FROM sec.filings f JOIN sec.companies c ON c.id=f.company_id WHERE c.cik=ANY(%s) AND f.form IN ('10-K','10-Q') ORDER BY c.ticker,f.filing_date,f.accession_number", (list(companies.values()),)).fetchall()
    ids = [f["id"] for f in result["filings"]]
    names = {r["table_name"] for r in relations if r["table_schema"] == "sec"}
    for name in ("filing_processing_sets", "preferred_processing_sets", "source_processing_sets", "source_document_sets", "source_documents"):
        result[name] = conn.execute(f"SELECT * FROM sec.{name} WHERE filing_id=ANY(%s) ORDER BY 1", (ids,)).fetchall() if name in names else []
    # The browsing view's recommendation is evidence, NOT an approved selection.
    if any(r["table_schema"] == "finance" and r["table_name"] == "filing" for r in relations):
        result["finance_filing"] = conn.execute("SELECT * FROM finance.filing WHERE cik=ANY(%s) ORDER BY ticker,accession_number", (list(companies.values()),)).fetchall()
    return result


def connection_failure(error):
    """Classify without retaining exception strings, DSNs or parameters."""
    import socket
    code = getattr(error, "sqlstate", None)
    message = str(error).lower()  # memory only; never returned or persisted
    phase = ("DNS" if isinstance(error, socket.gaierror) or any(s in message for s in
                ("translate host name", "resolve host", "name or service not known", "nodename nor servname")) else
             "authentication" if code in {"28P01", "28000"} or "password authentication failed" in message or "no password supplied" in message else
             "SSL" if "certificate" in message or "ssl" in message or "tls" in message else
             "permission" if code == "42501" else
             "TCP" if any(s in message for s in ("connection refused", "timeout expired", "network is unreachable", "operation timed out")) else
             "configuration_or_query")
    return {"status": "blocked", "failure_stage": phase, "error_type": type(error).__name__, "sqlstate": code,
            "attempted_at_utc": utc_now(), "error_text": "withheld_to_protect_credentials"}


def merge_candidates(inventory, inputs, companies):
    """Union discoveries/manifests/queue and DB, retaining un-ingested filings."""
    candidates, conflicts, discovery_complete = {}, [], set()

    def add(row, origin, ticker=None):
        cik = str(row.get("cik", "")).zfill(10)
        ticker = ticker or row.get("ticker") or row.get("requested_ticker") or next((t for t, c in companies.items() if c == cik), None)
        if ticker not in companies:
            return
        accession = row.get("accession_number", row.get("accession"))
        if not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            return
        if row.get("form") not in {"10-K", "10-Q"}:
            return
        if row.get("cik") and cik != companies[ticker]:
            conflicts.append({"accession": accession, "field": "CIK", "values": [companies[ticker], cik], "origin": origin})
            return
        clean = {"ticker": ticker, "CIK": companies[ticker], "accession": accession, "form": row["form"],
                 "filing_date": row.get("filing_date"), "reporting_period": row.get("reporting_period_end", row.get("reporting_period")),
                 "filing_id": row.get("filing_id", row.get("id")) if origin == "shared_database" else None,
                 "candidate_sources": [origin], "scope_status": "pending_owner_confirmation"}
        if accession in candidates:
            current = candidates[accession]
            for key in ("ticker", "CIK", "form", "filing_date", "reporting_period"):
                if current.get(key) and clean.get(key) and current[key] != clean[key]:
                    conflicts.append({"accession": accession, "field": key, "values": [current[key], clean[key]], "origin": origin})
                elif not current.get(key):
                    current[key] = clean.get(key)
            current["candidate_sources"] = sorted(set(current["candidate_sources"] + [origin]))
        else:
            candidates[accession] = clean

    for row in inventory.get("filings", []):
        add(row, "shared_database")
    for origin, data in inputs:
        if isinstance(data, list):
            for row in data:
                add(row, origin)
        elif "accession_number" in data and "files" in data:
            add(data, origin)
        elif isinstance(data.get("companies"), dict):
            for ticker, company in data["companies"].items():
                # Completeness only applies to an explicitly stated discovery
                # window, never all history or the unconfirmed team scope.
                if data.get("discovery_complete") and company.get("status") == "discovered":
                    discovery_complete.add(ticker)
                for row in company.get("filings", []):
                    add(row, origin, ticker)
            # Backfill queues also have a companies status dictionary, but
            # their filing records live at top level. Do not drop failed or
            # pending acquisitions merely because they are not company-nested.
            rows = data.get("filings", data.get("items", data.get("verified_filings", {})))
            for row in rows.values() if isinstance(rows, dict) else rows:
                if isinstance(row, dict):
                    add(row.get("filing", row), origin)
        else:
            rows = data.get("filings", data.get("items", data.get("verified_filings", [])))
            for row in rows.values() if isinstance(rows, dict) else rows:
                if isinstance(row, dict):
                    add(row.get("filing", row), origin)
    return sorted(candidates.values(), key=lambda r: (r["ticker"], r["filing_date"] or "", r["accession"])), conflicts, discovery_complete


def processing_candidates(inventory, filing_id):
    result = []
    for relation, family in (("filing_processing_sets", "legacy"), ("source_processing_sets", "source-v4")):
        for row in inventory.get(relation, []):
            if str(row["filing_id"]) == str(filing_id):
                result.append({"contract": family, "id": str(row["id"]), "row": row})
    return sorted(result, key=lambda s: (s["contract"], s["id"]))


def parse_selections(values):
    from uuid import UUID
    result = {}
    for value in values:
        try:
            accession, choice = value.split("=", 1)
            family, set_id = choice.split(":", 1)
            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or family not in {"legacy", "source-v4"}:
                raise ValueError
            UUID(set_id)
        except ValueError:
            raise DatabaseError("--processing-set requires accession=legacy:UUID or accession=source-v4:UUID") from None
        if accession in result:
            raise DatabaseError("Conflicting duplicate processing-set override.")
        result[accession] = {"contract": family, "id": set_id}
    return result


def available_sources(snapshot, roots):
    payloads, availability = {}, []
    receipts = {str(r["id"]): r for r in snapshot["sources"]}
    for root in roots:
        for key, source in source_bytes(snapshot, root).items():
            expected = receipts[key]["sha256"]
            if key not in payloads or hashlib.sha256(source).hexdigest() == expected:
                payloads[key] = source
    for receipt in snapshot["sources"]:
        source = payloads.get(str(receipt["id"]))
        availability.append({"source_document_id": str(receipt["id"]), "role": receipt["role"],
            "local_path": receipt["local_path"], "source_url": receipt["source_url"], "sha256": receipt["sha256"],
            "availability": "missing_local_bytes" if source is None else
                "hash_verified" if hashlib.sha256(source).hexdigest() == receipt["sha256"] else "hash_mismatch"})
    return payloads, availability


def coverage_check(snapshot, sources):
    """Reselect statement groups; do not assume one physical table per kind."""
    if not snapshot["tables"]:
        return {"status": "unverified", "reason": "no_stored_tables"}
    evidence = []
    for html_id in sorted({t["source_document_id"] for t in snapshot["tables"]}):
        tables = [t for t in snapshot["tables"] if t["source_document_id"] == html_id]
        scope = tables[0]["structure"].get("fact_scope", {})
        if html_id not in sources or scope.get("xml_document_id") not in sources:
            evidence.append({"status": "unverified", "reason": "original_HTML_or_XML_unavailable", "source_document_id": html_id})
            continue
        tree = html.document_fromstring(sources[html_id], parser=html.HTMLParser(encoding="utf-8", no_network=True)).getroottree()
        facts = [_fact_input(f) for f in snapshot["facts"] if f["filing_id"] == scope.get("filing_id")
                 and f["source_document_id"] == scope.get("xml_document_id") and f["extraction_version"] == scope.get("facts_version")]
        try:
            selected = select_statement_segments(tree, facts)
            expected = Counter((s.statement_type, source_xpath(tree, s.element)) for s in selected)
            actual = Counter((t["statement_type"], t["structure"].get("source_xpath")) for t in tables)
            evidence.append({"status": "passed" if actual == expected else "failed", "source_document_id": html_id,
                "expected_physical_segments": [{"type": k[0], "xpath": k[1], "count": v} for k, v in sorted(expected.items())],
                "stored_physical_segments": [{"type": k[0], "xpath": k[1], "count": v} for k, v in sorted(actual.items())]})
        except ValueError as error:
            evidence.append({"status": "unverified", "reason": "existing_statement_selector_cannot_establish_coverage", "error_type": type(error).__name__})
    return {"status": "failed" if any(e["status"] == "failed" for e in evidence) else
            "unverified" if any(e["status"] == "unverified" for e in evidence) else "passed", "documents": evidence,
            "method": "existing_statement_segment_selector_not_manual_independent_acceptance"}


def audit_candidate(snapshot, roots):
    from decimal import Decimal, InvalidOperation
    sources, availability = available_sources(snapshot, roots)
    result = audit_snapshot(snapshot, sources)
    # This is an internal stored-fact consistency check, NOT proof of the
    # displayed value or its transformation from the unavailable original HTML.
    accepted_fact_ids = {link["fact_id"] for link in snapshot["cell_fact_links"]}
    for fact in snapshot["facts"]:
        if fact["id"] not in accepted_fact_ids:
            continue
        try:
            expected = None if fact["is_nil"] or fact["unit"] is None else Decimal(fact["raw_value"])
            observed = None if fact["numeric_value"] is None else Decimal(fact["numeric_value"])
            correct = expected == observed and (expected is None or expected.is_finite())
        except (InvalidOperation, TypeError):
            correct = False
        if not correct:
            result["errors"].append({"reason": "stored_fact_raw_numeric_inconsistency", "fact_id": fact["id"],
                "raw_value": fact["raw_value"], "numeric_value": fact["numeric_value"], "is_nil": fact["is_nil"]})
    result["source_availability"] = availability
    result["coverage_check"] = coverage_check(snapshot, sources)
    mapped_ids = {m["table_id"] for m in result["section_mappings"]}
    for table in snapshot["tables"]:
        if table["id"] not in mapped_ids:
            result["section_mappings"].append({"accession": snapshot["accession"], "processing_set": snapshot.get("selection"),
                "source/document_set": snapshot.get("selected_processing_set", {}).get("document_set_id"),
                "table_id": table["id"], "table_key": table["table_key"], "table_xpath": table["structure"].get("source_xpath"),
                "section_uuid": None, "section_key": None, "section_title": None, "section_locator": None,
                "mapping_status": "unverified_original_HTML_or_matching_report_missing",
                "evidence": {"source_document_id": table["source_document_id"], "html_sha256": table["structure"].get("html_sha256")}})
    unresolved = [c for c in snapshot["table_cells"] if c["resolution_status"] in {"unresolved", "review_required"}]
    # Structurally valid unresolved/review rows are not semantic passes.
    for cell in unresolved:
        result["unverified"].append({"table_id": cell["table_id"], "cell_id": cell["id"], "cell_key": cell["cell_key"],
            "reason": cell["resolution_status"] + "_cell_requires_disposition", "unresolved_reason": cell.get("unresolved_reason")})
    if result["coverage_check"]["status"] == "failed":
        result["errors"].append({"reason": "statement_segment_coverage_mismatch", "evidence": result["coverage_check"]})
    if result["coverage_check"]["status"] != "passed":
        result["unverified"].append({"reason": "original_statement_coverage_not_verified"})
    result["relational_checks_status"] = "failed" if result["errors"] else "passed_stored_evidence_only"
    if result["errors"]:
        result["status"] = "failed"
    elif result["unverified"] or result.get("processing_validation_status") != "passed":
        result["status"] = "unverified"
    for example in result["trace_examples"]:
        table = next(t for t in snapshot["tables"] if t["id"] == example["table_id"])
        fact = next(f for f in snapshot["facts"] if f["id"] == example["fact_id"])
        example.update(accession=snapshot["accession"], processing_set=snapshot.get("selection"),
            table_source=next(s for s in snapshot["sources"] if s["id"] == table["source_document_id"]),
            fact_source=next((s for s in snapshot["sources"] if s["id"] == fact["source_document_id"]), None),
            fact_source_reference=fact["source_reference"], raw_source_verification="verified" if result["status"] == "passed" else "partial_not_end_to_end")
        cell = next(c for c in snapshot["table_cells"] if c["id"] == example["cell_id"])
        link = next(link for link in snapshot["cell_fact_links"] if link["cell_id"] == example["cell_id"] and link["fact_id"] == example["fact_id"])
        example.update(display_text=cell["display_text"], row_heading=cell["row_heading"], column_headings=cell["column_headings"],
            stored_link_evidence=link["evidence"], section_candidates_not_mapped=[{
                key: s.get(key) for key in ("id", "section_key", "original_title", "source_locator", "validation_status")}
                for s in snapshot["sections"] if s["report_id"] in {r["id"] for r in snapshot["reports"] if r["source_document_id"] == table["source_document_id"]}])
    return result


def issue_rows(accession, selection, result):
    rows = []
    for kind in ("errors", "unverified"):
        for issue in result.get(kind, []):
            reason = issue["reason"]
            owner = ("Acquisition" if "missing" in reason.lower() and any(x in reason.lower() for x in ("html", "xml", "source")) else
                     "Cleaning/Chunking" if "section" in reason or "report" in reason else
                     "Bryce/Financial Data + Jazzy" if any(x in reason for x in ("fact_evidence", "original_XML", "scoped_fact", "review_required", "unresolved_cell")) else "Jazzy")
            rows.append({"accession": accession, "processing_family": selection["contract"], "processing_set_id": selection["id"],
                **{k: issue.get(k) for k in ("table_id", "table_key", "cell_id", "cell_key")}, "check": reason,
                "actual_value": issue, "expected_evidence": "Exact pinned processing scope and unchanged HTML/XML receipts; see per-set snapshot and checks",
                "issue_type": "validation_failure" if kind == "errors" else "unverified_evidence_or_disposition",
                "owner": owner, "fix_status": "not_written_to_shared_database", "retest_result": "failed" if kind == "errors" else "unverified"})
    return rows


def summarize(candidate, checks, snapshots, issues, inventory, sets=None):
    row = dict(candidate)
    sets = processing_candidates(inventory, candidate.get("filing_id")) if sets is None else sets
    row.update(processing_family="|".join(sorted({s["contract"] for s in sets})),
        processing_set_id="|".join(s["id"] for s in sets), selection_status="all_candidates_pinned_separately_not_owner_approved",
        count_grain="distinct_DB_IDs_across_all_candidate_sets; not_one_preferred_version", evidence_path=candidate["accession"],
        candidate_checks=len(checks), issue_count=len([i for i in issues if i["accession"] == candidate["accession"]]))
    for target, old, new in (("table_version", "table_extraction_version", "tables"), ("facts_version", "fact_extraction_version", "facts"),
                              ("report_version", "report_extraction_version", "reports"), ("chunk_version", "chunking_version", "chunks")):
        row[target] = "|".join(sorted({str(s["row"].get("versions", {}).get(new) or s["row"].get(old)) for s in sets
                                    if s["row"].get("versions", {}).get(new) or s["row"].get(old)}))
    row["source_document_set"] = "|".join(sorted({str(s["row"]["document_set_id"]) for s in sets if s["row"].get("document_set_id")}))
    row["report_version"] = "|".join(sorted({r["extraction_version"] for snap in snapshots for r in snap["reports"]})) or row["report_version"]
    row["chunk_version"] = "|".join(sorted({c["chunking_version"] for snap in snapshots for c in snap.get("chunks", [])})) or row["chunk_version"]
    if candidate["accession"] == PRIOR_SAMPLE:
        row.update(validation_status="excluded_prior_sample_not_revalidated", source_status="not_rechecked", table_coverage="not_rechecked",
            database_relationship_status="not_rechecked", original_source_semantics_status="not_rechecked")
        return row
    tables = {r["id"]: r for snap in snapshots for r in snap["tables"]}
    cells = {r["id"]: r for snap in snapshots for r in snap["table_cells"]}
    links = {(r["cell_id"], r["fact_id"]): r for snap in snapshots for r in snap["cell_fact_links"]}
    members = {(r["table_id"], r["fact_id"]) for snap in snapshots for r in snap["financial_table_facts"]}
    if snapshots:
        row.update(table_count=len(tables), cell_count=len(cells), cell_fact_link_count=len(links), unique_membership_count=len(members),
            unresolved_count=sum(c["resolution_status"] == "unresolved" for c in cells.values()),
            review_required_count=sum(c["resolution_status"] == "review_required" for c in cells.values()),
            source_only_count=sum(c["resolution_status"] == "source_only" for c in cells.values()))
    row["table_coverage"] = "failed" if any(c["coverage_check"]["status"] == "failed" for c in checks) else "passed" if checks and all(c["coverage_check"]["status"] == "passed" for c in checks) else "unverified"
    row["source_status"] = "receipts_present_bytes_unavailable_locally" if any(a["availability"] == "missing_local_bytes" for c in checks for a in c["source_availability"]) else "available" if checks else "not_checked"
    row["validation_status"] = ("not_ingested" if not candidate.get("filing_id") else "unprocessed" if not sets else
        "failed" if any(c["status"] == "failed" for c in checks) else
        "blocked" if not checks or row["source_status"] != "available" else "unverified_selection_or_disposition")
    row["database_relationship_status"] = ("failed" if any(c.get("relational_checks_status") == "failed" for c in checks) else
        "passed_stored_evidence_only" if checks and len(checks) == len(sets) else "unverified")
    row["original_source_semantics_status"] = "passed" if checks and all(c["status"] == "passed" for c in checks) else "unverified"
    return row


def write_csv(path, rows, columns):
    import csv
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, sort_keys=True, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in row.items() if key in columns})


def source_requirements(inventory, companies, roots):
    """Receipt paths may refer to another import machine; resolve, never assume."""
    filings = {str(f["id"]): f for f in inventory.get("filings", []) if f.get("ticker") in companies}
    rows = []
    for source in inventory.get("source_documents", []):
        filing = filings.get(str(source["filing_id"]))
        if not filing:
            continue
        relative = source["local_path"]
        attempted, existing, hashes = [], [], []
        for root in roots:
            root = root.resolve()
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or re.match(r"^[A-Za-z]:[\\/]", relative):
                continue
            attempted.append(str(path))
            if path.is_file():
                existing.append(str(path))
                hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
        excluded = filing["accession_number"] == PRIOR_SAMPLE
        rows.append({"ticker": filing["ticker"], "accession": filing["accession_number"], "source_document_id": str(source["id"]),
            "source_url": source["source_url"], "local_path": relative, "SHA-256": source["sha256"], "file_type": source["role"],
            "local_exists": bool(existing), "resolved_paths": existing, "attempted_paths": attempted,
            "local_sha256": hashes, "hash_status": "missing" if not hashes else "matched" if source["sha256"] in hashes else "mismatch",
            "needed_for_this_run": not excluded, "minimum_relationship_source": not excluded and source["role"] in {"main_inline_xbrl_filing", "extracted_xbrl_instance"},
            "source_path_note": "DB relative path; original import-machine archive root is not known"})
    return sorted(rows, key=lambda r: (r["ticker"], r["accession"], r["file_type"]))


def download_required_sources(requirements, root, *, identity_approved):
    """Reuse the SEC client; immutable new archive, no ingest, no hash changes."""
    from .acquisition import AccessBlocked, AcquisitionError, SecClient, validate_sec_url
    root = root.resolve()
    status = {"checked_at_utc": utc_now(), "database_writes": False, "identity_value_recorded": False,
        "requested": sum(r["minimum_relationship_source"] and r["hash_status"] == "missing" for r in requirements),
        "files": [], "requests": []}
    if not identity_approved:
        return status | {"status": "blocked_identity_authorization_required"}
    try:
        client = SecClient(interval_seconds=1.0)
    except AcquisitionError:
        return status | {"status": "blocked_SEC_contact_identity_missing_or_invalid", "HTTP_requests": 0}
    with client:
        for row in requirements:
            if not row["minimum_relationship_source"] or row["hash_status"] != "missing":
                continue
            entry = {"accession": row["accession"], "source_url": row["source_url"], "expected_sha256": row["SHA-256"]}
            try:
                validate_sec_url(row["source_url"])
                if f"/{row['accession'].replace('-', '')}/" not in row["source_url"]:
                    raise DatabaseError("SEC source URL does not belong to the receipt accession.")
                path = (root / row["local_path"]).resolve()
                if not path.is_relative_to(root) or re.match(r"^[A-Za-z]:[\\/]", row["local_path"]):
                    raise DatabaseError("Receipt path is not portable within the new archive root.")
                if path.exists():
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    entry.update(status="existing_bytes_retained", observed_sha256=digest, hash_matches=digest == row["SHA-256"])
                else:
                    data, metadata = client.get(row["source_url"])
                    entry.update(observed_sha256=metadata["sha256"], hash_matches=metadata["sha256"] == row["SHA-256"])
                    # Mismatching bytes are retained separately, not at the
                    # receipt's canonical path and never accepted as originals.
                    target = path if entry["hash_matches"] else root / "hash-differences" / row["accession"] / (metadata["sha256"] + "-" + path.name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as stream:
                        stream.write(data)
                    entry.update(status="downloaded_hash_matched" if entry["hash_matches"] else "downloaded_hash_mismatch_not_original", saved_path=str(target))
            except AccessBlocked:
                entry["status"] = "SEC_access_blocked_session_stopped"
                status["files"].append(entry)
                break
            except (AcquisitionError, DatabaseError, OSError) as error:
                entry.update(status="unavailable", error_type=type(error).__name__)
            status["files"].append(entry)
    status["requests"] = client.request_log
    status["status"] = "attempted_not_automatically_accepted"
    return status


def run_validation(inventory, inputs, companies, roots, output, snapshot_reader, selections=None):
    from .table_sections import canonical
    inventory = canonical(inventory)
    candidates, conflicts, discovery_complete = merge_candidates(inventory, inputs, companies)
    requirements = source_requirements(inventory, companies, roots)
    selections = selections or {}
    known = {c["accession"]: c for c in candidates}
    for accession, selection in selections.items():
        if accession not in known or not any({k: s[k] for k in ("contract", "id")} == selection
                for s in processing_candidates(inventory, known[accession].get("filing_id"))):
            raise DatabaseError("Processing-set override is missing, cross-filing or outside the requested manifest/tickers.")
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "source_requirements.csv", requirements, tuple(requirements[0]) if requirements else ("accession", "source_url", "local_path", "SHA-256", "file_type", "local_exists"))
    minimum = [r for r in requirements if r["minimum_relationship_source"] and r["hash_status"] != "matched"]
    write_csv(output / "missing_sources_minimal.csv", minimum, tuple(requirements[0]) if requirements else ("accession",))
    save_json(output / "candidate_manifest.json", {"scope_status": "pending_owner_confirmation", "year_filter": None,
        "basis": "Union of observed shared DB holdings and explicitly supplied discovery/manifests; NOT a final shortlist",
        "filings": candidates, "metadata_conflicts": conflicts})
    issues, summaries, versions, traces, mappings = [], [], [], [], []
    for conflict in conflicts:
        issues.append({"accession": conflict["accession"], "check": "input_metadata_conflict", "actual_value": conflict,
            "issue_type": "scope_or_identity_conflict", "owner": "Michael + Acquisition", "fix_status": "needs_confirmation", "retest_result": "unverified"})
    for candidate in candidates:
        accession = candidate["accession"]
        checks, snapshots = [], []
        sets = processing_candidates(inventory, candidate.get("filing_id"))
        if accession in selections:
            sets = [s for s in sets if {k: s[k] for k in ("contract", "id")} == selections[accession]]
        if accession != PRIOR_SAMPLE:
            for selection in sets:
                key = f"{selection['contract']}--{selection['id']}"
                directory = output / accession / key
                directory.mkdir(parents=True, exist_ok=True)
                try:
                    snapshot = snapshot_reader(accession, {k: selection[k] for k in ("contract", "id")})
                    save_json(directory / "snapshot.json", snapshot)
                    check = audit_candidate(snapshot, roots)
                    check["evidence_origin"] = inventory.get("evidence_origin", "saved_shared_inventory_and_snapshot_replay_not_live")
                    save_json(directory / "checks.json", check)
                    snapshots.append(snapshot)
                    checks.append(check)
                    issues.extend(issue_rows(accession, selection, check))
                    examples = check["trace_examples"]
                    if examples:
                        example = next((e for e in examples if e["fact"].get("unit") is not None), examples[0])
                        traces.append(example | {"ticker": candidate["ticker"]})
                    mappings.extend(check["section_mappings"])
                    metrics = {"table_count": len(snapshot["tables"]), "cell_count": len(snapshot["table_cells"]),
                        "cell_fact_link_count": len(snapshot["cell_fact_links"]), "unique_membership_count": len({(m["table_id"], m["fact_id"]) for m in snapshot["financial_table_facts"]})}
                    versions.append({"accession": accession, "ticker": candidate["ticker"], "processing_family": selection["contract"],
                        "processing_set_id": selection["id"], **selection["row"], **metrics,
                        "report_version": sorted({r["extraction_version"] for r in snapshot["reports"]}),
                        "chunk_version": sorted({c["chunking_version"] for c in snapshot["chunks"]}),
                        "check_status": check["status"], "relational_checks_status": check["relational_checks_status"], "evidence_path": str(directory.relative_to(output))})
                except (psycopg.Error, DatabaseError, KeyError, TypeError, ValueError, OSError) as error:
                    failure = {"error_type": type(error).__name__, "status": "snapshot_or_audit_incomplete"}
                    save_json(directory / "failure.json", failure)
                    issues.append({"accession": accession, "processing_family": selection["contract"], "processing_set_id": selection["id"],
                        "check": "snapshot_or_audit_incomplete", "actual_value": failure, "issue_type": "blocked",
                        "owner": "Jazzy + Michael", "fix_status": "not_written_to_shared_database", "retest_result": "unverified"})
        row = summarize(candidate, checks, snapshots, issues, inventory, sets)
        if len(checks) != len(sets) and accession != PRIOR_SAMPLE and row["validation_status"] != "failed":
            row["validation_status"] = "blocked"
        if accession in selections:
            row["selection_status"] = "explicit_override_pinned_not_approval_of_team_scope"
        summaries.append(row)
        print(json.dumps({"accession": accession, "ticker": candidate["ticker"], "status": row["validation_status"], "candidate_sets_checked": len(checks)}), flush=True)
    coverage = [{"ticker": t, "CIK": c, "observed_candidate_filings": sum(r["ticker"] == t for r in candidates),
        "expected_final_filing_count": None, "missing_final_accessions": "unknown_without_final_list",
        "discovery_status": "supplied_discovery_window_complete_not_final_scope" if t in discovery_complete else "no_complete_discovery_inventory_available",
        "scope_status": "pending_owner_confirmation"} for t, c in sorted(companies.items())]
    save_json(output / "processing_candidates.json", versions)
    save_json(output / "trace_examples.json", traces)
    save_json(output / "trace_examples_by_company.json", [next(t for t in traces if t["ticker"] == ticker)
        for ticker in sorted({t["ticker"] for t in traces})])
    save_json(output / "table_section_mapping.json", mappings)
    write_csv(output / "table_section_mapping.csv", mappings, ("accession", "processing_set", "source/document_set", "table_id", "table_key", "table_xpath", "section_uuid", "section_key", "section_title", "section_locator", "mapping_status", "evidence"))
    write_csv(output / "validation_summary.csv", summaries, SUMMARY_COLUMNS)
    write_csv(output / "validation_issues.csv", issues, ISSUE_COLUMNS)
    write_csv(output / "company_coverage.csv", coverage, tuple(coverage[0]) if coverage else ("ticker",))
    write_csv(output / "processing_candidates.csv", versions, ("ticker", "accession", "processing_family", "processing_set_id", "set_key", "validation_status", "source_validation_status", "coverage", "counts", "versions", "table_extraction_version", "fact_extraction_version", "report_version", "chunk_version", "fact_source_document_id", "table_source_document_id", "report_id", "document_set_id", "table_count", "cell_count", "cell_fact_link_count", "unique_membership_count", "check_status", "relational_checks_status", "evidence_path"))
    counts = dict(Counter(r["validation_status"] for r in summaries))
    save_json(output / "run_status.json", {"finished_at_utc": utc_now(), "scope_status": "pending_owner_confirmation", "companies": len(companies),
        "candidate_filings": len(candidates), "status_counts": counts, "candidate_sets_checked": len(versions), "issues": len(issues), "shared_database_updated": False})
    relationship_counts = dict(Counter(r["database_relationship_status"] for r in summaries))
    source_counts = dict(Counter(r["original_source_semantics_status"] for r in summaries))
    company_lines = "\n".join(f"| {r['ticker']} | {r['observed_candidate_filings']} | Unknown; owner confirmation needed |" for r in coverage)
    report = f"""# Historical table relationship validation

Scope: {len(companies)} requested companies; {len(candidates)} observed candidate filings. **Pending owner confirmation.**
Run completed: {utc_now()}. No final accession list or final year range was assumed.
The phrase '17 10Q/10K' was not interpreted. Missing historical accessions and the expected final count remain unknown.

## Results

Status counts: `{json.dumps(counts, sort_keys=True)}`.
Processing candidates actually checked: {len(versions)}. Issues / evidence gaps: {len(issues)}.
Database relationship status: `{json.dumps(relationship_counts, sort_keys=True)}`.
Original-file semantics status: `{json.dumps(source_counts, sort_keys=True)}`.
Minimum missing or mismatching HTML/XML receipts: {len(minimum)}. See missing_sources_minimal.csv;
source_requirements.csv includes all {len(requirements)} source receipts and their resolved local availability.
SEC downloads require the configured real EDGAR_IDENTITY and explicit approval; see source-download-status.json when attempted.
The old Microsoft sample {PRIOR_SAMPLE} is excluded from revalidation.
No filing is accepted against an owner-approved final processing selection in this run.
No shared database rows, preferences, permissions, migrations or sealed results were changed.

| Company | Observed candidate filings | Final expected / missing filings |
| --- | ---: | --- |
{company_lines}

## Methods and versions

Read-only REPEATABLE READ inventory and explicitly pinned snapshots of EVERY available candidate set; no newest-set assumption.
See shared-inventory.json for identity, TLS, query timestamp, schema, grants and the finance view's recorded
selection_reason / validation_status / coverage. That browsing recommendation is not an approved processing selection.
See processing_candidates.csv for actual set IDs, versions, counts, coverage and per-set results.
Summary counts are DISTINCT database IDs across the candidate sets, not one selected version and not sums of overlapping sets.
Zeros mean queried empty relations; blanks mean not queried. Review-required and unresolved cells are not semantic passes.

Checks reuse read_snapshot / audit_snapshot: retained cell structure, parents, scoped fact evidence,
periods, units, concepts, value metadata, links, memberships, v4 occurrences and quality issues.
When original bytes exist, check receipt hashes, physical DOM grids, executable XPaths, XML facts,
inline scale/sign and section boundaries; reselect statement segments without forcing exactly three records.
Unavailable HTML/XML means source-level coverage, omitted/duplicate/note/layout-table detection and original
value transformations remain UNVERIFIED. Stored JSON agreeing with stored rows is only a partial check.
Section UUIDs are never manufactured. Trace examples describe actual stored chains, not source-level acceptance
when original bytes are absent. No synthetic chunk is presented as a real trace.

## Evidence and reproduction

validation_summary.csv: one row per observed filing, including prior-sample exclusion and un-ingested discoveries.
validation_issues.csv: failures and missing evidence; company_coverage.csv: unknown missing-history denominator.
Each accession / family--set-ID directory contains the read-only snapshot and checks (or an explicit failure receipt).
Source identities, URLs and SHA-256 hashes are in snapshots and source_availability; paths do not imply local availability.
See docs/historical-table-validation.md for commands and exit codes. Outputs remain private and are not committed.

## Remaining dependencies

- Scope owner (Emma / Michael): final accession manifest, meaning of '17', inclusion rules, approved per-filing processing sets.
- Acquisition: complete discovery.json with window/completeness evidence and archive files matching stored source hashes.
- Bryce / Financial Data: disposition of fact/period/unit issues and review-required occurrence evidence; see issue rows.
- Michael: approve processing/document-set choices and any separately reviewed shared deployment. No shared fixes were made.
- Cleaning / Chunking: original-HTML section locators and ambiguous/missing section boundaries. Missing maps are separate evidence gaps.
- salu: independent acceptance of scope, source evidence and results; independent review is NOT asserted.

This is a reproducible candidate audit with explicit limitations, not a claim that all historical tables passed acceptance.
"""
    (output / "validation_report.md").write_text(report, encoding="utf-8")
    return 1 if counts.get("failed") else 2  # Final scope remains pending, even if partial checks pass.
