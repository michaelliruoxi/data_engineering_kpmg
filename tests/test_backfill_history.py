"""Reviewed history extension, resumable discovery, and real source bindings."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sec_pipeline import acquisition, backfill
from tests.test_issuer_history import record
from tests.test_registrants import joint_fixture
from tests.test_filing_sources import save_fixture


def filing(cik=1, accession="0000000001-26-000001", *, filed="2026-04-30"):
    return {
        "cik": f"{cik:010d}", "company": f"Original issuer {cik}",
        "accession_number": accession, "form": "10-Q", "filing_date": filed,
        "reporting_period_end": "2026-03-31", "primary_document": "report.htm",
        "filing_index_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.html",
    }


class HistoryQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "queue.json"
        self.universe_path = self.root / "universe.json"
        self.universe_path.write_text("fixed membership snapshot", encoding="utf-8")
        self.universe = {"as_of": "2026-10-13", "companies": [
            {"cik": "0000000001", "tickers": ["FIRST"]},
            {"cik": "0000000004", "tickers": ["FOURTH"]},
        ]}
        for guard in (
            patch("sec_pipeline.backfill.load_snapshot", return_value=self.universe),
            patch("psycopg.connect", side_effect=AssertionError("Database access is forbidden in history queue tests")),
            patch("httpx.Client.send", side_effect=AssertionError("Network access is forbidden in history queue tests")),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def history(self, name, records, *, indent=None):
        path = self.root / name
        path.write_text(json.dumps({"schema_version": 1, "records": records}, indent=indent), encoding="utf-8")
        return path

    def open(self, history_path=None, *, extend=False):
        return backfill.open_state(self.path, self.universe_path, "2021-10-13", "2026-10-13",
                                   issuer_history_path=history_path, allow_history_extension=extend)

    def completed_state(self, state):
        source = filing()
        state["filings"][source["accession_number"]] = {"filing": source, "status": "ingested", "attempts": 2,
                                                        "result": {"existing_receipt": True}}
        state["companies"]["0000000001"] = {"status": "discovered", "filing_count": 1}
        state["last_run"] = {"mode": "ingest", "complete": True, "verified_successes": 1}
        backfill.save_state(self.path, state)
        return deepcopy(state["filings"])

    def test_initial_history_addition_requires_discovery_and_preserves_existing_work(self):
        state, _ = self.open()
        previous = self.completed_state(state)
        history = self.history("history.json", [record()])
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "explicit discovery"):
            self.open(history)
        self.assertEqual(self.path.read_bytes(), original)
        extended, universe = self.open(history, extend=True)
        self.assertEqual(extended["filings"], previous)
        self.assertEqual(extended["companies"]["0000000001"]["status"], "discovered")
        self.assertFalse(extended["last_run"]["complete"])
        self.assertEqual(extended["last_run"]["invalidated_by"], "issuer_history_extended")
        self.assertEqual(universe["history_jobs"][0]["cik"], "0000000002")
        self.assertEqual(universe["history_jobs"][0]["since"], "2021-10-13")
        self.assertEqual(universe["history_jobs"][0]["until"], "2024-01-01")
        resumed, same_universe = self.open()
        self.assertEqual(resumed, extended)
        self.assertEqual(same_universe["history_jobs"], universe["history_jobs"])

    def test_strict_superset_extension_keeps_prior_relationships_and_receipts(self):
        first = self.history("history-first.json", [record()])
        state, universe = self.open(first)
        previous = self.completed_state(state)
        key = universe["history_jobs"][0]["job_key"]
        state["historical_issuers"][key] = {"status": "discovered", "filing_count": 4}
        backfill.save_state(self.path, state)
        second = self.history("history-second.json", [record(), record(4, 5, "2025-06-01")])
        changed, members = self.open(second, extend=True)
        self.assertEqual(changed["filings"], previous)
        self.assertEqual(changed["historical_issuers"][key]["status"], "discovered")
        self.assertEqual(len(members["history_jobs"]), 2)
        self.assertFalse(changed["last_run"]["complete"])
        self.assertEqual(changed["issuer_history"]["path"], str(second.resolve()))

    def test_removed_or_rewritten_reviewed_relationship_cannot_extend_queue(self):
        first = self.history("history-first.json", [record(), record(4, 5, "2025-06-01")])
        self.open(first)
        original = self.path.read_bytes()
        for records in ([record()], [record() | {"effective_date": "2024-02-01"}, record(4, 5, "2025-06-01")]):
            with self.subTest(records=records):
                other = self.history("history-invalid.json", records)
                with self.assertRaisesRegex(ValueError, "preserve every existing"):
                    self.open(other, extend=True)
                self.assertEqual(self.path.read_bytes(), original)

    def test_extension_requires_untampered_previous_reviewed_file(self):
        first = self.history("history-first.json", [record()])
        self.open(first)
        original = self.path.read_bytes()
        first.write_text(first.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        second = self.history("history-second.json", [record(), record(4, 5, "2025-06-01")])
        with self.assertRaisesRegex(ValueError, "previous issuer-history source changed"):
            self.open(second, extend=True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_reformatted_equal_record_set_is_not_a_strict_superset(self):
        first = self.history("history-first.json", [record()])
        self.open(first)
        original = self.path.read_bytes()
        same = self.history("history-reformatted.json", [record()], indent=2)
        with self.assertRaisesRegex(ValueError, "strict|new relationship|superset"):
            self.open(same, extend=True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_equal_hash_relocation_is_saved_only_during_discovery(self):
        first = self.history("history-first.json", [record()])
        state, _ = self.open(first)
        self.completed_state(state)
        other = self.root / "history-relocated.json"
        other.write_bytes(first.read_bytes())
        original = self.path.read_bytes()
        inspected, _ = self.open(other)
        self.assertEqual(inspected["issuer_history"]["path"], str(first.resolve()))
        self.assertEqual(self.path.read_bytes(), original)
        relocated, _ = self.open(other, extend=True)
        self.assertEqual(relocated["issuer_history"]["path"], str(other.resolve()))
        self.assertTrue(relocated["last_run"]["complete"])

    def test_job_limit_is_shared_between_current_and_predecessor_issuers_and_resumes(self):
        history = self.history("history.json", [record(), record(4, 5, "2025-06-01")])
        state, universe = self.open(history)
        discover = Mock(return_value=[])
        with patch("sec_pipeline.acquisition.discover_filings", discover):
            first = backfill.discover_queue(self.path, state, universe, object(), company_limit=3, emit=lambda message: None)
            self.assertEqual([call.args[1] for call in discover.call_args_list], ["0000000001", "0000000004", "0000000002"])
            self.assertEqual(first["companies"], {"discovered": 2})
            self.assertEqual(first["predecessor_jobs"], {"discovered": 1})
            resumed, universe = self.open()
            second = backfill.discover_queue(self.path, resumed, universe, object(), company_limit=1, emit=lambda message: None)
        self.assertEqual(discover.call_args.args[1:], ("0000000005", "2021-10-13", "2025-06-01"))
        self.assertEqual(second["predecessor_jobs"], {"discovered": 2})
        self.assertEqual(discover.call_count, 4)

    def test_historical_access_block_checkpoints_and_stops_remaining_jobs(self):
        history = self.history("history.json", [record(), record(4, 5, "2025-06-01")])
        state, universe = self.open(history)
        state["companies"] = {company["cik"]: {"status": "discovered"} for company in universe["companies"]}
        discover = Mock(side_effect=acquisition.AccessBlocked("fixture block"))
        with patch("sec_pipeline.acquisition.discover_filings", discover):
            with self.assertRaises(acquisition.AccessBlocked):
                backfill.discover_queue(self.path, state, universe, object(), emit=lambda message: None)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(discover.call_count, 1)
        self.assertEqual(saved["historical_issuers"][universe["history_jobs"][0]["job_key"]]["status"], "access_blocked")
        self.assertNotIn(universe["history_jobs"][1]["job_key"], saved["historical_issuers"])

    def test_current_and_history_discovery_keep_one_accession_with_original_records(self):
        history = self.history("history.json", [record()])
        state, universe = self.open(history)
        current = filing(1, "0000000001-24-000001", filed="2024-01-01") | {"reporting_period_end": "2023-12-31"}
        predecessor = current | {"cik": "0000000002", "company": "Original predecessor",
                                 "filing_index_url": current["filing_index_url"].replace("/data/1/", "/data/2/")}
        calls = {"0000000001": [current], "0000000004": [], "0000000002": [predecessor]}
        with patch("sec_pipeline.acquisition.discover_filings", side_effect=lambda client, cik, start, stop: calls[cik]):
            summary = backfill.discover_queue(self.path, state, universe, object(), emit=lambda message: None)
        self.assertEqual(summary["filings_discovered"], 1)
        entry = state["filings"][current["accession_number"]]
        self.assertEqual(entry["filing"], current)
        self.assertEqual(entry["discovery_bindings"], [current, predecessor])
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["attempts"], 0)

    def test_critical_filing_metadata_conflicts_fail_without_changing_existing_work(self):
        state, _ = self.open()
        original = filing()
        backfill.remember_discovery(state, original)
        entry = state["filings"][original["accession_number"]]
        entry.update(status="ingested", attempts=3, result={"receipt": True})
        for field, value in (("form", "10-K"), ("filing_date", "2026-05-01"),
                             ("reporting_period_end", "2026-03-30"), ("primary_document", "different.htm"),
                             ("filing_index_url", "https://example.org/index.html")):
            with self.subTest(field=field):
                before = deepcopy(state)
                with self.assertRaises(ValueError):
                    backfill.remember_discovery(state, original | {field: value})
                self.assertEqual(state, before)
        before = deepcopy(state)
        backfill.remember_discovery(state, original | {"company": "Changed current SEC company name"})
        self.assertEqual(state, before)

    def test_new_binding_invalidates_success_but_duplicate_binding_does_not(self):
        state, _ = self.open()
        original, other = filing(1), filing(2)
        backfill.remember_discovery(state, original)
        entry = state["filings"][original["accession_number"]]
        entry.update(status="ingested", attempts=2)
        state["last_run"] = {"complete": True}
        backfill.remember_discovery(state, other)
        self.assertEqual(entry["status"], "pending")
        self.assertFalse(state["last_run"]["complete"])
        self.assertEqual(state["last_run"]["invalidated_by"], "new_registrant_binding")
        before = deepcopy(state)
        backfill.remember_discovery(state, other)
        self.assertEqual(state, before)

    def test_discovery_refresh_preserves_filing_receipts_and_resets_only_discovery(self):
        state, _ = self.open(self.history("history.json", [record()]))
        previous = self.completed_state(state)
        state["historical_issuers"]["old-job"] = {"status": "failed", "cik": "0000000002", "error_type": "ValueError"}
        backfill.refresh_discovery(self.path, state)
        self.assertEqual(state["filings"], previous)
        self.assertEqual(state["companies"]["0000000001"]["status"], "pending")
        self.assertEqual(state["historical_issuers"]["old-job"]["status"], "pending")
        self.assertFalse(state["last_run"]["complete"])
        self.assertEqual(state["last_run"]["invalidated_by"], "discovery_refresh")


class RealBindingQueueTests(unittest.TestCase):
    def test_two_discovered_registrants_use_real_saved_index_and_dei_before_completion(self):
        manifest, secondary, payloads = joint_fixture()
        primary = secondary | {"cik": manifest["cik"], "company": manifest["company"], "filing_index_url": manifest["filing_index_url"]}
        with tempfile.TemporaryDirectory() as temporary, patch("psycopg.connect", side_effect=AssertionError("No database")), \
                patch("httpx.Client.send", side_effect=AssertionError("No network")):
            root = Path(temporary)
            save_fixture(root, manifest, payloads)
            directory = (root / manifest["index_document"]["local_path"]).parent
            (directory / "source_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            state = {"schema_version": 1, "configuration": {}, "companies": {}, "filings": {}}
            backfill.remember_discovery(state, secondary)
            backfill.remember_discovery(state, primary)
            path = root / "queue.json"
            self.assertEqual(backfill.process_queue(path, state, None, root, mode="download", emit=lambda message: None), 1)
            entry = state["filings"][manifest["accession_number"]]
            self.assertEqual(entry["status"], "downloaded")
            self.assertTrue(state["last_run"]["complete"])
            self.assertEqual(entry["filing"]["cik"], secondary["cik"])
            saved_manifest = (directory / "source_manifest.json").read_bytes()
            unknown = secondary | {"cik": "0000000044", "company": "Unlisted issuer",
                                   "filing_index_url": secondary["filing_index_url"].replace("/data/43/", "/data/44/")}
            backfill.remember_discovery(state, unknown)
            self.assertFalse(state["last_run"]["complete"])
            self.assertEqual(backfill.process_queue(path, state, None, root, mode="download", emit=lambda message: None), 1)
            self.assertEqual(entry["status"], "failed")
            self.assertFalse(state["last_run"]["complete"])
            self.assertEqual((directory / "source_manifest.json").read_bytes(), saved_manifest)


if __name__ == "__main__":
    unittest.main()
