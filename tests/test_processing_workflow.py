"""Offline transaction-controller tests; these do not exercise PostgreSQL."""

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_pipeline.processing import (
    PipelineStages,
    ProcessingError,
    ValidationResult,
    run_ingestion_transaction,
)


class RecordingConnection:
    """Track use of the transaction context, without a database or credentials."""

    def __init__(self):
        self.closed = False
        self.autocommit = False
        self.info = SimpleNamespace(transaction_status=SimpleNamespace(name="IDLE"))
        self.events = []
        self.pending = []
        self.committed = []
        self.commits = self.rollbacks = 0

    @contextmanager
    def transaction(self):
        self.events.append("begin")
        self.info.transaction_status.name = "INTRANS"
        try:
            yield
        except BaseException:
            self.pending.clear()
            self.rollbacks += 1
            self.events.append("rollback")
            raise
        else:
            self.committed.extend(self.pending)
            self.pending.clear()
            self.commits += 1
            self.events.append("commit")
        finally:
            self.info.transaction_status.name = "IDLE"


class ProcessingWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_root = Path(temporary.name).resolve()
        self.manifest = json.loads((ROOT / "source_manifest.json").read_text())
        self.manifest_path = self.data_root / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest))
        records = [self.manifest["index_document"], *self.manifest["files"]]
        self.seed_result = {
            "filing_id": UUID(int=10),
            "document_ids": {
                record["local_path"]: UUID(int=i + 1)
                for i, record in enumerate(records)
            },
        }
        self.conn = RecordingConnection()
        self.versions = {
            "facts": "test-facts-v1", "tables": "test-tables-v1",
            "reports": "test-reports-v1", "chunks": "paragraph-v1",
        }
        self.required_checks = ("counts", "citations")
        self.failure = None
        self.validation = ValidationResult(
            dict(self.versions), {name: True for name in self.required_checks}
        )

        def writer(name):
            def stage(conn, context):
                self.assertIs(conn, self.conn)
                self.assertEqual(dict(context.versions), self.versions)
                conn.events.append(name)
                conn.pending.append(name)
                if self.failure == name:
                    raise RuntimeError("private adapter diagnostic")
                return f"{name}-result"
            return stage

        def validate(conn, context):
            self.assertIs(conn, self.conn)
            self.assertEqual(list(context.outputs), ["facts", "tables", "report", "chunks"])
            self.assertEqual(conn.committed, [])
            conn.events.append("validation")
            if self.failure == "validation":
                raise RuntimeError("private validator diagnostic")
            return self.validation

        self.stages = PipelineStages(
            facts=writer("facts"), tables=writer("tables"),
            report=writer("report"), chunks=writer("chunks"), validate=validate,
        )

    def seed_metadata(self, conn, manifest_path, data_root):
        self.assertIs(conn, self.conn)
        self.assertEqual(manifest_path, self.manifest_path)
        self.assertEqual(data_root, self.data_root)
        conn.events.append("metadata")
        conn.pending.append("metadata")
        if self.failure == "metadata":
            raise RuntimeError("private metadata diagnostic")
        return self.seed_result

    def run_pipeline(self, **overrides):
        arguments = dict(
            manifest=self.manifest, manifest_path=self.manifest_path,
            data_root=self.data_root, versions=self.versions, stages=self.stages,
            required_checks=self.required_checks, seed_metadata=self.seed_metadata,
        )
        arguments.update(overrides)
        return run_ingestion_transaction(self.conn, **arguments)

    def assert_rolled_back(self):
        self.assertEqual(self.conn.pending, [])
        self.assertEqual(self.conn.committed, [])
        self.assertEqual((self.conn.commits, self.conn.rollbacks), (0, 1))

    def test_success_uses_one_connection_and_validates_before_commit(self):
        context = self.run_pipeline()
        self.assertEqual(self.conn.events, [
            "begin", "metadata", "facts", "tables", "report", "chunks", "validation", "commit"
        ])
        self.assertEqual((self.conn.commits, self.conn.rollbacks), (1, 0))
        self.assertEqual(self.conn.committed, ["metadata", "facts", "tables", "report", "chunks"])
        self.assertEqual(context.source_ids.xml_document_id, UUID(int=3))
        self.assertEqual(context.source_ids.html_document_id, UUID(int=2))
        self.assertEqual(context.outputs["report"], "report-result")

    def test_failure_at_each_write_stage_aborts_and_does_not_commit(self):
        for name in ("metadata", "facts", "tables", "report", "chunks", "validation"):
            with self.subTest(stage=name):
                self.conn = RecordingConnection()
                self.failure = name
                with self.assertRaisesRegex(ProcessingError, name) as error:
                    self.run_pipeline()
                self.assertNotIn("private", str(error.exception))
                self.assert_rolled_back()
                self.assertEqual(self.conn.events[-2:], [name, "rollback"])

    def test_failed_or_unverified_checks_block_commit(self):
        for value in (False, None, 0, 1):
            with self.subTest(value=value):
                self.conn = RecordingConnection()
                self.validation = ValidationResult(self.versions, {"counts": True, "citations": value})
                with self.assertRaisesRegex(ProcessingError, "unverified"):
                    self.run_pipeline()
                self.assert_rolled_back()

    def test_a_suppressed_abort_cannot_be_reported_as_success(self):
        original_transaction = self.conn.transaction

        @contextmanager
        def suppress_abort():
            try:
                with original_transaction():
                    yield
            except RuntimeError:
                pass

        self.conn.transaction = suppress_abort
        self.failure = "chunks"
        with self.assertRaisesRegex(ProcessingError, "transaction was rolled back"):
            self.run_pipeline()
        self.assert_rolled_back()

    def test_aborted_transactions_cannot_pass_when_adapters_swallow_errors(self):
        original_stages = self.stages

        def aborted(operation):
            def adapter(conn, *args):
                result = operation(conn, *args)
                conn.info.transaction_status.name = "INERROR"
                return result
            return adapter

        for phase in ("metadata", "facts", "tables", "report", "chunks", "validation"):
            with self.subTest(phase=phase):
                self.conn = RecordingConnection()
                arguments = {}
                if phase == "metadata":
                    arguments["seed_metadata"] = aborted(self.seed_metadata)
                else:
                    name = "validate" if phase == "validation" else phase
                    arguments["stages"] = replace(original_stages, **{
                        name: aborted(getattr(original_stages, name)),
                    })
                with self.assertRaisesRegex(ProcessingError, "aborted"):
                    self.run_pipeline(**arguments)
                self.assert_rolled_back()

    def test_missing_required_check_blocks_commit(self):
        self.validation = ValidationResult(self.versions, {"counts": True})
        with self.assertRaisesRegex(ProcessingError, "checks are missing"):
            self.run_pipeline()
        self.assert_rolled_back()

    def test_empty_check_results_block_commit(self):
        self.validation = ValidationResult(self.versions, {})
        with self.assertRaisesRegex(ProcessingError, "checks are missing"):
            self.run_pipeline()
        self.assert_rolled_back()

    def test_different_validation_versions_block_commit(self):
        self.validation = ValidationResult(
            self.versions | {"facts": "test-facts-v2"}, {name: True for name in self.required_checks}
        )
        with self.assertRaisesRegex(ProcessingError, "different processing versions"):
            self.run_pipeline()
        self.assert_rolled_back()

    def test_bare_success_flag_cannot_replace_check_results(self):
        self.validation = True
        with self.assertRaisesRegex(ProcessingError, "explicit check results"):
            self.run_pipeline()
        self.assert_rolled_back()

    def test_missing_adapter_result_blocks_commit(self):
        self.stages = replace(self.stages, tables=lambda conn, context: None)
        with self.assertRaisesRegex(ProcessingError, "tables adapter returned no result"):
            self.run_pipeline()
        self.assert_rolled_back()

    def test_versions_cannot_be_overridden_by_an_adapter(self):
        def overwrite(conn, context):
            context.versions["facts"] = "test-facts-v2"
        self.stages = replace(self.stages, facts=overwrite)
        with self.assertRaisesRegex(ProcessingError, "facts"):
            self.run_pipeline()
        self.assert_rolled_back()
        self.assertEqual(self.versions["facts"], "test-facts-v1")

    def test_missing_adapters_fail_before_metadata(self):
        with self.assertRaisesRegex(ProcessingError, "adapters are required"):
            self.run_pipeline(stages=replace(self.stages, chunks=None))
        self.assertEqual(self.conn.events, [])

    def test_empty_or_duplicate_check_requirements_fail_before_metadata(self):
        for checks in ((), ("counts", "counts"), "counts"):
            with self.subTest(checks=checks):
                with self.assertRaises(ProcessingError):
                    self.run_pipeline(required_checks=checks)
                self.assertEqual(self.conn.events, [])

    def test_missing_or_blank_versions_fail_before_metadata(self):
        for versions in ({"facts": "v1"}, self.versions | {"facts": " "}):
            with self.subTest(versions=versions):
                with self.assertRaises(ProcessingError):
                    self.run_pipeline(versions=versions)
                self.assertEqual(self.conn.events, [])

    def test_changed_manifest_fails_before_metadata(self):
        self.manifest_path.write_text(json.dumps(self.manifest | {"status": "pending"}))
        with self.assertRaisesRegex(ProcessingError, "changed after input verification"):
            self.run_pipeline()
        self.assertEqual(self.conn.events, [])

    def test_unverified_manifest_fails_before_metadata(self):
        with self.assertRaisesRegex(ProcessingError, "verified manifest"):
            self.run_pipeline(manifest=self.manifest | {"status": "pending"})
        self.assertEqual(self.conn.events, [])

    def test_autocommit_is_rejected_before_metadata(self):
        self.conn.autocommit = True
        with self.assertRaisesRegex(ProcessingError, "autocommit=False"):
            self.run_pipeline()
        self.assertEqual(self.conn.events, [])

    def test_existing_transaction_is_rejected_without_rolling_back_caller_work(self):
        self.conn.info.transaction_status.name = "INTRANS"
        self.conn.pending = ["caller-work"]
        with self.assertRaisesRegex(ProcessingError, "fresh idle connection"):
            self.run_pipeline()
        self.assertEqual(self.conn.events, [])
        self.assertEqual(self.conn.pending, ["caller-work"])

    def test_closed_connection_is_rejected_before_metadata(self):
        self.conn.closed = True
        with self.assertRaisesRegex(ProcessingError, "connection is closed"):
            self.run_pipeline()
        self.assertEqual(self.conn.events, [])


if __name__ == "__main__":
    unittest.main()
