"""Offline tests of explicit processing selection contracts."""
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import unittest
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from sec_pipeline import db_cli
from sec_pipeline.catalog import (
    ProcessingSetInput, prefer_unselected_processing_set, select_preferred_processing_set, store_processing_set,
    validated_processing_set_candidates,
)
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
        with self.assertRaises(DatabaseError):
            prefer_unselected_processing_set(conn, uuid4(), uuid4())
        conn.execute.assert_not_called()


class ValidatedSelectionTests(unittest.TestCase):
    def test_candidates_follow_the_given_key_priority_and_skip_preferred_filings(self):
        filing, selection = uuid4(), uuid4()
        conn = Mock(autocommit=False)
        conn.execute.return_value.fetchall.return_value = [(filing, selection, "filing-import-v3-text")]
        keys = ["filing-import-v3", "filing-import-v3-text"]
        self.assertEqual(validated_processing_set_candidates(conn, keys), [(filing, selection, "filing-import-v3-text")])
        statement, parameters = conn.execute.call_args.args
        self.assertEqual(parameters, (keys, keys))
        self.assertIn("validation_status = 'passed'", statement)
        self.assertIn("NOT EXISTS (SELECT 1 FROM sec.preferred_processing_sets", statement)
        self.assertIn("array_position(%s::text[], s.set_key)", statement)
        for keys in ([], ["filing-import-v3", "filing-import-v3"], [""], "filing-import-v3", [None]):
            with self.subTest(keys=keys), self.assertRaises(DatabaseError):
                validated_processing_set_candidates(conn, keys)

    def test_unselected_filings_are_preferred_without_replacing_a_choice(self):
        conn = Mock(autocommit=False)
        conn.execute.return_value.fetchone.side_effect = [None, ("filing",), None]
        results = [prefer_unselected_processing_set(conn, uuid4(), uuid4()) for _ in range(3)]
        self.assertEqual(results, [False, True, False])
        insert = conn.execute.call_args_list[-1].args[0]
        self.assertIn("ON CONFLICT (filing_id) DO NOTHING", insert)
        conn.commit.assert_not_called()

    def test_cli_selects_each_filing_in_its_own_transaction(self):
        rows = [(uuid4(), uuid4(), "filing-import-v3"), (uuid4(), uuid4(), "filing-import-v3-text"),
                (uuid4(), uuid4(), "filing-import-v3-text")]
        conn = MagicMock(autocommit=False)
        conn.__enter__.return_value = conn
        with patch.object(db_cli, "connect", return_value=conn), \
                patch("sec_pipeline.catalog.validated_processing_set_candidates", return_value=rows) as candidates, \
                patch("sec_pipeline.catalog.prefer_unselected_processing_set", side_effect=[True, True, False]) as prefer, \
                redirect_stdout(io.StringIO()) as output:
            code = db_cli.main(["--env-file", "fixture.env", "prefer-validated",
                                "--set-key", "filing-import-v3", "--set-key", "filing-import-v3-text"])
        self.assertEqual(code, 0)
        self.assertEqual(candidates.call_args.args[1], ["filing-import-v3", "filing-import-v3-text"])
        self.assertEqual(prefer.call_count, 3)
        self.assertEqual(conn.transaction.call_count, 3)
        self.assertEqual(json.loads(output.getvalue()), {
            "candidates": {"filing-import-v3": 1, "filing-import-v3-text": 2},
            "selected": {"filing-import-v3": 1, "filing-import-v3-text": 1}, "dry_run": False})

    def test_cli_dry_run_writes_nothing(self):
        conn = MagicMock(autocommit=False)
        conn.__enter__.return_value = conn
        with patch.object(db_cli, "connect", return_value=conn), \
                patch("sec_pipeline.catalog.validated_processing_set_candidates",
                      return_value=[(uuid4(), uuid4(), "filing-import-v3")]), \
                patch("sec_pipeline.catalog.prefer_unselected_processing_set") as prefer, \
                redirect_stdout(io.StringIO()) as output:
            code = db_cli.main(["prefer-validated", "--set-key", "filing-import-v3", "--dry-run"])
        self.assertEqual(code, 0)
        prefer.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["candidates"], {"filing-import-v3": 1})


if __name__ == "__main__":
    unittest.main()
