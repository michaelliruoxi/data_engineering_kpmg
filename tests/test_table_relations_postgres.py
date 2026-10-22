"""Opt-in checks against the runner's isolated, public MSFT sample database.

No migrations, credentials, shared deployment, or deletions are performed here.
Set SEC_RELATIONS_DB_TEST=1, PGHOST/PGPORT/PGUSER, SEC_RELATIONS_DB_DATABASE,
and SEC_RELATIONS_SOURCE_ROOT after running run_table_relations_local.py.
"""
from copy import deepcopy
import os
from pathlib import Path
from uuid import UUID

import psycopg
import pytest

from sec_pipeline import database as db
from sec_pipeline.table_relations import audit_snapshot, read_snapshot, validate_selection
from sec_pipeline.table_relations_cli import source_bytes, write_outputs
from sec_pipeline.table_sections import map_table_section, store_table_section_mapping
from scripts.run_table_relations_local import local_options


@pytest.fixture(scope="module")
def sample():
    if os.environ.get("SEC_RELATIONS_DB_TEST") != "1":
        pytest.skip("Opt-in isolated MSFT relationship database not selected.")
    options = local_options(os.environ["PGHOST"], int(os.environ["PGPORT"]),
                            os.environ["SEC_RELATIONS_DB_DATABASE"], os.environ["PGUSER"])
    with psycopg.connect(**options, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = conn.execute("SELECT id FROM sec.filing_processing_sets WHERE set_key='jazzy-local-relations-fixture-v1'").fetchone()
            assert row, "Run the public sample loader in a new isolated DB first."
            selection = {"contract": "legacy", "id": str(row[0])}
            snapshot = read_snapshot(conn, "0001193125-26-191507", selection=selection)
    return options, snapshot, source_bytes(snapshot, Path(os.environ["SEC_RELATIONS_SOURCE_ROOT"]))


def test_actual_receiving_rows_and_duplicate_occurrences_are_correct(sample):
    _, snapshot, sources = sample
    result = audit_snapshot(snapshot, sources)
    assert result["status"] == "passed" and result["errors"] == result["unverified"] == []
    assert len(snapshot["facts"]) == 1622
    assert sum(t["anchor_cells"] for t in result["tables"]) == 2285
    assert sum(t["cell_references"] for t in result["tables"]) == 284
    assert sum(t["unique_memberships"] for t in result["tables"]) == 283
    assert all(m["section_key"] == "part-i-item-1" and UUID(m["section_uuid"]) for m in result["section_mappings"])
    assert result["processing_validation_status"] == "unvalidated"  # Not team/human acceptance.
    assert all(t["table_chunk"] is None for t in result["trace_examples"])


@pytest.mark.parametrize("defect", ["missing_table", "wrong_table_parent", "missing_fact", "wrong_fact_id",
    "cross_filing", "cross_source", "cross_version", "cross_processing_set", "link_evidence", "fact_period",
    "fact_unit", "fact_value", "scale", "sign", "membership", "missing_section", "wrong_section_id"])
def test_mutated_row_snapshots_detect_specific_relationship_defects(sample, defect):
    _, original, sources = sample
    snapshot = deepcopy(original)
    table = snapshot["tables"][0]
    link = next(row for row in snapshot["cell_fact_links"] if row["table_id"] == table["id"])
    fact = next(row for row in snapshot["facts"] if row["id"] == link["fact_id"])
    bad = str(UUID(int=999))
    if defect == "missing_table":
        table["id"] = bad
    elif defect == "wrong_table_parent":
        link["table_id"] = bad
    elif defect == "missing_fact":
        snapshot["facts"].remove(fact)
    elif defect == "wrong_fact_id":
        link["fact_id"] = bad
    elif defect in {"cross_filing", "cross_source", "cross_version"}:
        fact[{"cross_filing": "filing_id", "cross_source": "source_document_id", "cross_version": "extraction_version"}[defect]] = bad
    elif defect == "cross_processing_set":
        snapshot["selection"]["id"] = bad
    elif defect == "link_evidence":
        link["evidence"]["raw_value"] = "not-the-original-value"
    elif defect == "fact_period":
        fact["period_end"] = "1900-01-01"
        fact["instant_date"] = "1900-01-01"
    elif defect == "fact_unit":
        fact["unit"] = {"numerator": ["EUR"], "denominator": []}
    elif defect == "fact_value":
        fact["numeric_value"] = "999"
    elif defect in {"scale", "sign"}:
        ref = next(r for row in table["structure"]["rows"] for c in row for r in c["fact_references"])
        ref["scale" if defect == "scale" else "inline_sign"] = "999" if defect == "scale" else "-"
    elif defect == "membership":
        snapshot["financial_table_facts"] = []
    elif defect == "missing_section":
        snapshot["sections"] = []
    else:
        snapshot["sections"][0]["id"] = "not-a-database-UUID"
    result = audit_snapshot(snapshot, sources)
    assert result["status"] == "failed" and result["errors"], defect


def test_repeat_export_is_byte_stable_without_reassigning_IDs(sample, tmp_path):
    _, snapshot, sources = sample
    result = audit_snapshot(snapshot, sources)
    write_outputs(tmp_path / "first", result, snapshot=snapshot)
    write_outputs(tmp_path / "second", audit_snapshot(snapshot, sources), snapshot=snapshot)
    assert {p.name: p.read_bytes() for p in (tmp_path / "first").iterdir()} == {p.name: p.read_bytes() for p in (tmp_path / "second").iterdir()}


def test_read_snapshot_requires_readonly_and_rejects_nonexistent_selection(sample):
    options, snapshot, _ = sample
    with psycopg.connect(**options) as conn:
        conn.execute("SELECT 1")
        with pytest.raises(db.DatabaseError, match="read-only"):
            read_snapshot(conn, snapshot["accession"])
        with pytest.raises(db.DatabaseError):
            validate_selection(conn, snapshot["tables"][0], snapshot["reports"][0], {"contract": "legacy", "id": str(UUID(int=999))})
    with psycopg.connect(**options, autocommit=True) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            assert read_snapshot(conn, snapshot["accession"])["selection"] is None  # Never choose newest candidate.
            with pytest.raises(db.DatabaseError, match="absent"):
                read_snapshot(conn, snapshot["accession"], selection={"contract": "legacy", "id": str(UUID(int=999))})


def test_proposal_idempotence_and_receiving_UUID_guards(sample):
    options, snapshot, sources = sample
    mapping = audit_snapshot(snapshot, sources)["section_mappings"][0]
    with psycopg.connect(**options) as conn:
        with conn.transaction(force_rollback=True):
            assert store_table_section_mapping(conn, mapping, sources[mapping["source_document_id"]]) == mapping["section_uuid"]
            assert store_table_section_mapping(conn, mapping, sources[mapping["source_document_id"]]) == mapping["section_uuid"]
            for field in ("table_id", "report_id", "section_uuid", "filing_id", "source_document_id", "source/document_set"):
                bad = deepcopy(mapping)
                bad[field] = str(UUID(int=999))
                with pytest.raises(db.DatabaseError), conn.transaction():
                    store_table_section_mapping(conn, bad, sources[mapping["source_document_id"]])
    with psycopg.connect(**options, autocommit=True) as conn:
        with pytest.raises(db.DatabaseError, match="caller-owned"):
            store_table_section_mapping(conn, mapping, sources[mapping["source_document_id"]])


def test_new_report_mapping_and_late_failure_roll_back_with_outer_transaction(sample):
    options, snapshot, sources = sample
    table, original = snapshot["tables"][0], snapshot["reports"][0]
    with psycopg.connect(**options) as conn:
        before = conn.execute("SELECT count(*) FROM sec.financial_table_sections").fetchone()[0]
        with conn.transaction(force_rollback=True):
            report_id = db.store_report(conn, db.ReportInput(filing_id=UUID(original["filing_id"]),
                source_document_id=UUID(original["source_document_id"]), extraction_version="report-local-rollback-v1",
                full_text=original["full_text"], sections=original["sections"]))
            from psycopg.rows import dict_row
            with conn.cursor(row_factory=dict_row) as cursor:
                report = cursor.execute("SELECT * FROM sec.reports WHERE id=%s", (report_id,)).fetchone()
                sections = cursor.execute("SELECT * FROM sec.report_sections WHERE report_id=%s", (report_id,)).fetchall()
            mapping = map_table_section(table, report, sections, sources[table["source_document_id"]], accession=snapshot["accession"])
            store_table_section_mapping(conn, mapping, sources[table["source_document_id"]])
            assert conn.execute("SELECT count(*) FROM sec.financial_table_sections").fetchone()[0] == before + 1
            with pytest.raises(psycopg.errors.DivisionByZero), conn.transaction():
                conn.execute("SELECT 1/0")
        assert conn.execute("SELECT count(*) FROM sec.financial_table_sections").fetchone()[0] == before
        assert conn.execute("SELECT 1 FROM sec.reports WHERE id=%s", (report_id,)).fetchone() is None
