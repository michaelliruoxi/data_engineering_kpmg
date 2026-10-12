"""Real PostgreSQL runner tests with clearly marked, non-financial fixtures.

Use scripts/test_ingestion_local.py. Successful runs commit fixture rows to prove
visibility and replay, then delete only the recorded fixture IDs and versions.
The database must be local, dedicated, migrated, and empty before the suite.
No databases are created, migrated, truncated, or dropped by this module.
"""

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date
from decimal import Decimal
import hashlib
import io
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from process_filing import verified_inputs
from sec_pipeline import chunking, database as db
from sec_pipeline.processing import (
    IngestionPlan, ParseResult, PipelineStages, ProcessingError, ValidationResult,
    WriteResult, run_ingestion, run_ingestion_transaction,
)


TABLES = (
    "companies", "filings", "source_documents", "financial_facts",
    "financial_tables", "financial_table_facts", "reports", "chunks",
)
FIXTURE_TEXT = (
    "Fixture only: Revenue 😀 rose by €1.25. Cafe\u0301 text.\n\n"
    "净利润增长。These sentences are transaction test data, not an extracted SEC report.\n\n"
    "The original filing is unchanged. This paragraph also exercises chunk overlap."
)


def check_connection_target(conn, options):
    # The client connects to the published loopback port. Docker can forward it
    # to a private server interface, so inet_server_addr() is not that address.
    address = conn.info.hostaddr
    if address not in {"127.0.0.1", "::1"} or address != options["hostaddr"]:
        raise RuntimeError("The actual connection does not use the requested local loopback address.")
    database = conn.execute("SELECT current_database()").fetchone()[0]
    if database != options["dbname"]:
        raise RuntimeError("The actual database is not the requested dedicated test database.")


def load_verified_test_inputs():
    """Reuse the tested CLI verifier before opening any live connection."""
    with redirect_stdout(io.StringIO()):
        return verified_inputs(manifest_path=ROOT / "source_manifest.json", data_root=ROOT)


