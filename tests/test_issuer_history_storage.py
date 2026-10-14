"""Issuer succession identities, immutable evidence, and additive SQL contracts."""

from copy import deepcopy
from datetime import date, datetime, timezone
import ipaddress
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import psycopg
import pytest

from sec_pipeline import database
from sec_pipeline.issuer_history_storage import store_issuer_history


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "src/sec_pipeline/migrations/006_issuer_successions.sql"


def record(**changes):
    return dict(
        current_cik="0002115436", predecessor_cik="0000034088",
        current_name="ExxonMobil Holdings Corporation", predecessor_name="Exxon Mobil Corporation",
        effective_date="2026-07-01",
        evidence_url="https://www.sec.gov/Archives/edgar/data/2115436/000119312526291990/d71068d8k12b.htm",
        evidence_sha256="a" * 64, evidence_local_path="data/evidence/issuer-history/successor.htm",
        evidence_retrieved_at="2026-10-13T12:00:00Z",
    ) | changes


class Recorder:
    autocommit = False

    def __init__(self):
        self.companies = {}
        self.edges = {}
        self.calls = []

    def execute(self, statement, parameters):
        query = " ".join(statement.split())
        values = tuple(parameters)
        self.calls.append((query, values))
        if query.startswith("INSERT INTO sec.companies"):
            cik, name, ticker = values
            if cik in self.companies:
                result = None
            else:
                identifier = UUID(int=100 + len(self.companies))
                self.companies[cik] = {"id": identifier, "name": name, "ticker": ticker}
                result = (identifier,)
        elif query.startswith("SELECT id FROM sec.companies"):
            result = (self.companies[values[0]]["id"],)
        elif query.startswith("INSERT INTO sec.issuer_successions"):
            pair = values[:2]
            if pair in self.edges:
                result = None
            else:
                identifier = UUID(int=1000 + len(self.edges))
                self.edges[pair] = {"id": identifier, "values": values}
                result = (identifier,)
        elif query.startswith("SELECT id FROM sec.issuer_successions"):
            existing = self.edges.get(values[:2])
            result = (existing["id"],) if existing is not None and existing["values"][:5] == values else None
        else:
            raise AssertionError("Unexpected SQL or an attempted overwrite of original data.")
        return SimpleNamespace(fetchone=lambda: result)

    def commit(self):
        raise AssertionError("The caller owns commit.")

    def rollback(self):
        raise AssertionError("The caller owns rollback.")

    def transaction(self):
        raise AssertionError("The caller owns transactions and savepoints.")


def test_new_edge_registers_two_stable_companies_and_normalizes_receipt_to_utc():
    conn = Recorder()
    incoming = record(evidence_retrieved_at="2026-10-13T08:00:00-04:00")
    [identifier] = store_issuer_history(conn, [incoming])
    assert identifier == UUID(int=1000)
    edge = next(iter(conn.edges.values()))["values"]
    assert edge[:2] == (conn.companies[incoming["current_cik"]]["id"], conn.companies[incoming["predecessor_cik"]]["id"])
    assert edge[2] == date(2026, 7, 1)
    assert edge[-1] == datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    assert edge[-1].tzinfo is timezone.utc
    assert not any(query.startswith(("UPDATE", "DELETE")) for query, _ in conn.calls)


def test_identical_evidence_replay_preserves_original_profiles_path_and_receipt():
    conn = Recorder()
    original = record()
    first = store_issuer_history(conn, [original])
    conn.companies[original["current_cik"]].update(name="Existing current display profile", ticker="XOM")
    before_companies, before_edges = deepcopy(conn.companies), deepcopy(conn.edges)
    replay = record(current_name="Another display spelling", predecessor_name="Another prior spelling",
                    evidence_local_path="relocated/evidence.htm", evidence_retrieved_at="2026-10-14T12:00:00Z")
    assert store_issuer_history(conn, [replay]) == first
    assert conn.companies == before_companies
    assert conn.edges == before_edges
    assert "FOR SHARE" in conn.calls[-1][0]


def test_optional_successor_relationship_from_archived_evidence_is_accepted():
    conn = Recorder()
    initial = store_issuer_history(conn, [record()])
    assert store_issuer_history(conn, [record() | {"relationship": "successor_issuer"}]) == initial
    assert len(conn.edges) == 1


@pytest.mark.parametrize("relationship", [None, "predecessor", "acquisition", "", "SUCCESSOR_ISSUER"])
def test_other_relationship_values_are_rejected_before_sql(relationship):
    conn = Recorder()
    with pytest.raises(database.DatabaseError, match="successor_issuer"):
        store_issuer_history(conn, [record() | {"relationship": relationship}])
    assert conn.calls == []


