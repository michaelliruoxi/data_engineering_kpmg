"""Fact coverage across archived filings: offline, read-only, missing filings stay visible."""

import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from sec_pipeline import fact_coverage as cov
from tests.test_fact_extraction import DEFINITIONS, EX, HEAD, INLINE, INLINE_FACTS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / "config" / "shortlist.json"
SAMPLE_ACCESSION = "0001193125-26-191507"
DEI = "http://xbrl.sec.gov/dei/2025"
# The shared inline fixture without its deliberately unmatched HTML-only tag.
MATCHED_INLINE = re.sub(r'<td><ix:nonFraction id="html_only".*?</ix:nonFraction></td>', "", INLINE, flags=re.S)


def discovery(*extra, status="discovered"):
    scope = cov.load_scope(CONFIG)
    companies = {c["ticker"]: {"status": "discovered", "filings": []} for c in scope["companies"]}
    companies["MSFT"]["filings"] = [{"cik": "0000789019", "form": "10-Q", "accession_number": SAMPLE_ACCESSION,
                                     "filing_date": "2026-04-29", "reporting_period_end": "2026-03-31"}, *extra]
    companies["MSFT"]["status"] = status
    return {"scope_key": cov.shortlist_scope_key(scope), "discovery_complete": True, "companies": companies}


class SampleCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = cov.assess_archives(cov.load_scope(CONFIG), [PROJECT_ROOT])

    def test_supplied_microsoft_filing_reconciles_with_its_inventory(self):
        self.assertEqual(self.report["status"], "complete_for_supplied_archives")
        [filing] = self.report["filings"]
        self.assertEqual((filing["ticker"], filing["form"], filing["accession_number"], filing["status"]),
                         ("MSFT", "10-Q", SAMPLE_ACCESSION, "passed"))
        self.assertEqual(filing["facts"]["fact_occurrences"], 1622)
        self.assertEqual((filing["facts"]["extracted"], filing["facts"]["failed"]), (1622, 0))
        self.assertEqual((filing["facts"]["extracted_numeric"], filing["facts"]["extracted_nil"],
                          filing["facts"]["extracted_text"]), (1426, 2, 194))
        self.assertEqual(filing["periods"]["by_kind"], {"duration": 827, "instant": 795})
        self.assertEqual(filing["periods"]["report_period"], {"start": "2025-07-01", "end": "2026-03-31"})
        self.assertEqual((filing["periods"]["fiscal_year_focus"], filing["periods"]["fiscal_period_focus"]),
                         ("2026", "Q3"))
        self.assertEqual(filing["units"]["by_unit"], {"USD": 1307, "pure": 55, "shares": 35, "USD/shares": 29,
                                                      "EUR": 1, "Segment": 1})
        self.assertEqual(set(filing["core_concepts"].values()), {"exact"})
        self.assertEqual(filing["inline_correspondence"]["numeric_values_equal"], 1426)
        self.assertEqual(filing["labels"]["source"], "extension_schema")
        self.assertEqual(filing["footnotes_not_extracted"], 3)

    def test_company_rollup_requires_both_forms(self):
        msft = next(c for c in self.report["companies"] if c["ticker"] == "MSFT")
        self.assertEqual((msft["usable_10k"], msft["usable_10q"], msft["has_usable_10k_and_10q"]), (0, 1, False))
        self.assertEqual(self.report["filings_by_year"]["MSFT"], {"2026": {"10-Q": 1}})
        self.assertTrue(all(c["filings"] == 0 for c in self.report["companies"] if c["ticker"] != "MSFT"))

    def test_discovered_filings_without_archives_are_reported_missing(self):
        extra = {"cik": "0000789019", "form": "10-K", "accession_number": "0001193125-26-323660",
                 "filing_date": "2026-07-29", "reporting_period_end": "2026-06-30"}
        report = cov.assess_archives(cov.load_scope(CONFIG), [PROJECT_ROOT], discovery(extra))
        self.assertEqual(report["status"], "review_required")
        statuses = {f["accession_number"]: f["status"] for f in report["filings"]}
        self.assertEqual(statuses, {SAMPLE_ACCESSION: "passed", "0001193125-26-323660": "missing_archive"})
        with self.assertRaises(cov.CoverageError):
            cov.assess_archives(cov.load_scope(CONFIG), [PROJECT_ROOT], discovery(status="discovery_failed"))

    def test_discovery_requires_complete_snapshot_bound_to_the_unfiltered_scope(self):
        scope = cov.load_scope(CONFIG)
        prior = scope | {"until": "2022-12-31"}
        bad = discovery() | {"scope_key": cov.shortlist_scope_key(prior)}
        for snapshot in (bad, discovery() | {"discovery_complete": False}):
            with self.subTest(snapshot=snapshot), self.assertRaises(cov.CoverageError):
                cov.assess_archives(scope, [], snapshot, tickers=["MSFT"])
        del bad["scope_key"]
        with self.assertRaisesRegex(cov.CoverageError, "scope"):
            cov.assess_archives(scope, [], bad)

    def test_discovery_rejects_unusable_rows_and_archive_identity_disagreement(self):
        scope = cov.load_scope(CONFIG)
        for field, value in (("cik", "0000320193"), ("filing_date", "2020-01-01"),
                             ("reporting_period_end", "2026-03-30")):
            snapshot = discovery()
            snapshot["companies"]["MSFT"]["filings"][0][field] = value
            with self.subTest(field=field), self.assertRaises(cov.CoverageError):
                cov.assess_archives(scope, [PROJECT_ROOT], snapshot)

    def test_changed_source_bytes_are_an_archive_error_not_a_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(PROJECT_ROOT / "source_manifest.json", root / "source_manifest.json")
            shutil.copytree(PROJECT_ROOT / "data/raw/sec" / SAMPLE_ACCESSION, root / "data/raw/sec" / SAMPLE_ACCESSION)
            xml = root / "data/raw/sec" / SAMPLE_ACCESSION / "msft-20260331_htm.xml"
            xml.write_bytes(xml.read_bytes().replace(b"82886000000", b"82886000001", 1))
            report = cov.assess_archives(cov.load_scope(CONFIG), [root])
            for roots in ([root, PROJECT_ROOT], [PROJECT_ROOT, root]):
                with self.subTest(roots=roots):
                    duplicate = cov.assess_archives(cov.load_scope(CONFIG), roots)
                    self.assertEqual(duplicate["totals"]["passed"], 1)
        [filing] = report["filings"]
        self.assertEqual(filing["status"], "archive_error")
        self.assertIn("source verification failed", filing["problems"][0])

    def test_conflicting_verified_copies_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = json.loads((PROJECT_ROOT / "source_manifest.json").read_text(encoding="utf-8"))
            shutil.copytree(PROJECT_ROOT / "data/raw/sec" / SAMPLE_ACCESSION, root / "data/raw/sec" / SAMPLE_ACCESSION)
            record = manifest["index_document"]
            path = root / record["local_path"]
            changed = path.read_bytes() + b"\n"
            path.write_bytes(changed)
            record.update(size_bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest())
            (root / "source_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(cov.CoverageError, "Conflicting verified"):
                cov.assess_archives(cov.load_scope(CONFIG), [PROJECT_ROOT, root])

    def test_unreadable_manifest_or_nonexistent_root_is_a_setup_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = cov.load_scope(CONFIG)
            with self.assertRaisesRegex(cov.CoverageError, "No source manifests"):
                cov.assess_archives(scope, [PROJECT_ROOT, root])
            report = cov.assess_archives(scope, [root], discovery())
            self.assertEqual(report["totals"]["missing_archive"], 1)
            (root / "source_manifest.json").write_text("{", encoding="utf-8")
            for invalid in (root, root / "typo"):
                with self.subTest(invalid=invalid), self.assertRaises(cov.CoverageError):
                    cov.assess_archives(scope, [PROJECT_ROOT, invalid])

    def test_scope_window_excludes_filings_outside_it(self):
        scope = cov.load_scope(CONFIG) | {"since": "2026-05-01"}
        empty = cov.assess_archives(scope, [PROJECT_ROOT])
        self.assertEqual(empty["filings"], [])
        self.assertEqual(empty["status"], "review_required")


class FilingAssessmentTests(unittest.TestCase):
    """assess_filing on synthetic archives shaped like 2021-era filings."""

    def assess(self, xml_body, html, *, form="10-Q", period_end="2026-03-31"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "instance.xml").write_text(HEAD.replace("SCHEMA", "https://example.invalid/ex.xsd")
                                               .replace('xmlns:ex=', f'xmlns:dei="{DEI}" xmlns:ex=')
                                               + DEFINITIONS + xml_body + "</xbrli:xbrl>", encoding="utf-8")
            (root / "filing.htm").write_text(html, encoding="utf-8")
            manifest = {"cik": "0000000001", "form": form, "accession_number": "0000000001-21-000001",
                        "filing_date": "2021-04-29", "reporting_period_end": period_end,
                        "files": [{"role": "main_inline_xbrl_filing", "local_path": "filing.htm", "sha256": "h"},
                                  {"role": "extracted_xbrl_instance", "local_path": "instance.xml", "sha256": "x"}]}
            return cov.assess_filing(manifest, root, "TEST")

    def test_registry_three_filing_is_compared_and_reviewed_for_missing_identity(self):
        older = (MATCHED_INLINE.replace("2022-02-16", "2015-02-26").replace("ixt:num-dot-decimal", "ixt:numdotdecimal")
                 .replace('format="ixt:numdotdecimal">4.27</', 'format="ixt:zerodash">—</'))
        result = self.assess(INLINE_FACTS.replace(">4.27</", ">0</"), older)
        self.assertEqual(result["inline_correspondence"]["numeric_values_not_compared"], 0)
        self.assertEqual(result["inline_correspondence"]["numeric_values_equal"], 3)
        # No DEI facts and no statement anchors: extractable, but flagged for review.
        self.assertEqual(result["status"], "review")
        self.assertIn("dei:DocumentType None differs from form 10-Q", result["review"])
        self.assertEqual(result["labels"]["facts_with_label"], 0)
        self.assertIn("separate label linkbase", result["labels"]["source"])

    def test_identity_and_anchors_pass_when_present(self):
        body = INLINE_FACTS + f"""
            <dei:DocumentType contextRef="ytd" id="d1">10-Q</dei:DocumentType>
            <dei:DocumentPeriodEndDate contextRef="ytd" id="d2">2026-03-31</dei:DocumentPeriodEndDate>"""
        html = MATCHED_INLINE.replace("</body>", '<ix:nonNumeric id="d1" name="dei:DocumentType" contextRef="ytd">10-Q</ix:nonNumeric>'
                              '<ix:nonNumeric id="d2" name="dei:DocumentPeriodEndDate" contextRef="ytd">March 31, 2026</ix:nonNumeric></body>'
                              ).replace('xmlns:ex=', f'xmlns:dei="{DEI}" xmlns:ex=')
        result = self.assess(body, html)
        self.assertEqual(result["dei"]["DocumentType"], "10-Q")
        self.assertEqual(result["periods"]["report_period"], {"start": "2025-07-01", "end": "2026-03-31"})
        self.assertEqual(result["problems"], [])
        # Only the anchors are missing in this synthetic filing.
        self.assertEqual(len(result["review"]), 1)
        self.assertTrue(result["review"][0].startswith("no undimensioned fact at the report period end"))
        wrong = self.assess(body, html, period_end="2026-02-28")
        self.assertTrue(any("differs from the SEC report date 2026-02-28" in r for r in wrong["review"]))

    def test_anchors_require_a_dated_fact_at_the_declared_report_end(self):
        gaap = 'xmlns:gaap="http://fasb.org/us-gaap/2025"'
        body = INLINE_FACTS + f"""
            <dei:DocumentType contextRef="ytd" id="d1">10-Q</dei:DocumentType>
            <dei:DocumentPeriodEndDate contextRef="ytd" id="d2">2026-04-01</dei:DocumentPeriodEndDate>
            <gaap:Assets {gaap} contextRef="day" id="a1" unitRef="usd">1</gaap:Assets>
            <gaap:Revenues {gaap} contextRef="always" id="a2" unitRef="usd">1</gaap:Revenues>"""
        html = MATCHED_INLINE.replace('xmlns:ex=', f'xmlns:dei="{DEI}" {gaap} xmlns:ex=').replace(
            "</body>", '<ix:nonNumeric id="d1" name="dei:DocumentType" contextRef="ytd">10-Q</ix:nonNumeric>'
            '<ix:nonNumeric id="d2" name="dei:DocumentPeriodEndDate" contextRef="ytd">April 1, 2026</ix:nonNumeric>'
            '<ix:nonFraction id="a1" name="gaap:Assets" contextRef="day" unitRef="usd">1</ix:nonFraction>'
            '<ix:nonFraction id="a2" name="gaap:Revenues" contextRef="always" unitRef="usd">1</ix:nonFraction></body>')
        result = self.assess(body, html, period_end="2026-04-01")
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["core_concepts"]["assets"], "off_1d")
        self.assertEqual(result["core_concepts"]["revenue"], "missing")
        self.assertTrue(any("DocumentType context end" in reason for reason in result["review"]))
        self.assertTrue(any("assets" in reason for reason in result["review"]))

    def test_irregular_digit_grouping_needs_review(self):
        result = self.assess(INLINE_FACTS, MATCHED_INLINE.replace("11,351", "1,13,51"))
        self.assertEqual(result["problems"], [])
        self.assertTrue(any("irregular digit grouping" in r and "'1,13,51'" in r for r in result["review"]))

    def test_unsupported_inline_format_fails_the_filing(self):
        unsupported = MATCHED_INLINE.replace('format="ixt:num-dot-decimal">4.27</', 'format="ixt:num-comma-decimal">4,27</')
        result = self.assess(INLINE_FACTS, unsupported)
        self.assertEqual(result["status"], "failed")
        self.assertIn("1 numeric value(s) use an unsupported inline format and were not compared", result["problems"])

    def test_unreadable_instance_fails_with_a_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "instance.xml").write_text("<not-xbrl/>", encoding="utf-8")  # well-formed, not XBRL
            (root / "filing.htm").write_text(INLINE, encoding="utf-8")
            manifest = {"cik": "0000000001", "form": "10-K", "accession_number": "0000000001-21-000002",
                        "filing_date": "2021-06-01", "files": [
                            {"role": "main_inline_xbrl_filing", "local_path": "filing.htm", "sha256": "h"},
                            {"role": "extracted_xbrl_instance", "local_path": "instance.xml", "sha256": "x"}]}
            result = cov.assess_filing(manifest, root, "TEST")
        self.assertEqual(result["status"], "failed")
        self.assertIn("extraction stopped", result["problems"][0])


    def test_malformed_html_fails_with_a_reason(self):
        result = self.assess(INLINE_FACTS, "<html><body><td>broken</body></html>")
        self.assertEqual(result["status"], "failed")
        self.assertIn("inline HTML cannot be checked", result["problems"][0])