class IngestionPostgresTests(unittest.TestCase):
    connection_options = None

    @classmethod
    def open_connection(cls):
        return psycopg.connect(**cls.connection_options)

    @classmethod
    def snapshot(cls):
        with cls.open_connection() as observer:
            return {
                table: observer.execute(sql.SQL("SELECT * FROM sec.{} ORDER BY {}").format(
                    sql.Identifier(table),
                    sql.SQL("table_id, fact_id") if table == "financial_table_facts"
                    else sql.Identifier("id"),
                )).fetchall()
                for table in TABLES
            }

    @classmethod
    def source_hashes(cls):
        records = [cls.manifest["index_document"], *cls.manifest["files"]]
        paths = [ROOT / "source_manifest.json", *(ROOT / r["local_path"] for r in records)]
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}

    @classmethod
    def setUpClass(cls):
        if cls.connection_options is None:
            raise unittest.SkipTest("Use scripts/test_ingestion_local.py for live PostgreSQL tests.")
        options = cls.connection_options
        if (options.get("host") not in {"localhost", "127.0.0.1", "::1"}
                or options.get("hostaddr") not in {"127.0.0.1", "::1"}
                or not re.fullmatch(r"[A-Za-z0-9_]+_test(?:_[A-Za-z0-9_]+)?", options["dbname"])):
            raise RuntimeError("Live ingestion tests require an explicitly local test database.")
        cls.enterClassContext(patch("httpx.Client.send", side_effect=AssertionError("Unexpected HTTP request")))
        cls.enterClassContext(patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected HTTP request")))
        cls.manifest = load_verified_test_inputs()
        cls.initial_hashes = cls.source_hashes()
        cls.guard = cls.open_connection()
        cls.addClassCleanup(cls.guard.close)
        if not cls.guard.execute("SELECT pg_try_advisory_lock(%s)", (73654321455,)).fetchone()[0]:
            raise RuntimeError("Another ingestion test suite is using this test database.")
        check_connection_target(cls.guard, options)
        baseline = db.status(cls.guard)
        cls.guard.rollback()
        if not baseline["ready"]:
            raise RuntimeError("Migrate the dedicated test database before running this suite.")
        if any(baseline["counts"].values()):
            raise RuntimeError("The dedicated test database must be empty; existing data will not be deleted.")
        cls.baseline = cls.snapshot()
        cls.addClassCleanup(cls.assert_final_state)

    @classmethod
    def assert_final_state(cls):
        if cls.snapshot() != cls.baseline:
            raise AssertionError("Fixture cleanup did not restore the original empty test database.")
        if cls.source_hashes() != cls.initial_hashes:
            raise AssertionError("An original source file or manifest changed during the tests.")

    def setUp(self):
        self.assertEqual(self.snapshot(), self.baseline)
        prefix = f"ingestion-fixture-{uuid4().hex}"
        self.versions = {name: f"{prefix}-{name}" for name in ("facts", "tables", "reports", "chunks")}
        self.required = ("metadata", "facts", "tables_links", "report", "chunks", "uncommitted")
        self.owned_filings = set()
        self.owned_companies = set()
        self.events = []
        self.validation_seen = False
        self.addCleanup(self.cleanup_fixtures)

    def cleanup_fixtures(self):
        # Restrict output deletion to this test's versions and metadata to IDs
        # captured from its default seed. Foreign rows make FK deletion fail,
        # rolling the cleanup transaction back rather than deleting other data.
        with self.open_connection() as cleaner:
            for filing in self.owned_filings:
                cleaner.execute("""DELETE FROM sec.financial_table_facts
                    WHERE filing_id = %s
                    AND table_id IN (SELECT id FROM sec.financial_tables
                        WHERE filing_id = %s AND extraction_version = %s)
                    AND fact_id IN (SELECT id FROM sec.financial_facts
                        WHERE filing_id = %s AND extraction_version = %s)""",
                    (filing, filing, self.versions["tables"], filing, self.versions["facts"]))
                for table, key, version in (
                    ("chunks", "chunking_version", "chunks"),
                    ("financial_tables", "extraction_version", "tables"),
                    ("reports", "extraction_version", "reports"),
                    ("financial_facts", "extraction_version", "facts"),
                ):
                    cleaner.execute(sql.SQL("DELETE FROM sec.{} WHERE filing_id = %s AND {} = %s").format(
                        sql.Identifier(table), sql.Identifier(key)), (filing, self.versions[version]))
                cleaner.execute("DELETE FROM sec.source_documents WHERE filing_id = %s", (filing,))
                cleaner.execute("DELETE FROM sec.filings WHERE id = %s", (filing,))
            for company in self.owned_companies:
                cleaner.execute("DELETE FROM sec.companies WHERE id = %s", (company,))
        self.assertEqual(self.snapshot(), self.baseline)

    def fixture_stages(self, *, fail_after=None, validation_change=None):
        def facts(conn, context):
            filing = context.source_ids.filing_id
            self.owned_filings.add(filing)
            company = conn.execute("SELECT company_id FROM sec.filings WHERE id = %s", (filing,)).fetchone()[0]
            self.owned_companies.add(company)
            records = tuple(db.FactInput(
                filing_id=filing, source_document_id=context.source_ids.xml_document_id,
                extraction_version=context.versions["facts"], occurrence_key=f"fixture-{index}",
                concept_namespace="https://example.invalid/ingestion-fixture", concept_name=f"FixtureValue{index}",
                numeric_value=value, raw_value=str(value), period_kind="duration",
                period_start=date(2026, 1, 1), period_end=date(2026, 3, 31),
                unit={"numerator": ["{http://www.xbrl.org/2003/iso4217}USD"], "denominator": []},
                source_reference={"fixture_only": True},
            ) for index, value in enumerate((Decimal("1.250"), Decimal("0"))))
            return {"items": records, "ids": tuple(db.store_fact(conn, record) for record in records)}

        def tables(conn, context):
            record = db.FinancialTableInput(
                filing_id=context.source_ids.filing_id, source_document_id=context.source_ids.html_document_id,
                extraction_version=context.versions["tables"], table_key="fixture-only-table",
                statement_type="fixture_only", structure={"fixture_only": True, "rows": [["A", "1.250"], ["B", "0"]]},
                readable_text="Fixture only | A: 1.250 | B: 0",
            )
            table_id = db.store_financial_table(conn, record)
            for fact_id in context.outputs["facts"]["ids"]:
                db.link_table_fact(conn, context.source_ids.filing_id, table_id, fact_id)
            return {"item": record, "id": table_id}

        def report(conn, context):
            record = db.ReportInput(
                filing_id=context.source_ids.filing_id, source_document_id=context.source_ids.html_document_id,
                extraction_version=context.versions["reports"], full_text=FIXTURE_TEXT,
                sections=[{"title": "Fixture only", "offset_start": 0, "offset_end": len(FIXTURE_TEXT)}],
            )
            return {"item": record, "id": db.store_report(conn, record)}

        def chunks(conn, context):
            report_result = context.outputs["report"]
            record = report_result["item"]
            items = tuple(chunking.chunk_report(
                filing_id=context.source_ids.filing_id, report_id=report_result["id"],
                full_text=record.full_text, sections=record.sections,
                chunking_version=context.versions["chunks"], max_chars=80, overlap=15,
            ))
            return {"items": items, "ids": tuple(chunking.store_chunks(conn, items))}

        def validate(conn, context):
            self.events.append("validation")
            self.validation_seen = True
            result = self.validate_rows(conn, context)
            return validation_change(result) if validation_change else result

        def wrapped(name, operation):
            def stage(conn, context):
                self.events.append(name)
                result = operation(conn, context)
                if fail_after == name:
                    raise RuntimeError("Deliberate fixture-stage failure after database writes.")
                return result
            return stage

        return PipelineStages(**{
            name: wrapped(name, operation)
            for name, operation in (("facts", facts), ("tables", tables), ("report", report), ("chunks", chunks))
        }, validate=validate)

    def validate_rows(self, conn, context):
        filing = context.source_ids.filing_id
        sources = conn.execute("SELECT role, local_path, sha256 FROM sec.source_documents WHERE filing_id = %s", (filing,)).fetchall()
        expected_sources = [(r["role"], r["local_path"], r["sha256"]) for r in [self.manifest["index_document"], *self.manifest["files"]]]
        facts = conn.execute("""SELECT id, source_document_id, extraction_version, numeric_value, raw_value
            FROM sec.financial_facts WHERE filing_id = %s ORDER BY occurrence_key""", (filing,)).fetchall()
        expected_facts = [(ident, r.source_document_id, r.extraction_version, r.numeric_value, r.raw_value)
            for ident, r in zip(context.outputs["facts"]["ids"], context.outputs["facts"]["items"])]
        table = context.outputs["tables"]
        tables = conn.execute("""SELECT id, source_document_id, extraction_version, structure, readable_text
            FROM sec.financial_tables WHERE filing_id = %s""", (filing,)).fetchall()
        links = conn.execute("SELECT table_id, fact_id FROM sec.financial_table_facts WHERE filing_id = %s", (filing,)).fetchall()
        report = context.outputs["report"]
        reports = conn.execute("""SELECT id, source_document_id, extraction_version, full_text, sections
            FROM sec.reports WHERE filing_id = %s""", (filing,)).fetchall()
        chunks = conn.execute("""SELECT c.id, c.report_id, c.chunking_version, c.chunk_index, c.text,
            c.offset_start, c.offset_end, c.section, c.metadata, s.role, r.extraction_version
            FROM sec.chunks c JOIN sec.reports r ON r.id = c.report_id
            JOIN sec.source_documents s ON s.id = r.source_document_id
            WHERE c.filing_id = %s ORDER BY c.chunk_index""", (filing,)).fetchall()
        expected_chunks = [(ident, r.report_id, r.chunking_version, r.chunk_index, r.text,
            r.offset_start, r.offset_end, r.section, r.metadata, "main_inline_xbrl_filing", self.versions["reports"])
            for ident, r in zip(context.outputs["chunks"]["ids"], context.outputs["chunks"]["items"])]
        checks = {
            "metadata": sorted(sources) == sorted(expected_sources) and len(sources) == 4,
            "facts": facts == expected_facts and all(r[1] == context.source_ids.xml_document_id for r in facts),
            "tables_links": tables == [(table["id"], context.source_ids.html_document_id, self.versions["tables"],
                table["item"].structure, table["item"].readable_text)]
                and set(links) == {(table["id"], ident) for ident in context.outputs["facts"]["ids"]},
            "report": reports == [(report["id"], context.source_ids.html_document_id, self.versions["reports"],
                FIXTURE_TEXT, report["item"].sections)],
            "chunks": len(chunks) > 1 and chunks == expected_chunks and all(
                r.text == FIXTURE_TEXT[r.offset_start:r.offset_end] for r in context.outputs["chunks"]["items"]),
            "uncommitted": self.snapshot() == self.observer_before,
        }
        return ValidationResult(versions=dict(context.versions), checks=checks)

    def run_once(self, stages=None):
        self.events.clear()
        self.validation_seen = False
        self.observer_before = self.snapshot()
        with self.open_connection() as writer:
            try:
                return run_ingestion_transaction(
                    writer, manifest=self.manifest, manifest_path=ROOT / "source_manifest.json", data_root=ROOT,
                    versions=self.versions, stages=stages or self.fixture_stages(), required_checks=self.required,
                )
            finally:
                self.assertFalse(writer.closed)
                self.assertEqual(writer.info.transaction_status, TransactionStatus.IDLE)

    def ingestion_fixture_plan(self):
        """Adapt this suite's existing fixture writers to the import contract."""
        original = self.fixture_stages()

        def fixture_context(context):
            outputs = {}
            for name, result in context.outputs.items():
                if name in {"facts", "chunks"}:
                    outputs[name] = {"items": result.parsed.items, "ids": result.stored_ids}
                else:
                    outputs[name] = {"item": result.parsed.items[0], "id": result.stored_ids[0]}
            return replace(context, outputs=outputs)

        def bridge(name):
            def write(conn, context):
                result = getattr(original, name)(conn, fixture_context(context))
                if name in {"facts", "chunks"}:
                    return WriteResult(ParseResult(result["items"]), result["ids"])
                return WriteResult(ParseResult((result["item"],)), (result["id"],))
            return write

        return IngestionPlan(PipelineStages(
            facts=bridge("facts"), tables=bridge("tables"), report=bridge("report"), chunks=bridge("chunks"),
            validate=lambda conn, context: original.validate(conn, fixture_context(context)),
        ), self.required)

    def run_import_once(self, reference=None):
        self.events.clear()
        self.validation_seen = False
        self.observer_before = self.snapshot()
        with self.open_connection() as writer:
            try:
                return run_ingestion(
                    writer, manifest_path=ROOT / "source_manifest.json", data_root=ROOT,
                    versions=self.versions, plan=self.ingestion_fixture_plan(), reference=reference,
                    verify_inputs=lambda path, root: load_verified_test_inputs(),
                )
            finally:
                self.assertFalse(writer.closed)
                self.assertEqual(writer.info.transaction_status, TransactionStatus.IDLE)

    def test_import_summary_replay_uses_actual_database_counts(self):
        reference = {"expected_counts": {"facts": 2, "tables": 1, "reports": 1}}
        first = self.run_import_once(reference)
        before = self.snapshot()
        second = self.run_import_once(reference)
        self.assertEqual(first, second)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(first["validation"]["passed"])
        for name, table in (("facts", "financial_facts"), ("tables", "financial_tables"),
                            ("reports", "reports"), ("chunks", "chunks")):
            with self.subTest(stage=name):
                self.assertEqual(first["counts"][name]["stored"], len(before[table]))
                self.assertEqual(first["counts"][name]["extracted"], len(before[table]))

    def test_import_reference_count_failure_rolls_back_before_commit(self):
        with self.assertRaisesRegex(ProcessingError, "do not match reference"):
            self.run_import_once({"expected_counts": {"facts": 999}})
        self.assertTrue(self.validation_seen)
        self.assertEqual(self.snapshot(), self.baseline)

    def test_success_commits_all_stages_only_after_real_row_validation(self):
        context = self.run_once()
        self.assertEqual(self.events, ["facts", "tables", "report", "chunks", "validation"])
        self.assertTrue(all(context.outputs["validation"].checks.values()))
        self.assertEqual(self.source_hashes(), self.initial_hashes)
        rows = self.snapshot()
        self.assertEqual({key: len(value) for key, value in rows.items()}, {
            "companies": 1, "filings": 1, "source_documents": 4, "financial_facts": 2,
            "financial_tables": 1, "financial_table_facts": 2, "reports": 1,
            "chunks": len(context.outputs["chunks"]["ids"]),
        })

    def test_each_stage_failure_rolls_back_real_metadata_and_all_outputs(self):
        for stage in ("facts", "tables", "report", "chunks"):
            with self.subTest(stage=stage):
                with self.assertRaisesRegex(ProcessingError, f"during {stage}"):
                    self.run_once(self.fixture_stages(fail_after=stage))
                self.assertFalse(self.validation_seen)
                self.assertEqual(self.snapshot(), self.baseline)

    def test_late_failed_unverified_missing_and_wrong_version_validation_roll_back(self):
        changes = {
            "failed": lambda result: replace(result, checks=dict(result.checks) | {"chunks": False}),
            "unverified": lambda result: replace(result, checks=dict(result.checks) | {"chunks": None}),
            "missing": lambda result: replace(result, checks={k: v for k, v in result.checks.items() if k != "chunks"}),
            "wrong_version": lambda result: replace(result, versions=dict(result.versions) | {"facts": "another-version"}),
        }
        for name, change in changes.items():
            with self.subTest(validation=name):
                with self.assertRaises(ProcessingError):
                    self.run_once(self.fixture_stages(validation_change=change))
                self.assertTrue(self.validation_seen)
                self.assertEqual(self.events[-1], "validation")
                self.assertEqual(self.snapshot(), self.baseline)

    def test_replay_preserves_all_ids_content_counts_and_table_links(self):
        first = self.run_once()
        before = self.snapshot()
        second = self.run_once()
        self.assertEqual(second.source_ids, first.source_ids)
        for stage in ("facts", "tables", "report", "chunks"):
            self.assertEqual(second.outputs[stage], first.outputs[stage])
        self.assertEqual(self.snapshot(), before)

    def test_failed_replay_preserves_previously_committed_rows(self):
        self.run_once()
        before = self.snapshot()
        change = lambda result: replace(result, checks=dict(result.checks) | {"chunks": False})
        with self.assertRaises(ProcessingError):
            self.run_once(self.fixture_stages(validation_change=change))
        self.assertTrue(self.validation_seen)
        self.assertEqual(self.snapshot(), before)

    def test_content_conflict_rolls_back_new_rows_and_preserves_previous_import(self):
        first = self.run_once()
        before = self.snapshot()
        stages = self.fixture_stages()

        def conflict(conn, context):
            record = first.outputs["facts"]["items"][0]
            db.store_fact(conn, replace(record, occurrence_key="new-before-conflict"))
            return db.store_fact(conn, replace(record, numeric_value=Decimal("999"), raw_value="999"))

        with self.assertRaisesRegex(ProcessingError, "during facts") as failure:
            self.run_once(replace(stages, facts=conflict))
        self.assertIsInstance(failure.exception.__cause__, db.DataConflict)
        self.assertFalse(self.validation_seen)
        self.assertEqual(self.snapshot(), before)

    def test_late_database_constraint_failure_rolls_back_the_entire_import(self):
        stages = self.fixture_stages()

        def invalid_chunk(conn, context):
            result = stages.chunks(conn, context)
            db.store_chunk(conn, replace(result["items"][0], chunk_index=len(result["items"]), text="Wrong quotation"))
            return result

        with self.assertRaisesRegex(ProcessingError, "during chunks") as failure:
            self.run_once(replace(stages, chunks=invalid_chunk))
        self.assertIsInstance(failure.exception.__cause__, psycopg.errors.CheckViolation)
        self.assertFalse(self.validation_seen)
        self.assertEqual(self.snapshot(), self.baseline)

    def test_adapter_cannot_manually_commit_or_rollback_the_outer_transaction(self):
        for operation in ("commit", "rollback"):
            with self.subTest(operation=operation):
                stages = self.fixture_stages()

                def invalid_finish(conn, context):
                    result = stages.facts(conn, context)
                    getattr(conn, operation)()
                    return result

                with self.assertRaisesRegex(ProcessingError, "during facts") as failure:
                    self.run_once(replace(stages, facts=invalid_finish))
                self.assertIsInstance(failure.exception.__cause__, psycopg.ProgrammingError)
                self.assertEqual(self.snapshot(), self.baseline)

    def test_swallowed_sql_errors_in_stage_or_validator_cannot_report_success(self):
        for phase in ("chunks", "validate"):
            with self.subTest(phase=phase):
                stages = self.fixture_stages()
                operation = getattr(stages, phase)

                def swallow_error(conn, context):
                    result = operation(conn, context)
                    try:
                        conn.execute("SELECT 1 / 0")
                    except psycopg.errors.DivisionByZero:
                        pass
                    return result

                with self.assertRaisesRegex(ProcessingError, "aborted"):
                    self.run_once(replace(stages, **{phase: swallow_error}))
                self.assertEqual(self.snapshot(), self.baseline)

    def test_recovered_savepoint_error_leaves_the_outer_transaction_healthy(self):
        stages = self.fixture_stages()

        def recovered(conn, context):
            result = stages.facts(conn, context)
            try:
                with conn.transaction():
                    conn.execute("SELECT 1 / 0")
            except psycopg.errors.DivisionByZero:
                pass
            return result

        context = self.run_once(replace(stages, facts=recovered))
        self.assertTrue(all(context.outputs["validation"].checks.values()))
        self.assertEqual(len(self.snapshot()["financial_facts"]), 2)


if __name__ == "__main__":
    unittest.main()
