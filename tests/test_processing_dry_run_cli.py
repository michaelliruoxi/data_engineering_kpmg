"""CLI dry-run isolation with real source verification and fixture parsers."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

# This is a test provider, not the team's financial extractors or cleaner.
PROVIDER = r'''
from pathlib import Path
from lxml import etree, html
import psycopg
import sec_pipeline.database as database
from sec_pipeline.processing import (
    DryRunPlan, DryRunStages, ParseResult, ParsedReport, ValidationResult,
)

def forbidden(*args, **kwargs):
    raise AssertionError("A database connection is forbidden in this dry-run test.")

psycopg.connect = forbidden
database.connect = forbidden

def source(context, role):
    record = next(item for item in context.manifest["files"] if item["role"] == role)
    return context.data_root / record["local_path"]

def facts(context):
    tree = etree.parse(str(source(context, "extracted_xbrl_instance")),
                       etree.XMLParser(no_network=True, resolve_entities=False))
    values = [(node.tag, node.text) for node in tree.getroot() if node.get("contextRef")][:2]
    return ParseResult(values)

def tables(context):
    tree = html.parse(str(source(context, "main_inline_xbrl_filing")),
                      parser=html.HTMLParser(no_network=True))
    return ParseResult([table.text_content() for table in tree.xpath("//table")[:1]])

def report(context):
    tree = html.parse(str(source(context, "main_inline_xbrl_filing")),
                      parser=html.HTMLParser(no_network=True))
    text = " ".join(tree.getroot().text_content().split())[:2500]
    return ParseResult((ParsedReport(text, ({"section_id": "fixture",
        "offset_start": 0, "offset_end": len(text)},)),))

def validate(context):
    text = context.outputs["report"].items[0].full_text
    return ValidationResult(context.versions, {
        "parsers": all(result.items for result in context.outputs.values()),
        "slices": all(chunk.text == text[chunk.offset_start:chunk.offset_end]
                      for chunk in context.outputs["chunks"].items),
    })

def get_dry_run_plan():
    return DryRunPlan(DryRunStages(facts=facts, tables=tables, report=report,
                                 validate=validate), ("parsers", "slices"))
'''


class ProcessingDryRunCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("process_filing.py", "download_sec_sample.py"):
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        shutil.copytree(ROOT / "src" / "sec_pipeline", self.root / "src" / "sec_pipeline",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.provider = self.root / "src" / "fixture_parsers.py"
        self.provider.write_text(PROVIDER)
        self.manifest = json.loads((ROOT / "source_manifest.json").read_text())
        self.manifest_path = self.root / "source_manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.records = [self.manifest["index_document"], *self.manifest["files"]]
        self.script = scripts / "process_filing.py"
        self.arguments = [
            "--dry-run", "--adapter-module", "fixture_parsers",
            "--facts-version", "fixture-facts-v1", "--tables-version", "fixture-tables-v1",
            "--reports-version", "fixture-reports-v1", "--chunks-version", "paragraph-v1",
        ]

    def copy_sources(self, root=None):
        destination = self.root if root is None else root
        for record in self.records:
            target = destination / record["local_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / record["local_path"], target)

    def run_cli(self, *arguments):
        environment = os.environ.copy()
        for key in list(environment):
            if key.startswith(("PG", "POSTGRES_")):
                environment.pop(key)
        return subprocess.run(
            [sys.executable, str(self.script), *arguments], cwd=self.root,
            env=environment, capture_output=True, text=True, timeout=30,
        )

    def test_help_lists_dry_run_without_inputs_or_adapter_import(self):
        self.provider.write_text("raise RuntimeError('help must not load adapters')")
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for option in ("--verify-inputs", "--dry-run", "--adapter-module", "--reference-file", "--data-root"):
            self.assertIn(option, result.stdout)

    def test_real_sources_and_fixture_parsers_work_without_database_credentials(self):
        self.copy_sources()
        before = {record["local_path"]: (self.root / record["local_path"]).stat().st_mtime_ns
                  for record in self.records}
        result = self.run_cli(*self.arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["accession_number"], self.manifest["accession_number"])
        self.assertEqual(summary["counts"]["facts"]["extracted"], 2)
        self.assertEqual(summary["counts"]["tables"]["extracted"], 1)
        self.assertGreater(summary["counts"]["chunks"]["extracted"], 0)
        self.assertTrue(all(item["stored"] == 0 for item in summary["counts"].values()))
        self.assertEqual(summary["source_hashes"], {r["local_path"]: r["sha256"] for r in self.records})
        self.assertNotIn("filing_id", result.stdout)
        self.assertNotIn("Verified ", result.stdout)
        after = {record["local_path"]: (self.root / record["local_path"]).stat().st_mtime_ns
                 for record in self.records}
        self.assertEqual(after, before)

    def test_missing_team_adapter_module_is_an_error_not_a_success(self):
        self.copy_sources()
        self.provider.unlink()
        result = self.run_cli(*self.arguments)
        self.assertEqual(result.returncode, 1)
        self.assertIn("adapter module is unavailable", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_bad_adapter_factory_is_an_error_not_a_success(self):
        self.copy_sources()
        self.provider.write_text("def get_dry_run_plan():\n    return True\n")
        result = self.run_cli(*self.arguments)
        self.assertEqual(result.returncode, 1)
        self.assertIn("must return DryRunPlan", result.stderr)

    def test_corrupt_source_stops_before_loading_adapters(self):
        self.copy_sources()
        self.provider.write_text("raise RuntimeError('adapters must not load')")
        path = self.root / self.manifest["index_document"]["local_path"]
        content = bytearray(path.read_bytes())
        content[0] ^= 1
        path.write_bytes(content)
        result = self.run_cli(*self.arguments)
        self.assertEqual(result.returncode, 1)
        self.assertIn("SHA-256 mismatch", result.stderr)
        self.assertNotIn("adapters must not load", result.stderr)

    def test_dry_run_requires_explicit_adapters_and_versions(self):
        result = self.run_cli("--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --adapter-module", result.stderr)

    def test_private_stage_exception_is_not_logged(self):
        self.copy_sources()
        self.provider.write_text(PROVIDER + "\ndef tables(context):\n    raise RuntimeError('fake-password-private')\n")
        result = self.run_cli(*self.arguments)
        self.assertEqual(result.returncode, 1)
        self.assertIn("tables", result.stderr)
        self.assertNotIn("fake-password", result.stderr)

    def test_reference_count_mismatch_cannot_report_success(self):
        self.copy_sources()
        reference = self.root / "reference.json"
        reference.write_text(json.dumps({"expected_counts": {"facts": 999}}))
        result = self.run_cli(*self.arguments, "--reference-file", str(reference))
        self.assertEqual(result.returncode, 1)
        self.assertIn("do not match reference", result.stderr)

    def test_explicit_data_root_is_used_for_verification_and_parsing(self):
        elsewhere = self.root / "other-inputs"
        self.copy_sources(elsewhere)
        result = self.run_cli(*self.arguments, "--data-root", str(elsewhere))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"]["facts"]["extracted"], 2)


if __name__ == "__main__":
    unittest.main()
