"""Offline tests of explicit processing selection contracts."""
from dataclasses import replace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from sec_pipeline.catalog import ProcessingSetInput, select_preferred_processing_set, store_processing_set
from sec_pipeline.database import DataConflict, DatabaseError


def record(**changes):
    data = dict(
        filing_id=uuid4(), set_key="reviewed-excerpt-v1", report_id=uuid4(),
        coverage={"facts": "absent", "tables": "absent", "reports": "partial", "chunks": "absent"},
        validation_status="passed", validation={"checks": {"source_match": True}},
        configuration={"description": "Selected excerpt; not a complete filing"},
    )
    data.update(changes)
    return ProcessingSetInput(**data)


class ProcessingSetTests(unittest.TestCase):
    def test_coverage_and_validation_are_independent_and_explicit(self):
        partial = record()
        self.assertEqual(partial.coverage["reports"], "partial")
        self.assertEqual(partial.validation_status, "passed")
        for changes in (
            {"coverage": {"reports": "complete"}},
            {"coverage": {"facts": "absent", "tables": "absent", "reports": "absent", "chunks": "absent"}},
            {"coverage": {"facts": "absent", "tables": "absent", "reports": [], "chunks": "absent"}},
            {"validation_status": "automatic"},
            {"validation": {}},
            {"validation": {"checks": {}}},
            {"validation": {"checks": {"quality": None}}},
            {"validation": {"checks": {"quality": 1}}},
            {"validation": {"checks": {"": True}}},
        ):
            with self.subTest(changes=changes), self.assertRaises(DatabaseError):
                record(**changes)
        self.assertEqual(record(validation_status="unvalidated", validation={}).validation_status, "unvalidated")

    def test_stage_selectors_do_not_infer_sources_versions_or_absence(self):
        for changes in (
            {"fact_source_document_id": uuid4()},
            {"table_extraction_version": "tables-v1"},
            {"chunking_version": "chunks-v1", "report_id": None},
            {"report_id": None},
            {"chunking_version": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(DatabaseError):
                record(**changes)
        both = record(fact_source_document_id=uuid4(), fact_extraction_version="facts-v8",
                      table_source_document_id=uuid4(), table_extraction_version="tables-v2",
                      coverage={"facts": "complete", "tables": "partial", "reports": "partial", "chunks": "absent"})
        self.assertNotEqual(both.fact_source_document_id, both.table_source_document_id)
        self.assertNotEqual(both.fact_extraction_version, both.table_extraction_version)

    def test_identical_replay_returns_existing_id_and_changed_identity_conflicts(self):
        existing_id = uuid4()
        conn = Mock(autocommit=False)
        conn.execute.side_effect = [Mock(), Mock(fetchone=Mock(return_value=(existing_id,))),
                                    Mock(fetchone=Mock(return_value=(existing_id,)))]
        self.assertEqual(store_processing_set(conn, record()), existing_id)
        conn.commit.assert_not_called()
        conn.execute.side_effect = [Mock(), Mock(fetchone=Mock(return_value=(existing_id,))),
                                    Mock(fetchone=Mock(return_value=None))]
        with self.assertRaises(DataConflict):
            store_processing_set(conn, record())
        conn.commit.assert_not_called()

    def test_store_revalidates_mutated_dictionary_before_querying(self):
        selection = record()
        selection.validation["checks"]["source_match"] = False
        conn = Mock(autocommit=False)
        with self.assertRaises(DatabaseError):
            store_processing_set(conn, selection)
        conn.execute.assert_not_called()

    def test_helpers_do_not_allow_autocommit(self):
        conn = Mock(autocommit=True)
        with self.assertRaises(DatabaseError):
            store_processing_set(conn, record())
        with self.assertRaises(DatabaseError):
            select_preferred_processing_set(conn, uuid4(), uuid4())
        conn.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
