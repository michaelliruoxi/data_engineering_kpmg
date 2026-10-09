"""Pure dry-run controller tests, using fixture parsers and the real chunker."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_pipeline.processing import (
    DryRunStages, ParseResult, ParsedReport, ProcessingError,
    ValidationResult, run_dry_run,
)


class ProcessingDryRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manifest_path = self.root / "manifest.json"
        self.manifest = json.loads((ROOT / "source_manifest.json").read_text())
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.versions = {
            "facts": "fixture-facts-v1", "tables": "fixture-tables-v1",
            "reports": "fixture-reports-v1", "chunks": "paragraph-v1",
        }
        self.required = ("nonempty", "slices")
        self.events = []
        self.context = None
        self.text = "期末金额 €100. Cafe\u0301 report text.\n\n" * 200

        def facts(context):
            self.events.append("facts")
            self.context = context
            return ParseResult(("fact-one", "fact-two"), rejected=("fixture rejection",))

        def tables(context):
            self.events.append("tables")
            self.assertEqual(len(context.outputs["facts"].items), 2)
            return ParseResult(("table-one",), unresolved_cells=1)

        def report(context):
            self.events.append("report")
            return ParseResult((ParsedReport(self.text, ({
                "section_id": "fixture", "offset_start": 0, "offset_end": len(self.text),
            },)),))

        def validate(context):
            self.events.append("validation")
            report_text = context.outputs["report"].items[0].full_text
            checks = {
                "nonempty": all(result.items for result in context.outputs.values()),
                "slices": all(
                    chunk.text == report_text[chunk.offset_start:chunk.offset_end]
                    and chunk.chunking_version == context.versions["chunks"]
                    for chunk in context.outputs["chunks"].items
                ),
            }
            return ValidationResult(context.versions, checks)

        self.stages = DryRunStages(facts=facts, tables=tables, report=report, validate=validate)

    def verifier(self, manifest_path, data_root):
        self.events.append("verify")
        self.assertEqual(manifest_path, self.manifest_path)
        self.assertEqual(data_root, self.root)
        return json.loads(manifest_path.read_text())

    def run_dry(self, **overrides):
        arguments = dict(
            manifest_path=self.manifest_path, data_root=self.root,
            versions=self.versions, stages=self.stages,
            required_checks=self.required, verify_inputs=self.verifier,
        )
        arguments.update(overrides)
        with patch("psycopg.connect", side_effect=AssertionError("database connection forbidden")) as pg:
            with patch("sec_pipeline.database.connect", side_effect=AssertionError("database connection forbidden")) as db:
                result = run_dry_run(**arguments)
            db.assert_not_called()
        pg.assert_not_called()
        return result

    def test_parses_all_stages_and_uses_real_chunks_without_database(self):
        summary = self.run_dry()
        self.assertEqual(self.events, ["verify", "facts", "tables", "report", "validation", "verify"])
        self.assertEqual(summary["mode"], "dry-run")
        self.assertTrue(summary["validation"]["passed"])
        self.assertEqual(summary["versions"], self.versions)
        self.assertEqual(summary["counts"]["facts"]["extracted"], 2)
        self.assertEqual(summary["counts"]["facts"]["rejected"], 1)
        self.assertEqual(summary["counts"]["tables"]["unresolved_cells"], 1)
        self.assertGreater(summary["counts"]["chunks"]["extracted"], 1)
        self.assertEqual(summary["report_characters"], len(self.text))
        self.assertTrue(all(item["stored"] == 0 for item in summary["counts"].values()))
        self.assertEqual(len(summary["source_hashes"]), 4)

    def test_summary_does_not_expose_temporary_ids_or_parsed_records(self):
        encoded = json.dumps(self.run_dry())
        for value in (
            self.context.source_ids.filing_id, self.context.source_ids.xml_document_id,
            self.context.source_ids.html_document_id, self.context.report_id,
        ):
            self.assertNotIn(str(value), encoded)
        self.assertNotIn("fact-one", encoded)
        self.assertNotIn(self.text, encoded)
        self.assertFalse(hasattr(self.context, "connection"))

    def test_verification_failure_stops_before_parsers(self):
        def fail(*arguments):
            raise OSError("private filesystem diagnostic")
        with self.assertRaisesRegex(ProcessingError, "input verification") as error:
            self.run_dry(verify_inputs=fail)
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.events, [])

    def test_missing_parser_cannot_be_skipped(self):
        with self.assertRaisesRegex(ProcessingError, "adapters are required"):
            self.run_dry(stages=replace(self.stages, tables=None))
        self.assertEqual(self.events, [])

    def test_parser_failure_identifies_stage_without_private_diagnostics(self):
        def fail(context):
            raise RuntimeError("fake password must not appear")
        with self.assertRaisesRegex(ProcessingError, "tables") as error:
            self.run_dry(stages=replace(self.stages, tables=fail))
        self.assertNotIn("password", str(error.exception))
        self.assertEqual(self.events, ["verify", "facts"])

    def test_none_result_is_not_a_successful_parse(self):
        with self.assertRaisesRegex(ProcessingError, "facts adapter must return ParseResult"):
            self.run_dry(stages=replace(self.stages, facts=lambda context: None))

    def test_bad_report_shape_stops_before_chunks(self):
        with self.assertRaisesRegex(ProcessingError, "one ParsedReport"):
            self.run_dry(stages=replace(self.stages, report=lambda context: ParseResult(("raw text",))))

    def test_bad_section_offsets_are_rejected_by_actual_chunker(self):
        def report(context):
            return ParseResult((ParsedReport("hello", ({"offset_start": 0, "offset_end": 10},)),))
        with self.assertRaisesRegex(ProcessingError, "chunks"):
            self.run_dry(stages=replace(self.stages, report=report))

    def test_missing_or_unverified_validation_cannot_pass(self):
        for checks in ({}, {"nonempty": True, "slices": None}, {"nonempty": True, "slices": False}):
            with self.subTest(checks=checks):
                stages = replace(self.stages, validate=lambda context: ValidationResult(context.versions, checks))
                with self.assertRaises(ProcessingError):
                    self.run_dry(stages=stages)

    def test_validation_for_other_versions_cannot_pass(self):
        def validate(context):
            return ValidationResult(dict(context.versions) | {"facts": "other-v1"}, {"nonempty": True, "slices": True})
        with self.assertRaisesRegex(ProcessingError, "different processing versions"):
            self.run_dry(stages=replace(self.stages, validate=validate))

    def test_reference_is_passed_to_validator_and_expected_counts_are_reported(self):
        reference = {"expected_counts": {"facts": 2, "reports": 1}, "fixture_value": 100}
        validator = self.stages.validate

        def validate(context):
            self.assertEqual(dict(context.reference), reference)
            return validator(context)

        summary = self.run_dry(stages=replace(self.stages, validate=validate), reference=reference)
        self.assertEqual(summary["counts"]["facts"]["expected"], 2)
        self.assertIsNone(summary["counts"]["chunks"]["expected"])

    def test_expected_count_mismatch_blocks_success(self):
        with self.assertRaisesRegex(ProcessingError, "do not match reference"):
            self.run_dry(reference={"expected_counts": {"facts": 999}})

    def test_invalid_reference_counts_stop_before_parsing(self):
        for counts in ({"facts": -1}, {"facts": True}, {"unknown": 1}):
            with self.subTest(counts=counts):
                with self.assertRaises(ProcessingError):
                    self.run_dry(reference={"expected_counts": counts})
                self.assertEqual(self.events, [])

    def test_input_changes_during_validation_block_success(self):
        validator = self.stages.validate

        def validate(context):
            result = validator(context)
            self.manifest_path.write_text(json.dumps(self.manifest | {"status": "pending"}))
            return result

        with self.assertRaisesRegex(ProcessingError, "Inputs changed during parsing"):
            self.run_dry(stages=replace(self.stages, validate=validate))

    def test_context_manifest_cannot_change_the_verified_snapshot(self):
        report = self.stages.report

        def alter(context):
            context.manifest["files"][0]["sha256"] = "fixture mutation"
            return report(context)

        summary = self.run_dry(stages=replace(self.stages, report=alter))
        path = self.manifest["files"][0]["local_path"]
        self.assertEqual(summary["source_hashes"][path], self.manifest["files"][0]["sha256"])

    def test_parser_results_require_materialized_records_and_valid_diagnostics(self):
        for value in ("text", {"record": 1}, iter([1])):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ProcessingError):
                    ParseResult(value)
        for value in (-1, True, "1"):
            with self.subTest(unresolved=value):
                with self.assertRaises(ProcessingError):
                    ParseResult((), unresolved_cells=value)


if __name__ == "__main__":
    unittest.main()
