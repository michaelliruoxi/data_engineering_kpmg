"""Reuse existing table exports in an EMPTY, isolated local PostgreSQL test DB.

Commits public sample data only to that dedicated test DB. Never extracts a
table, connects to RDS, or copies database IDs from another environment.
The optional section SQL proposal is installed ONLY with its explicit flag.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

import psycopg

from sec_pipeline import database as db
from sec_pipeline.catalog import ProcessingSetInput, store_processing_set
from sec_pipeline.fact_extraction import extract_facts, store_facts
from sec_pipeline.report_cleaning import clean_report
from sec_pipeline.table_chunk_links import resolve_table_chunks
from sec_pipeline.table_relations import audit_snapshot, read_snapshot
from sec_pipeline.table_relations_cli import source_bytes, write_outputs, save_json
from sec_pipeline.table_sections import store_table_section_mapping
from sec_pipeline.tables import FactScope, load_tables


def local_options(host, port, database, user):
    if host not in {"127.0.0.1", "::1"} or not re.fullmatch(r"[A-Za-z0-9_]+_test(?:_[A-Za-z0-9_]+)?", database):
        raise ValueError("Use an explicit loopback IP and a dedicated *_test database, never an application database.")
    return dict(host=host, hostaddr=host, port=port, user=user, dbname=database,
                sslmode="disable", password="", passfile="/nonexistent/jazzy-local-test-passfile", connect_timeout=5)


def load_existing(conn, root, exports, sections_file):
    manifest = db._verified_manifest(root / "source_manifest.json", root)
    seeded = db.seed_manifest(conn, root / "source_manifest.json", root)
    roles = {record["role"]: record for record in manifest["files"]}
    html_record, xml_record = roles["main_inline_xbrl_filing"], roles["extracted_xbrl_instance"]
    filing = seeded["filing_id"]
    html_id = seeded["document_ids"][html_record["local_path"]]
    xml_id = seeded["document_ids"][xml_record["local_path"]]
    facts = extract_facts(root / xml_record["local_path"], filing_id=filing, source_document_id=xml_id,
                          extraction_version="facts-v1").require_complete()
    stored = store_facts(conn, facts.facts, bulk=True)
    scope = {"filing_id": str(filing), "xml_document_id": str(xml_id), "facts_version": "facts-v1"}
    tables = []
    for key in ("income_statement", "balance_sheet", "cash_flow"):
        record = json.loads((exports / (key + ".json")).read_text())
        record.update(filing_id=filing, source_document_id=html_id)
        structure = deepcopy(record["structure"])
        if (structure["html_sha256"], structure["xml_sha256"]) != (html_record["sha256"], xml_record["sha256"]):
            raise ValueError("Existing export does not match the verified original archive.")
        structure["fact_scope"] = scope
        for row in structure["rows"]:
            for cell in row:
                for ref in cell["fact_references"]:
                    ref["scope"] = scope
        record["structure"] = structure
        tables.append(db.FinancialTableInput(**record))
    # Receiving DB IDs are obtained from store_facts/load_tables, never imported.
    load_tables(conn, tables, FactScope(filing, xml_id, "facts-v1", stored.fact_ids), bulk=True)
    cleaned = clean_report(root / html_record["local_path"], form=manifest["form"])
    sections = json.loads(sections_file.read_text())
    if sections != cleaned.sections:
        raise ValueError("Prior section descriptors do not match this parser and original HTML.")
    report = db.store_report(conn, db.ReportInput(filing_id=filing, source_document_id=html_id,
        extraction_version="report-v1", full_text=cleaned.full_text, sections=sections))
    selection = store_processing_set(conn, ProcessingSetInput(filing_id=filing, set_key="jazzy-local-relations-fixture-v1",
        fact_source_document_id=xml_id, fact_extraction_version="facts-v1", table_source_document_id=html_id,
        table_extraction_version="tables-v1", report_id=report,
        coverage={"facts": "complete", "tables": "partial", "reports": "complete", "chunks": "absent"},
        validation_status="unvalidated", validation={}, configuration={"local_test_only": True, "independent_review": False}))
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    return manifest["accession_number"], {"contract": "legacy", "id": str(selection)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--table-exports", type=Path, required=True)
    parser.add_argument("--sections", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-section-proposal", action="store_true")
    args = parser.parse_args()
    options = local_options(args.host, args.port, args.database, args.user)
    with psycopg.connect(**options) as conn:
        if conn.info.hostaddr != args.host or conn.info.dbname != args.database:
            raise ValueError("Actual endpoint differs from requested local test database.")
        if not conn.execute("SELECT to_regclass('sec.filings')").fetchone()[0]:
            db.migrate(conn)
        if conn.execute("SELECT count(*) FROM sec.filings").fetchone()[0]:
            raise ValueError("Refusing to change a populated database. Create a new dedicated test database.")
        if args.test_section_proposal:
            proposal = Path(__file__).resolve().parents[1] / "docs/proposals/table_section_mappings.sql"
            conn.execute(proposal.read_text())
        accession, selection = load_existing(conn, args.source_root, args.table_exports, args.sections)
    with psycopg.connect(**options, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = read_snapshot(conn, accession, selection=selection)
    sources = source_bytes(snapshot, args.source_root)
    result = audit_snapshot(snapshot, sources)
    result.update(evidence_origin="isolated_local_PostgreSQL_public_MSFT_source",
                  shared_database="not_validated", actual_chunk="not_supplied")
    table = snapshot["tables"][0]
    table_scope = {"accession": accession, "processing_set": selection, "source/document_set": None}
    synthetic = [{"chunk_id": "SYNTHETIC-local-adapter-fixture-"+str(n), "chunk_type": "table", "synthetic": True,
        "table_ids": [table["id"]], "filing_id": table["filing_id"], "source_document_id": table["source_document_id"],
        "table_version": table["extraction_version"], "source_locator": {"xpath": table["structure"]["source_xpath"]},
        **table_scope} for n in (1, 2)]
    chunks = resolve_table_chunks(synthetic, [table | table_scope], sources, allow_synthetic=True)
    # Exercise commit + replay and caller rollback only in the local proposal.
    if args.test_section_proposal and result["status"] == "passed":
        with psycopg.connect(**options) as conn:
            for mapping in result["section_mappings"]:
                conn.execute("SELECT 1")  # Explicit caller-owned transaction.
                store_table_section_mapping(conn, mapping, sources[mapping["source_document_id"]])
                store_table_section_mapping(conn, mapping, sources[mapping["source_document_id"]])
        with psycopg.connect(**options) as conn:
            rows = conn.execute("SELECT count(*) FROM sec.financial_table_sections").fetchone()[0]
        result["local_persistence"] = {"status": "passed", "rows": rows, "committed_replay": True, "shared_deployed": False}
    write_outputs(args.output, result, snapshot=snapshot)
    save_json(args.output / "synthetic-chunks.json", synthetic)
    save_json(args.output / "synthetic-chunk-mapping.json", chunks)
    print(json.dumps({key: result[key] for key in ("status", "selection", "errors", "unverified", "tables")}, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
