"""Offline regressions for the combined package and CSV export workflow."""

from contextlib import redirect_stderr, redirect_stdout
import csv
import importlib.util
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
import pandas as pd

from sec_pipeline.cli import main


class IncomeExportTests(unittest.TestCase):
    def setUp(self):
        for target in ("httpx.Client.send", "httpx.AsyncClient.send"):
            guard = patch(target, side_effect=AssertionError("Unexpected HTTP request"))
            guard.start()
            self.addCleanup(guard.stop)
        environment = patch.dict(os.environ, {"EDGAR_IDENTITY": "Research User test@example.org"})
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "income.csv"
        self.filing = SimpleNamespace(
            accession_number="0000320193-25-000079",
            url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/0000320193-25-000079-index.html",
            form="10-K",
        )
        self.financials = Mock()
        self.financials.income_statement.return_value.to_dataframe.return_value = pd.DataFrame([
            {"concept": "us-gaap_Revenue", "label": "Revenue", "2025 (FY)": 123},
            {"concept": "us-gaap_NetIncomeLoss", "label": "Net income", "2025 (FY)": 45},
        ])
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def run_cli(self, approved=True):
        args = ["--ticker", "aapl", "--output", str(self.output)]
        if approved:
            args.append("--identity-approved")
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return main(args)

    def test_importing_legacy_entry_point_has_no_side_effects(self):
        entry = Path(__file__).resolve().parents[1] / "main.py"
        spec = importlib.util.spec_from_file_location("legacy_entry_point", entry)
        with patch("sec_pipeline.main") as command:
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
        command.assert_not_called()

    def test_explicit_approval_is_required_before_export(self):
        with patch("sec_pipeline.cli.export_income_statement") as export:
            self.assertEqual(self.run_cli(approved=False), 1)
        export.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_invalid_identity_is_rejected_before_export(self):
        for identity in ("", "test@example.org", "Name\r\nInjected: header test@example.org"):
            with self.subTest(identity=identity), \
                 patch.dict(os.environ, {"EDGAR_IDENTITY": identity}), \
                 patch("sec_pipeline.cli.export_income_statement") as export:
                self.assertEqual(self.run_cli(), 1)
                export.assert_not_called()

    def test_statement_and_provenance_use_the_same_filing(self):
        with patch("edgar.Company") as company, \
             patch("edgar.set_identity") as identity, \
             patch("edgar.financials.Financials.extract", return_value=self.financials) as extract:
            company.return_value.get_filings.return_value = [self.filing, object()]
            self.assertEqual(self.run_cli(), 0)
        company.assert_called_once_with("AAPL")
        company.return_value.get_filings.assert_called_once_with(form="10-K")
        company.return_value.get_financials.assert_not_called()
        extract.assert_called_once_with(self.filing)
        identity.assert_called_once_with("Research User test@example.org")
        with self.output.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["2025 (FY)"] for row in rows], ["123", "45"])
        for row in rows:
            self.assertEqual(row["accession_number"], self.filing.accession_number)
            self.assertEqual(row["filing_url"], self.filing.url)
            self.assertEqual(row["company"], "AAPL")
            self.assertEqual(row["form_type"], "10-K")
        self.assertNotIn("test@example.org", self.stdout.getvalue() + self.stderr.getvalue())

    def test_existing_output_is_preserved_before_network_setup(self):
        original = b"previous export\n"
        self.output.write_bytes(original)
        with patch("edgar.Company") as company, patch("edgar.set_identity") as identity:
            self.assertEqual(self.run_cli(), 1)
        company.assert_not_called()
        identity.assert_not_called()
        self.assertEqual(self.output.read_bytes(), original)

    def test_output_created_during_lookup_is_not_overwritten(self):
        def concurrent_output(filing):
            self.output.write_bytes(b"concurrent export\n")
            return self.financials

        with patch("edgar.Company") as company, patch("edgar.set_identity"), \
             patch("edgar.financials.Financials.extract", side_effect=concurrent_output):
            company.return_value.get_filings.return_value = [self.filing]
            self.assertEqual(self.run_cli(), 1)
        self.assertEqual(self.output.read_bytes(), b"concurrent export\n")

    def test_missing_filings_leave_no_output(self):
        with patch("edgar.Company") as company, patch("edgar.set_identity"), \
             patch("edgar.financials.Financials.extract") as extract:
            company.return_value.get_filings.return_value = []
            self.assertEqual(self.run_cli(), 1)
        extract.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_missing_or_empty_statement_leaves_no_output(self):
        for state in ("no financials", "no statement", "empty statement"):
            with self.subTest(state=state), patch("edgar.Company") as company, \
                 patch("edgar.set_identity"), patch("edgar.financials.Financials.extract") as extract:
                company.return_value.get_filings.return_value = [self.filing]
                extract.return_value = self.financials
                if state == "no financials":
                    extract.return_value = None
                elif state == "no statement":
                    self.financials.income_statement.return_value = None
                else:
                    self.financials.income_statement.return_value = Mock()
                    self.financials.income_statement.return_value.to_dataframe.return_value = pd.DataFrame()
                self.assertEqual(self.run_cli(), 1)
                self.assertFalse(self.output.exists())

    def test_failed_sec_request_leaves_no_output(self):
        with patch("edgar.Company", side_effect=httpx.ConnectError("Network unavailable")), \
             patch("edgar.set_identity"):
            self.assertEqual(self.run_cli(), 1)
        self.assertFalse(self.output.exists())
        self.assertIn("ConnectError", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
