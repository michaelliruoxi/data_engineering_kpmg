"""Offline CLI checks for the Task 2 input-verification stage."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProcessingInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("process_filing.py", "download_sec_sample.py"):
            shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        self.script = scripts / "process_filing.py"
        self.manifest_path = self.root / "source_manifest.json"
        self.manifest = json.loads(
            (ROOT / "source_manifest.json").read_text(encoding="utf-8")
        )
        self.write_manifest()

    def write_manifest(self):
        self.manifest_path.write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )

    def copy_sources(self):
        records = [self.manifest["index_document"], *self.manifest["files"]]
        for record in records:
            relative = Path(record["local_path"])
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(self.script), *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def assert_failed(self, result, message):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("Input verification passed:", result.stdout)

    def test_help_works_without_source_files(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--verify-inputs", result.stdout)

    def test_verified_inputs_pass(self):
        self.copy_sources()
        result = self.run_cli("--verify-inputs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Input verification passed: 4 source documents.", result.stdout
        )

    def test_missing_manifest_stops(self):
        result = self.run_cli(
            "--verify-inputs", "--manifest", str(self.root / "missing.json")
        )
        self.assert_failed(result, "Input verification failed:")

    def test_missing_source_stops(self):
        result = self.run_cli("--verify-inputs")
        self.assert_failed(result, "Missing source document:")

    def test_unverified_manifest_stops(self):
        self.manifest["status"] = "pending"
        self.write_manifest()
        result = self.run_cli("--verify-inputs")
        self.assert_failed(result, "must have verified status")

    def test_unverified_source_stops(self):
        self.manifest["files"][-1]["status"] = "pending"
        self.write_manifest()
        result = self.run_cli("--verify-inputs")
        self.assert_failed(result, "Source is not marked verified:")

    def test_corrupted_source_stops(self):
        self.copy_sources()
        path = self.root / self.manifest["index_document"]["local_path"]
        data = bytearray(path.read_bytes())
        data[0] ^= 1
        path.write_bytes(data)
        result = self.run_cli("--verify-inputs")
        self.assert_failed(result, "SHA-256 mismatch:")


if __name__ == "__main__":
    unittest.main()