class CommandTests(unittest.TestCase):
    def run_main(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                patch("psycopg.connect", side_effect=AssertionError("coverage opened a database connection")):
            code = cov.main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def test_writes_three_files_once_and_exits_by_completeness(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "coverage"
            code, out, _ = self.run_main("--config", str(CONFIG), "--archive-root", str(PROJECT_ROOT),
                                         "--output", str(output))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["facts_extracted"], 1622)
            self.assertEqual(sorted(p.name for p in output.iterdir()), ["coverage.csv", "coverage.json", "coverage.md"])
            rows = (output / "coverage.csv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[1].startswith(f"MSFT,10-Q,2026-04-29,{SAMPLE_ACCESSION},passed,1622,1622,0,827,795,0,6,"))
            self.assertIn("| MSFT | 1 | 1 | 0 | 0 | 0 | 0 | 1 | 1,622 |", (output / "coverage.md").read_text())
            code, _, err = self.run_main("--archive-root", str(PROJECT_ROOT), "--output", str(output))
            self.assertEqual(code, 1)
            self.assertIn("not empty", err)

            missing = Path(directory) / "discovery.json"
            missing.write_text(json.dumps(discovery({"cik": "0000789019", "form": "10-K",
                                                     "accession_number": "0001193125-26-323660",
                                                     "filing_date": "2026-07-29",
                                                     "reporting_period_end": "2026-06-30"})), encoding="utf-8")
            code, out, _ = self.run_main("--archive-root", str(PROJECT_ROOT), "--discovery", str(missing),
                                         "--ticker", "MSFT", "--output", str(Path(directory) / "second"))
            self.assertEqual(code, 2)
            self.assertEqual((json.loads(out)["passed"], json.loads(out)["missing_archive"]), (1, 1))

    def test_rejects_unknown_tickers(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _, err = self.run_main("--archive-root", str(PROJECT_ROOT), "--ticker", "ZZZZ",
                                         "--output", str(Path(directory) / "out"))
        self.assertEqual(code, 1)
        self.assertIn("ZZZZ", err)


if __name__ == "__main__":
    unittest.main()