@pytest.mark.parametrize("changes", [
    {"effective_date": "2026-07-02"},
    {"evidence_url": "https://www.sec.gov/Archives/edgar/data/2115436/000119312526291990/other.htm"},
    {"evidence_sha256": "b" * 64},
])
def test_changed_immutable_payload_conflicts_without_replacing_the_existing_edge(changes):
    conn = Recorder()
    store_issuer_history(conn, [record()])
    before = deepcopy(conn.edges)
    with pytest.raises(database.DataConflict, match="preserve the original relationship"):
        store_issuer_history(conn, [record(**changes)])
    assert conn.edges == before


def test_conflicting_duplicate_pair_is_rejected_before_any_registration():
    conn = Recorder()
    with pytest.raises(database.DataConflict, match="one issuer pair"):
        store_issuer_history(conn, [record(), record(effective_date="2026-07-02")])
    assert conn.calls == []


def test_identical_duplicates_reuse_ids_and_different_pairs_preserve_input_order():
    conn = Recorder()
    other = record(
        current_cik="0001996862", predecessor_cik="0001144519", current_name="Bunge Global SA",
        predecessor_name="Bunge Limited", effective_date=date(2023, 11, 1),
        evidence_url="https://www.sec.gov/Archives/edgar/data/1144519/000110465923113122/tm2329110d1_8k.htm",
    )
    ids = store_issuer_history(conn, (item for item in [other, record(), other]))
    assert ids == [UUID(int=1000), UUID(int=1001), UUID(int=1000)]
    assert len(conn.companies) == 4 and len(conn.edges) == 2
    assert sum(query.startswith("INSERT INTO sec.companies") for query, _ in conn.calls) == 4


@pytest.mark.parametrize("changes", [
    {"current_cik": "2115436"}, {"predecessor_cik": "0000000000"}, {"current_cik": "0000034088"},
    {"current_name": " "}, {"predecessor_name": None}, {"effective_date": "2026-02-30"},
    {"effective_date": datetime(2026, 7, 1, tzinfo=timezone.utc)},
    {"evidence_sha256": "z" * 64}, {"evidence_sha256": "a" * 63},
    {"evidence_retrieved_at": "2026-10-13T12:00:00"},
    {"evidence_retrieved_at": "not a date"},
])
def test_invalid_fields_anywhere_in_batch_fail_before_sql(changes):
    conn = Recorder()
    with pytest.raises(database.DatabaseError):
        store_issuer_history(conn, [record(), record(**changes)])
    assert conn.calls == []


@pytest.mark.parametrize("url", [
    "http://www.sec.gov/Archives/edgar/data/2115436/000119312526291990/evidence.htm",
    "https://www.sec.gov.evil.invalid/Archives/edgar/data/2115436/000119312526291990/evidence.htm",
    "https://www.sec.gov@evil.invalid/Archives/edgar/data/2115436/000119312526291990/evidence.htm",
    "https://www.sec.gov/Archives/edgar/data/9999999/000119312526291990/evidence.htm",
    "https://www.sec.gov/Archives/edgar/data/2115436/000119312526291990/evidence.htm?download=1",
    "https://www.sec.gov/Archives/edgar/data/2115436/000119312526291990/../evidence.htm",
])
def test_evidence_urls_must_bind_to_one_of_the_linked_sec_issuers(url):
    conn = Recorder()
    with pytest.raises(database.DatabaseError, match="SEC archive URL"):
        store_issuer_history(conn, [record(evidence_url=url)])
    assert conn.calls == []


@pytest.mark.parametrize("path", ["", "../evidence.htm", "data/../evidence.htm", "./evidence.htm",
                                  "/outside/evidence.htm", "C:\\outside\\evidence.htm",
                                  "C:relative.htm", "\\\\server\\evidence.htm", "data/\nfile.htm", "data/\x7ffile.htm"])
def test_evidence_locator_rejects_absolute_and_traversing_paths(path):
    conn = Recorder()
    with pytest.raises(database.DatabaseError, match="relative local file path"):
        store_issuer_history(conn, [record(evidence_local_path=path)])
    assert conn.calls == []


def test_relative_windows_locator_is_supported_without_rewriting_first_receipt():
    conn = Recorder()
    value = "data\\evidence\\issuer-history\\successor.htm"
    store_issuer_history(conn, [record(evidence_local_path=value)])
    assert next(iter(conn.edges.values()))["values"][-2] == value


@pytest.mark.parametrize("records", [None, "wrong", {}, [None], [record() | {"unexpected": "value"}]])
def test_record_container_and_shape_are_checked_before_sql(records):
    conn = Recorder()
    with pytest.raises(database.DatabaseError):
        store_issuer_history(conn, records)
    assert conn.calls == []


