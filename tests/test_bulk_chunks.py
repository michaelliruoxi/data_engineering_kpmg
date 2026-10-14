"""Batched section lookup preserves chunk scope, boundaries, and transaction ownership."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from sec_pipeline import chunking
from sec_pipeline.database import ChunkInput, DatabaseError


FILING, REPORT, SECTION = UUID(int=1), UUID(int=2), UUID(int=3)


def chunk(index=0, **changes):
    return replace(ChunkInput(
        filing_id=FILING, report_id=REPORT, chunking_version="paragraph-v1",
        chunk_index=index, text="Revenue", offset_start=10, offset_end=17,
        section="financial-statements", metadata={"section_id": "financial-statements"},
    ), **changes)


class Connection:
    autocommit = False

    def __init__(self, sections=None):
        self.sections = sections or {}
        self.queries = []

    def execute(self, statement, parameters):
        self.queries.append((statement, parameters))
        assert statement.lstrip().startswith("SELECT")
        return SimpleNamespace(fetchall=lambda: self.sections.get(parameters, []))

    def commit(self):
        raise AssertionError("Caller owns commit.")

    def rollback(self):
        raise AssertionError("Caller owns rollback.")

    def transaction(self):
        raise AssertionError("Caller owns transactions and savepoints.")


def test_default_keeps_existing_per_record_storage_contract():
    records = [chunk(1), chunk(0)]
    ids = [UUID(int=30), UUID(int=31)]
    connection = Connection()
    with patch.object(chunking, "store_chunk", side_effect=ids) as store:
        assert chunking.store_chunks(connection, records) == ids
    assert [call.args for call in store.call_args_list] == [(connection, record) for record in records]
    assert connection.queries == []


def test_bulk_resolves_thousand_chunks_with_one_shared_section_lookup_and_ordered_ids():
    records = [chunk(index) for index in range(1000)]
    ids = [UUID(int=index + 100) for index in range(1000)]
    connection = Connection({(REPORT, FILING): [(SECTION, "financial-statements", 0, 100)]})
    with patch("sec_pipeline.bulk_storage.store_many", return_value=ids) as store, \
            patch.object(chunking, "store_chunk", side_effect=AssertionError("Per-chunk storage was used")):
        assert chunking.store_chunks(connection, records, bulk=True) == ids
    assert len(connection.queries) == 1
    statement, parameters = connection.queries[0]
    assert "WHERE report_id = %s AND filing_id = %s FOR SHARE" in statement
    assert parameters == (REPORT, FILING)
    passed_connection, table, resolved, keys = store.call_args.args
    assert passed_connection is connection and table == "chunks"
    assert keys == ("report_id", "chunking_version", "chunk_index")
    assert resolved == [replace(record, section_id=SECTION) for record in records]
    assert all(record.section_id is None for record in records)


def test_bulk_mixed_scopes_resolve_same_key_only_within_exact_report_and_filing():
    other_report, other_filing, other_section = UUID(int=20), UUID(int=21), UUID(int=22)
    records = [chunk(1), chunk(0, report_id=other_report, filing_id=other_filing), chunk(0)]
    connection = Connection({
        (REPORT, FILING): [(SECTION, "financial-statements", 0, 100)],
        (other_report, other_filing): [(other_section, "financial-statements", 0, 100)],
    })
    ids = [UUID(int=30), UUID(int=31), UUID(int=32)]
    with patch("sec_pipeline.bulk_storage.store_many", return_value=ids) as store:
        assert chunking.store_chunks(connection, records, bulk=True) == ids
    assert [parameters for _, parameters in connection.queries] == [(REPORT, FILING), (other_report, other_filing)]
    assert [record.section_id for record in store.call_args.args[2]] == [SECTION, other_section, SECTION]
    assert [record.chunk_index for record in store.call_args.args[2]] == [1, 0, 0]


@pytest.mark.parametrize("change", [{"report_id": UUID(int=99)}, {"filing_id": UUID(int=99)}])
def test_wrong_scope_cannot_reuse_another_reports_section(change):
    record = chunk(**change)
    connection = Connection({(REPORT, FILING): [(SECTION, "financial-statements", 0, 100)]})
    with patch("sec_pipeline.bulk_storage.store_many", return_value=[UUID(int=30)]) as store:
        chunking.store_chunks(connection, [record], bulk=True)
    assert connection.queries[0][1] == (record.report_id, record.filing_id)
    assert store.call_args.args[2] == [record]
    assert store.call_args.args[2][0].section_id is None


def test_conflicting_filing_ids_for_one_report_fail_before_reads_or_writes():
    connection = Connection()
    with patch("sec_pipeline.bulk_storage.store_many") as store:
        with pytest.raises(DatabaseError, match="multiple filing scopes"):
            chunking.store_chunks(connection, [chunk(), chunk(1, filing_id=UUID(int=99))], bulk=True)
    assert connection.queries == []
    store.assert_not_called()


def test_explicit_section_id_is_preserved_for_database_scope_and_bounds_constraints():
    explicit = UUID(int=99)
    record = chunk(section_id=explicit, offset_start=0, offset_end=1000)
    connection = Connection({(REPORT, FILING): [(SECTION, "financial-statements", 10, 20)]})
    with patch("sec_pipeline.bulk_storage.store_many", return_value=[UUID(int=30)]) as store:
        chunking.store_chunks(connection, [record], bulk=True)
    assert connection.queries == []
    assert store.call_args.args[2] == [record]
    assert store.call_args.args[2][0].section_id == explicit


def test_metadata_key_fallback_and_unresolved_keys_match_single_record_behavior():
    records = [
        chunk(0, section=None),
        chunk(1, section="unknown"),
        chunk(2, section=None, metadata={}),
        chunk(3, section=None, metadata={"section_id": 123}),
        chunk(4, section="financial-statements", metadata={"section_id": "ignored"}),
    ]
    connection = Connection({(REPORT, FILING): [(SECTION, "financial-statements", 0, 100)]})
    with patch("sec_pipeline.bulk_storage.store_many", return_value=[]) as store:
        chunking.store_chunks(connection, records, bulk=True)
    assert len(connection.queries) == 1
    assert [record.section_id for record in store.call_args.args[2]] == [SECTION, None, None, None, SECTION]


@pytest.mark.parametrize("bounds", [(9, 17), (10, 21)])
def test_any_resolved_boundary_failure_prevents_every_chunk_write(bounds):
    connection = Connection({(REPORT, FILING): [(SECTION, "financial-statements", 10, 20)]})
    records = [chunk(0), chunk(1, offset_start=bounds[0], offset_end=bounds[1])]
    with patch("sec_pipeline.bulk_storage.store_many") as store:
        with pytest.raises(DatabaseError, match="crosses its report section boundary"):
            chunking.store_chunks(connection, records, bulk=True)
    store.assert_not_called()


def test_exact_section_bounds_are_accepted():
    connection = Connection({(REPORT, FILING): [(SECTION, "financial-statements", 10, 17)]})
    with patch("sec_pipeline.bulk_storage.store_many", return_value=[UUID(int=30)]) as store:
        chunking.store_chunks(connection, [chunk()], bulk=True)
    assert store.call_args.args[2][0].section_id == SECTION


@pytest.mark.parametrize("records", [None, "wrong", {}, [chunk(), object()]])
def test_malformed_inputs_fail_before_section_queries_or_bulk_writes(records):
    connection = Connection()
    with patch("sec_pipeline.bulk_storage.store_many") as store:
        with pytest.raises(DatabaseError, match="ChunkInput"):
            chunking.store_chunks(connection, records, bulk=True)
    assert connection.queries == []
    store.assert_not_called()


def test_empty_bulk_input_has_no_database_operations():
    connection = Connection()
    with patch("sec_pipeline.bulk_storage.store_many") as store:
        assert chunking.store_chunks(connection, [], bulk=True) == []
    assert connection.queries == []
    store.assert_not_called()


def test_bulk_autocommit_is_rejected_before_sql():
    connection = Connection()
    connection.autocommit = True
    with pytest.raises(DatabaseError, match="autocommit=False"):
        chunking.store_chunks(connection, [chunk()], bulk=True)
    assert connection.queries == []


def test_bulk_error_propagates_without_recovery_or_transaction_ownership():
    connection = Connection()
    failure = RuntimeError("fixture database failure")
    with patch("sec_pipeline.bulk_storage.store_many", side_effect=failure):
        with pytest.raises(RuntimeError) as caught:
            chunking.store_chunks(connection, [chunk(section_id=SECTION)], bulk=True)
    assert caught.value is failure
