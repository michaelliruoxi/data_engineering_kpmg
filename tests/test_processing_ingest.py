"""Import command/summary contracts with a recording connection, not PostgreSQL."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import process_filing as cli
from sec_pipeline.chunking import chunk_report
from sec_pipeline.database import FactInput, FinancialTableInput, ReportInput
from sec_pipeline.processing import (
    IngestionPlan, ParseResult, PipelineStages, ProcessingError, ValidationResult,
    WriteResult, run_ingestion, store_ingestion_chunks,
)
from test_processing_workflow import RecordingConnection


class ImportRecordingConnection(RecordingConnection):
    def __init__(self):
        super().__init__()
        self.info.dbname = "sec_filings_local"
        self.info.host = self.info.hostaddr = "127.0.0.1"
        self.info.port = 5432
        self.rows = {name: {} for name in ("facts", "tables", "report", "chunks")}
        self.queries = []

    @contextmanager
    def transaction(self):
        before = {name: dict(rows) for name, rows in self.rows.items()}
        with super().transaction():
            try:
                yield
            except BaseException:
                self.rows = before
                raise

    def execute(self, statement, parameters):
        self.queries.append((statement, parameters))
        for name, table in (("facts", "financial_facts"), ("tables", "financial_tables"),
                            ("report", "reports"), ("chunks", "chunks")):
            if f"FROM sec.{table}" in statement:
                fields = ("filing_id", "report_id", "chunking_version") if name == "chunks" else (
                    "filing_id", "source_document_id", "extraction_version"
                )
                rows = [(ident,) for ident, record in self.rows[name].items()
                        if tuple(getattr(record, key) for key in fields) == parameters]
                return SimpleNamespace(fetchall=lambda: rows)
        raise AssertionError("Unexpected query in the import recording connection.")

    def close(self):
        self.closed = True


class ProcessingIngestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = json.loads((ROOT / "source_manifest.json").read_text())
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest))
        records = [self.manifest["index_document"], *self.manifest["files"]]
        self.seed = {"filing_id": UUID(int=10), "document_ids": {
            record["local_path"]: UUID(int=i + 1) for i, record in enumerate(records)
        }}
        self.versions = {"facts": "fixture-facts-v1", "tables": "fixture-tables-v1",
                         "reports": "fixture-reports-v1", "chunks": "paragraph-v1"}
        self.text = "Fixture only. 本测试不包含真实财务结果。 €100.\n\n" * 100
        self.conn = ImportRecordingConnection()
        self.validation = ValidationResult(self.versions, {"fixture_rows": True})
        self.events = []
        self.context = None

        def write(name, context, records, *, rejected=(), unresolved=0):
            self.events.append(name)
            self.conn.pending.append(name)
            start = {"facts": 100, "tables": 200, "report": 300, "chunks": 400}[name]
            ids = tuple(UUID(int=start + i) for i in range(len(records)))
            self.conn.rows[name].update(zip(ids, records))
            return WriteResult(ParseResult(records, rejected, unresolved), ids)

        def facts(conn, context):
            self.assertIs(conn, self.conn)
            self.context = context
            records = tuple(FactInput(
                filing_id=context.source_ids.filing_id, source_document_id=context.source_ids.xml_document_id,
                extraction_version=context.versions["facts"], occurrence_key=f"fixture-{index}",
                concept_namespace="https://example.invalid/fixture", concept_name="FixtureValue",
                numeric_value=Decimal(index), period_kind="instant", instant_date=date(2026, 3, 31),
            ) for index in (0, 1))
            return write("facts", context, records, rejected=("Fixture rejection",))

        def tables(conn, context):
            self.assertEqual(len(context.outputs["facts"].stored_ids), 2)
            record = FinancialTableInput(
                filing_id=context.source_ids.filing_id, source_document_id=context.source_ids.html_document_id,
                extraction_version=context.versions["tables"], table_key="fixture-only",
                statement_type="fixture_only", structure={"fixture_only": True}, readable_text="Fixture only",
            )
            return write("tables", context, (record,), unresolved=1)

        def report(conn, context):
            record = ReportInput(
                filing_id=context.source_ids.filing_id, source_document_id=context.source_ids.html_document_id,
                extraction_version=context.versions["reports"], full_text=self.text,
                sections=[{"section_id": "fixture", "offset_start": 0, "offset_end": len(self.text)}],
            )
            return write("report", context, (record,))

        def chunks(conn, context):
            result = context.outputs["report"]
            record = result.parsed.items[0]
            records = chunk_report(
                filing_id=context.source_ids.filing_id, report_id=result.stored_ids[0],
                full_text=record.full_text, sections=record.sections, chunking_version=context.versions["chunks"],
            )
            return write("chunks", context, records)

        def validate(conn, context):
            self.events.append("validate")
            self.assertEqual(conn.commits, 0)
            self.assertEqual(list(context.outputs), ["facts", "tables", "report", "chunks"])
            return self.validation

        self.plan = IngestionPlan(PipelineStages(facts, tables, report, chunks, validate), ("fixture_rows",))
        self.provider = ModuleType("fixture_ingestion_bridge")
        self.provider.get_ingestion_plan = lambda: self.plan
        self.env_file = self.root / "fixture.env"
        self.env_file.write_text("PGHOST=127.0.0.1\nPOSTGRES_DB=unused_default\nPOSTGRES_PASSWORD=fake-private-password\n")
        self.arguments = ["--ingest", "--adapter-module", self.provider.__name__,
                          "--env-file", str(self.env_file), "--database", "sec_filings_local"]
        for name, version in self.versions.items():
            self.arguments.extend((f"--{name}-version", version))

    def seed_metadata(self, conn, path, root):
        conn.pending.append("metadata")
        return self.seed

    def verify(self, manifest_path, data_root):
        self.events.append("verify")
        return json.loads(manifest_path.read_text())

    def run_import(self, **overrides):
        arguments = dict(manifest_path=self.manifest_path, data_root=self.root, versions=self.versions,
                         plan=self.plan, verify_inputs=self.verify, seed_metadata=self.seed_metadata)
        arguments.update(overrides)
        return run_ingestion(self.conn, **arguments)

    def run_cli(self, *arguments, connect_error=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {self.provider.__name__: self.provider}):
            with patch("sec_pipeline.database.connect", side_effect=connect_error,
                       return_value=self.conn) as connection:
                with patch("sec_pipeline.database.seed_manifest", side_effect=self.seed_metadata):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        try:
                            code = cli.main(list(arguments))
                        except SystemExit as exc:
                            code = exc.code
        return code, stdout.getvalue(), stderr.getvalue(), connection

    def assert_rollback(self):
        self.assertEqual((self.conn.commits, self.conn.rollbacks), (0, 1))
        self.assertFalse(any(self.conn.rows.values()))

    def test_summary_uses_actual_scoped_rows_and_validates_before_commit(self):
        summary = self.run_import(reference={"expected_counts": {"facts": 2, "tables": 1, "reports": 1}})
        self.assertEqual(self.events, ["verify", "facts", "tables", "report", "chunks", "validate", "verify"])
        self.assertEqual((self.conn.commits, self.conn.rollbacks), (1, 0))
        self.assertFalse(self.conn.closed)
        self.assertEqual(summary["mode"], "ingest")
        self.assertEqual(summary["counts"]["facts"], {
            "expected": 2, "extracted": 2, "stored": 2, "rejected": 1, "unresolved_cells": 0,
        })
        self.assertEqual(summary["counts"]["tables"]["unresolved_cells"], 1)
        self.assertEqual(len(self.conn.queries), 4)
        self.assertEqual(summary["report_characters"], len(self.text))
        self.assertNotIn(self.text, json.dumps(summary))
        self.assertNotIn(str(self.context.source_ids.filing_id), json.dumps(summary))

    def test_false_or_unverified_required_validation_rolls_back(self):
        for checks in ({"fixture_rows": False}, {"fixture_rows": None}, {}):
            with self.subTest(checks=checks):
                self.conn = ImportRecordingConnection()
                self.validation = ValidationResult(self.versions, checks)
                with self.assertRaises(ProcessingError):
                    self.run_import()
                self.assert_rollback()

    def test_expected_count_mismatch_rolls_back_all_written_rows(self):
        with self.assertRaisesRegex(ProcessingError, "do not match reference"):
            self.run_import(reference={"expected_counts": {"facts": 999}})
        self.assert_rollback()

    def test_missing_or_extra_stored_row_cannot_commit(self):
        original = self.plan.stages.validate
        for extra in (False, True):
            with self.subTest(extra=extra):
                self.conn = ImportRecordingConnection()

                def change(conn, context):
                    result = original(conn, context)
                    if extra:
                        conn.rows["facts"][UUID(int=999)] = context.outputs["facts"].parsed.items[0]
                    else:
                        conn.rows["facts"].pop(context.outputs["facts"].stored_ids[0])
                    return result

                with self.assertRaisesRegex(ProcessingError, "Stored facts rows"):
                    self.run_import(plan=replace(self.plan, stages=replace(self.plan.stages, validate=change)))
                self.assert_rollback()

    def test_temporary_or_wrong_source_ids_and_versions_cannot_commit(self):
        original = self.plan.stages.facts
        for alteration in ({"filing_id": UUID(int=999)}, {"source_document_id": UUID(int=999)},
                           {"extraction_version": "other-version"}):
            with self.subTest(alteration=alteration):
                self.conn = ImportRecordingConnection()

                def wrong(conn, context):
                    result = original(conn, context)
                    records = tuple(replace(record, **alteration) for record in result.parsed.items)
                    return WriteResult(ParseResult(records), result.stored_ids)

                with self.assertRaisesRegex(ProcessingError, "facts records"):
                    self.run_import(plan=replace(self.plan, stages=replace(self.plan.stages, facts=wrong)))
                self.assert_rollback()

    def test_chunks_cannot_use_a_temporary_report_id(self):
        original = self.plan.stages.chunks

        def wrong(conn, context):
            result = original(conn, context)
            records = tuple(replace(record, report_id=UUID(int=999)) for record in result.parsed.items)
            return WriteResult(ParseResult(records), result.stored_ids)

        with self.assertRaisesRegex(ProcessingError, "stored report ID"):
            self.run_import(plan=replace(self.plan, stages=replace(self.plan.stages, chunks=wrong)))
        self.assert_rollback()

    def test_report_must_return_one_storage_input(self):
        original = self.plan.stages.report

        def wrong(conn, context):
            result = original(conn, context)
            return WriteResult(ParseResult(result.parsed.items * 2), (UUID(int=300), UUID(int=301)))

        with self.assertRaisesRegex(ProcessingError, "exactly one ReportInput"):
            self.run_import(plan=replace(self.plan, stages=replace(self.plan.stages, report=wrong)))
        self.assert_rollback()

    def test_malformed_summary_fails_before_commit(self):
        self.validation = ValidationResult(self.versions, {"fixture_rows": True, ("bad", "key"): True})
        with self.assertRaisesRegex(ProcessingError, "during validation"):
            self.run_import()
        self.assert_rollback()

    def test_input_change_during_validation_blocks_commit(self):
        original = self.plan.stages.validate

        def changed(conn, context):
            result = original(conn, context)
            self.manifest_path.write_text(json.dumps(self.manifest | {"status": "pending"}))
            return result

        with self.assertRaisesRegex(ProcessingError, "Inputs changed"):
            self.run_import(plan=replace(self.plan, stages=replace(self.plan.stages, validate=changed)))
        self.assert_rollback()

    def test_reference_checks_reach_validator_with_registered_ids(self):
        original = self.plan.stages.validate
        reference = {"expected_counts": {"reports": 1}, "source_checks": [{"fixture": True}]}

        def check(conn, context):
            self.assertEqual(dict(context.reference), reference)
            self.assertEqual(context.source_ids.filing_id, self.seed["filing_id"])
            return original(conn, context)

        self.run_import(plan=replace(self.plan, stages=replace(self.plan.stages, validate=check)), reference=reference)

    def test_incomplete_plan_and_bad_reference_stop_before_transaction(self):
        for override in ({"plan": replace(self.plan, stages=replace(self.plan.stages, chunks=None))},
                         {"reference": {"expected_counts": {"facts": True}}}):
            with self.subTest(override=override):
                with self.assertRaises(ProcessingError):
                    self.run_import(**override)
                self.assertEqual(self.conn.events, [])

    def test_write_result_rejects_missing_duplicate_and_invalid_ids(self):
        for ids in ((), (UUID(int=1), UUID(int=1)), ("invalid", UUID(int=1))):
            with self.subTest(ids=ids):
                with self.assertRaises(ProcessingError):
                    WriteResult(ParseResult(("one", "two")), ids)

    def test_default_chunk_bridge_uses_real_parser_and_supplied_connection(self):
        def store(conn, chunks):
            self.assertIs(conn, self.conn)
            self.assertTrue(all(chunk.report_id == UUID(int=300) for chunk in chunks))
            ids = tuple(UUID(int=400 + index) for index, _ in enumerate(chunks))
            conn.rows["chunks"].update(zip(ids, chunks))
            return ids

        with patch("sec_pipeline.chunking.store_chunks", side_effect=store) as storage:
            summary = self.run_import(plan=replace(self.plan, stages=replace(self.plan.stages, chunks=store_ingestion_chunks)))
        storage.assert_called_once()
        self.assertGreater(summary["counts"]["chunks"]["stored"], 1)

    def test_cli_success_reports_explicit_destination_and_closes_connection(self):
        code, stdout, stderr, connection = self.run_cli(*self.arguments)
        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["destination"], {"host": "127.0.0.1", "port": 5432, "database": "sec_filings_local"})
        self.assertEqual(summary["counts"]["facts"]["stored"], 2)
        self.assertTrue(self.conn.closed)
        self.assertEqual(connection.call_args.kwargs["dbname"], "sec_filings_local")
        self.assertNotIn("fake-private-password", stdout + stderr)

    def test_cli_ignores_ambient_database_and_libpq_service_settings(self):
        ambient = {"PGHOST": "unexpected.invalid", "PGDATABASE": "shared", "PGSERVICE": "unexpected",
                   "PGSERVICEFILE": "/missing/private", "POSTGRES_DB": "shared"}
        with patch.dict(os.environ, ambient):
            code, _, stderr, connection = self.run_cli(*self.arguments)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(connection.call_args.kwargs["hostaddr"], "127.0.0.1")
            self.assertEqual(connection.call_args.kwargs["host"], "127.0.0.1")
            for key, value in ambient.items():
                self.assertEqual(os.environ[key], value)

    def test_cli_requires_explicit_destination_before_connecting(self):
        versions = []
        for name, version in self.versions.items():
            versions.extend((f"--{name}-version", version))
        code, stdout, stderr, connection = self.run_cli("--ingest", "--adapter-module", self.provider.__name__, *versions)
        self.assertEqual(code, 2)
        self.assertIn("explicit --env-file and --database", stderr)
        connection.assert_not_called()

    def test_cli_corrupt_input_stops_before_adapter_and_connection(self):
        self.provider.get_ingestion_plan = lambda: self.fail("The provider must not load for bad inputs")
        code, stdout, stderr, connection = self.run_cli(*self.arguments, "--manifest", str(self.root / "missing.json"))
        self.assertEqual(code, 1)
        self.assertIn("Input verification failed", stderr)
        self.assertEqual(stdout, "")
        connection.assert_not_called()

    def test_cli_missing_or_incomplete_adapter_stops_before_connection(self):
        for factory in (None, lambda: True, lambda: replace(self.plan, stages=replace(self.plan.stages, facts=None))):
            with self.subTest(factory=factory):
                self.provider.get_ingestion_plan = factory
                code, stdout, stderr, connection = self.run_cli(*self.arguments)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                connection.assert_not_called()

    def test_cli_invalid_reference_and_destination_do_not_connect(self):
        bad_reference = self.root / "bad-reference.json"
        bad_reference.write_text(json.dumps({"expected_counts": {"facts": True}}))
        for extra in (("--reference-file", str(bad_reference)), ("--database", "host=unexpected password=private"),
                      ("--env-file", str(self.root / "missing.env"))):
            with self.subTest(extra=extra):
                code, stdout, stderr, connection = self.run_cli(*self.arguments, *extra)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertNotIn("password=private", stderr)
                connection.assert_not_called()

    def test_cli_connection_failure_hides_private_diagnostics(self):
        code, stdout, stderr, _ = self.run_cli(*self.arguments, connect_error=RuntimeError("fake-private-password"))
        self.assertEqual(code, 1)
        self.assertIn("Cannot connect to the selected database", stderr)
        self.assertEqual(stdout, "")
        self.assertNotIn("fake-private-password", stderr)

    def test_cli_mismatched_connected_target_stops_before_writes_and_closes(self):
        self.conn.info.dbname = "unexpected_database"
        code, stdout, stderr, _ = self.run_cli(*self.arguments)
        self.assertEqual(code, 1)
        self.assertIn("explicitly selected database target", stderr)
        self.assertEqual(self.conn.events, [])
        self.assertTrue(self.conn.closed)

    def test_cli_late_failure_rolls_back_closes_and_has_no_success_summary(self):
        self.validation = ValidationResult(self.versions, {"fixture_rows": False})
        code, stdout, stderr, _ = self.run_cli(*self.arguments)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Validation failed", stderr)
        self.assert_rollback()
        self.assertTrue(self.conn.closed)

    def test_help_does_not_verify_read_config_import_adapter_or_connect(self):
        with patch.object(cli, "verified_inputs", side_effect=AssertionError("Inputs must not be read")):
            with patch.object(cli, "load_plan", side_effect=AssertionError("Adapters must not load")):
                code, stdout, stderr, connection = self.run_cli("--help")
        self.assertEqual(code, 0, stderr)
        for option in ("--ingest", "--dry-run", "--database", "--env-file", "--reference-file"):
            self.assertIn(option, stdout)
        connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
