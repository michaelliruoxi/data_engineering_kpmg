"""Source-only observations and hidden-nil storage contracts, without live SQL by default."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
import pytest

from sec_pipeline import database as db
from sec_pipeline.catalog import ProcessingSetInput, store_processing_set
from sec_pipeline.source_cells_v3 import POLICY, source_cell_counts, transform_tables
from sec_pipeline.table_cells import store_table_cells
from sec_pipeline.tables import FactScope, load_tables
from tests.test_source_cells_v3 import fixture


ROOT = Path(__file__).resolve().parents[1]


class StorageRecorder:
    autocommit = False

    def __init__(self):
        self.cells = {}
        self.queries = []

    @contextmanager
    def transaction(self):
        yield

    def execute(self, query, args):
        self.queries.append((query, args))
        return SimpleNamespace(fetchall=lambda: list(self.cells.items()))

    def commit(self):
        raise AssertionError("Caller owns commit.")

    def rollback(self):
        raise AssertionError("Caller owns rollback.")


def test_loader_stores_one_observation_and_no_fact_link_for_a_source_only_cell():
    original, facts, source = fixture()
    [table] = transform_tables([original], source, facts)
    conn, calls = StorageRecorder(), []

    def insert(connection, target, values, keys, **kwargs):
        assert connection is conn
        calls.append((target, deepcopy(values), keys))
        identifier = UUID(int=100 + len(calls))
        if target == "table_cells":
            conn.cells[values["cell_key"]] = identifier
        return identifier

    with patch("sec_pipeline.table_cells._insert_exact", side_effect=insert):
        result = store_table_cells(conn, UUID(int=5), table, {})
    observations = [values for target, values, _ in calls if target == "cell_source_observations"]
    assert len(observations) == 1
    assert observations[0]["cell_id"] == result["r000c001"]
    assert observations[0]["source_document_id"] == table.source_document_id
    assert observations[0]["source_sha256"] == table.structure["html_sha256"]
    assert observations[0]["evidence"] == table.structure["rows"][0][1]["source_only_evidence"]
    assert not any(target == "cell_fact_links" for target, _, _ in calls)
    assert any("SET relational_cells_version = 2" in query for query, _ in conn.queries)


def test_migration_gates_v3_rows_and_preserves_history_view_columns():
    migration = (ROOT / "src/sec_pipeline/migrations/007_source_cell_representations.sql").read_text(encoding="utf-8")
    assert "Every selected V3 table must retain schema-v2 relational cells" in migration
    assert "t.relational_cells_version IS DISTINCT FROM 2" in migration
    assert "t.structure->>'schema_version' IS DISTINCT FROM '2'" in migration
    assert "NEW.configuration->'source_cell_counts' IS DISTINCT FROM source_counts" in migration
    assert "NEW.display_text NOT IN ('-', '–', '—')" in migration
    assert "CREATE OR REPLACE VIEW sec.issuer_filing_history" in migration
    assert "selection.table_extraction_version IN ('tables-v2', 'tables-v3')" in migration
    assert "original_issuer.cik AS original_cik" in migration
    assert "DISTINCT ON (current_company_id, filing_id)" in migration
    assert "selection.validation->'checks'->'index_registrants_verified'='true'::jsonb" in migration


@pytest.fixture
def local_connection():
    if os.environ.get("SEC_DB_TEST") != "1":
        pytest.skip("Dedicated local PostgreSQL checks are opt-in.")
    from tests.test_catalog_postgres import local_test_options

    options = local_test_options()
    conn = psycopg.connect(**options)
    try:
        with conn.transaction(force_rollback=True):
            if conn.execute("SELECT to_regclass('sec.cell_source_observations')").fetchone()[0] is None:
                raise RuntimeError("Migrate the dedicated local test database through migration 007 first.")
            yield conn
    finally:
        conn.close()


def database_fixture(conn, *, hidden=False, load=True):
    original, _, source = fixture(binding="-sec-ix-hidden:F1" if hidden else None)
    company = db.register_company(conn, db.CompanyInput(cik="0000000911", name="Source marker fixture"))
    other = db.register_company(conn, db.CompanyInput(cik="0000000912", name="Co-registrant fixture"))
    index_url = "https://example.invalid/marker/index.html"
    filing = db.register_filing(conn, db.FilingInput(company_id=company,
        accession_number="0000000911-26-000001", form="10-Q", filing_date=date(2026, 8, 1),
        source_url=index_url, company_name_as_filed="Source marker fixture"))
    sources = {}
    for role, filename, digest in (("main_inline_xbrl_filing", "source.htm", original.structure["html_sha256"]),
                                  ("extracted_xbrl_instance", "source.xml", "b" * 64),
                                  ("filing_index", "index.html", "c" * 64)):
        sources[role] = db.register_source_document(conn, db.SourceDocumentInput(
            filing_id=filing, role=role, source_url=index_url if role == "filing_index" else "https://example.invalid/marker/" + filename,
            local_path="marker-fixture/" + filename, sha256=digest, size_bytes=len(source),
            retrieved_at=datetime(2026, 8, 2, tzinfo=timezone.utc)))
    original, facts, source = fixture(binding="-sec-ix-hidden:F1" if hidden else None, filing=filing,
        html_id=sources["main_inline_xbrl_filing"], xml_id=sources["extracted_xbrl_instance"])
    [table] = transform_tables([original], source, facts)
    fact_ids = {fact.occurrence_key: db.store_fact(conn, fact) for fact in facts}
    scope = FactScope(filing, sources["extracted_xbrl_instance"], "facts-v1", fact_ids)
    table_id = load_tables(conn, [table], scope)[table.table_key] if load else db.store_financial_table(conn, table)
    return SimpleNamespace(table=table, facts=facts, scope=scope, table_id=table_id,
                           company=company, other_company=other, index_url=index_url)


def selection(item, *, counts=None):
    return ProcessingSetInput(
        filing_id=item.table.filing_id, set_key="source-marker-fixture-v3",
        fact_source_document_id=item.scope.xml_document_id, fact_extraction_version="facts-v1",
        table_source_document_id=item.table.source_document_id, table_extraction_version="tables-v3",
        coverage={"facts": "complete", "tables": "partial", "reports": "absent", "chunks": "absent"},
        validation_status="passed", validation={"checks": {
            "source_only_markers_verified": True, "hidden_nil_bindings_verified": True,
            "financial_cell_inventory_complete": True, "index_registrants_verified": True}},
        configuration={"source_cell_counts": source_cell_counts([item.table]) if counts is None else counts,
                       "index_registrants": [{"cik": "0000000911", "is_canonical": True},
                                             {"cik": "0000000912", "is_canonical": False}]},
    )


def test_postgres_source_only_observation_replay_counts_and_seal(local_connection):
    conn = local_connection
    item = database_fixture(conn)
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    conn.execute("SET CONSTRAINTS ALL DEFERRED")
    rows = conn.execute("SELECT id,cell_id,evidence FROM sec.cell_source_observations WHERE table_id=%s", (item.table_id,)).fetchall()
    assert len(rows) == 1 and rows[0][2]["literal"] == "—"
    assert conn.execute("SELECT count(*) FROM sec.cell_fact_links WHERE table_id=%s", (item.table_id,)).fetchone()[0] == 0
    set_id = store_processing_set(conn, selection(item))
    counts = conn.execute("SELECT counts FROM sec.filing_processing_sets WHERE id=%s", (set_id,)).fetchone()[0]
    assert counts["source_only_marker_cells"] == 1 and counts["hidden_nil_cells"] == 0
    assert load_tables(conn, [item.table], item.scope) == {item.table.table_key: item.table_id}
    assert conn.execute("SELECT id,cell_id,evidence FROM sec.cell_source_observations WHERE table_id=%s", (item.table_id,)).fetchall() == rows
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("DELETE FROM sec.cell_source_observations WHERE table_id=%s", (item.table_id,))


def test_postgres_missing_observation_or_link_on_source_only_cell_cannot_pass(local_connection):
    conn = local_connection
    item = database_fixture(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("DELETE FROM sec.cell_source_observations WHERE table_id=%s", (item.table_id,))
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    cell_id = conn.execute("SELECT id FROM sec.table_cells WHERE table_id=%s AND resolution_status='source_only'", (item.table_id,)).fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("""INSERT INTO sec.cell_fact_links(filing_id,table_id,cell_id,fact_id,reference_order,link_method,evidence)
                VALUES (%s,%s,%s,%s,0,'hidden_inline_id',%s)""",
                (item.table.filing_id, item.table_id, cell_id, item.scope.xml_id_to_fact_id["F1"], Jsonb({})))


def test_postgres_hidden_nil_links_existing_fact_without_zero_or_observation(local_connection):
    conn = local_connection
    item = database_fixture(conn, hidden=True)
    row = conn.execute("""SELECT l.link_method,f.is_nil,f.numeric_value FROM sec.cell_fact_links l
        JOIN sec.financial_facts f ON f.id=l.fact_id WHERE l.table_id=%s""", (item.table_id,)).fetchone()
    assert row == ("hidden_inline_id", True, None)
    assert conn.execute("SELECT count(*) FROM sec.cell_source_observations WHERE table_id=%s", (item.table_id,)).fetchone()[0] == 0
    store_processing_set(conn, selection(item))


def test_postgres_v3_processing_set_rejects_unmanaged_tables_even_with_zero_counts(local_connection):
    conn = local_connection
    item = database_fixture(conn, load=False)
    zero_counts = {key: 0 for key in source_cell_counts([item.table])}
    with pytest.raises(psycopg.errors.CheckViolation, match="schema-v2 relational cells"):
        with conn.transaction():
            store_processing_set(conn, selection(item, counts=zero_counts))


def test_postgres_v3_history_retains_original_filing_and_verified_coregistrant(local_connection):
    conn = local_connection
    item = database_fixture(conn)
    store_processing_set(conn, selection(item))
    rows = conn.execute("""SELECT current_cik,original_cik,filing_id,relationship
        FROM sec.issuer_filing_history WHERE filing_id=%s ORDER BY current_cik""", (item.table.filing_id,)).fetchall()
    assert rows == [("0000000911", "0000000911", item.table.filing_id, "direct"),
                    ("0000000912", "0000000911", item.table.filing_id, "co_registrant")]