def test_empty_input_has_no_sql_and_autocommit_is_rejected():
    conn = Recorder()
    assert store_issuer_history(conn, []) == []
    assert conn.calls == []
    conn.autocommit = True
    with pytest.raises(database.DatabaseError, match="autocommit=False"):
        store_issuer_history(conn, [record()])
    assert conn.calls == []


def test_sql_adds_history_without_reassigning_existing_filings_or_profiles():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "UNIQUE (successor_company_id, predecessor_company_id)" in sql
    assert "CHECK (successor_company_id <> predecessor_company_id)" in sql
    assert "successor_company_id uuid NOT NULL REFERENCES sec.companies(id)" in sql
    assert "predecessor_company_id uuid NOT NULL REFERENCES sec.companies(id)" in sql
    assert "lpad(archive_cik, 10, '0') NOT IN (successor_cik, predecessor_cik)" in sql
    assert "WHERE successor_company_id=OLD.id OR predecessor_company_id=OLD.id" in sql
    assert "BEFORE UPDATE OF cik ON sec.companies" in sql
    assert "UPDATE sec.filings" not in sql and "UPDATE sec.companies" not in sql
    assert "DELETE FROM" not in sql and "DROP " not in sql
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sec_reader')" in sql
    assert "GRANT SELECT ON sec.issuer_successions, sec.issuer_filing_history TO sec_reader" in sql


def test_history_view_retains_original_identity_date_boundary_and_verified_coregistrants():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "f.filing_date <= s.effective_date" in sql
    assert "DISTINCT ON (current_company_id, filing_id)" in sql
    assert "original_issuer.cik AS original_cik" in sql
    assert "f.company_name_as_filed" in sql
    assert "original_issuer.id=f.company_id" in sql
    assert "selection.table_extraction_version='tables-v2'" in sql
    assert "selection.validation_status='passed'" in sql
    assert "selection.validation->'checks'->'index_registrants_verified'='true'::jsonb" in sql
    assert "jsonb_typeof(selection.configuration->'index_registrants')='array'" in sql
    assert "current_issuer.cik=registrant.value->>'cik'" in sql
    assert "source.role='filing_index' AND source.source_url=f.source_url" in sql


@pytest.mark.skipif(os.environ.get("SEC_ISSUER_HISTORY_TEST") != "1",
                    reason="Dedicated, already-migrated local database test is opt-in.")
def test_local_postgres_receipt_replay_and_history_boundary():
    env_file = Path(os.environ.get("SEC_ISSUER_HISTORY_TEST_ENV", ROOT / ".env.local"))
    settings = database.connection_settings(env_file)
    target = os.environ.get("SEC_ISSUER_HISTORY_TEST_DATABASE", "sec_issuer_history_test")
    if (settings["host"] not in {"127.0.0.1", "localhost", "::1"}
            or not target.endswith("_test") or target == settings["dbname"]):
        raise RuntimeError("Use a separate, migrated local test database ending in _test.")
    settings.update(dbname=target, hostaddr="127.0.0.1" if settings["host"] == "localhost" else settings["host"])
    with psycopg.connect(**settings) as conn:
        if not ipaddress.ip_address(conn.info.hostaddr).is_loopback or conn.info.dbname != target:
            raise RuntimeError("Connected destination is not the requested local test database.")
        with conn.transaction(force_rollback=True):
            if conn.execute("SELECT count(*) FROM sec.companies").fetchone()[0]:
                raise RuntimeError("The issuer-history test database must be empty.")
            incoming = record()
            [identifier] = store_issuer_history(conn, [incoming])
            assert store_issuer_history(conn, [record(evidence_local_path="another/evidence.htm",
                                                      evidence_retrieved_at="2026-10-14T12:00:00Z")]) == [identifier]
            row = conn.execute("SELECT evidence_local_path,evidence_retrieved_at FROM sec.issuer_successions WHERE id=%s",
                               (identifier,)).fetchone()
            assert row == (incoming["evidence_local_path"], datetime(2026, 10, 13, 12, tzinfo=timezone.utc))
            ids = dict(conn.execute("SELECT cik,id FROM sec.companies").fetchall())
            for day, sequence in ((date(2026, 6, 30), 1), (date(2026, 7, 1), 2), (date(2026, 7, 2), 3)):
                database.register_filing(conn, database.FilingInput(
                    company_id=ids[incoming["predecessor_cik"]], accession_number=f"0000034088-26-{sequence:06d}",
                    form="10-Q", filing_date=day,
                ))
            rows = conn.execute("SELECT accession_number,original_cik,relationship FROM sec.issuer_filing_history WHERE current_cik=%s ORDER BY filing_date",
                                (incoming["current_cik"],)).fetchall()
            assert rows == [("0000034088-26-000001", incoming["predecessor_cik"], "predecessor"),
                            ("0000034088-26-000002", incoming["predecessor_cik"], "predecessor")]
