"""Opt-in real XML/HTML bridge checks. Storage is recorded, never live SQL.

Run with SEC_FACTS_BRIDGE_SAMPLE=1 after PR #7's fact_extraction module is present.
An explicit run fails if that module is missing; it cannot skip the dependency.
"""

from contextlib import redirect_stdout
from dataclasses import replace
from decimal import Decimal
import hashlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from process_filing import verified_inputs
from sec_pipeline.facts_bridge import fact_ids_for_tables, parse_facts, write_facts
from sec_pipeline.processing import DryRunContext, SourceIds


class FactsBridgeSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("SEC_FACTS_BRIDGE_SAMPLE") != "1":
            raise unittest.SkipTest("Opt in with SEC_FACTS_BRIDGE_SAMPLE=1 and include PR #7's module.")
        from sec_pipeline import fact_extraction

        cls.facts_module = fact_extraction
        cls.enterClassContext(patch("psycopg.connect", side_effect=AssertionError("Sample check must not connect")))
        cls.enterClassContext(patch("httpx.Client.send", side_effect=AssertionError("Sample check must stay offline")))
        cls.enterClassContext(patch("urllib.request.urlopen", side_effect=AssertionError("Sample check must stay offline")))
        with redirect_stdout(io.StringIO()):
            cls.manifest = verified_inputs(ROOT / "source_manifest.json", ROOT)
        records = [cls.manifest["index_document"], *cls.manifest["files"]]
        cls.paths = [ROOT / "source_manifest.json", *(ROOT / record["local_path"] for record in records)]
        cls.initial_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in cls.paths}
        cls.addClassCleanup(cls.check_unchanged)
        cls.context = DryRunContext(
            manifest=cls.manifest, data_root=ROOT,
            source_ids=SourceIds(UUID(int=1), UUID(int=2), UUID(int=3)), report_id=UUID(int=4),
            versions={"facts": "facts-v1", "tables": "not-run", "reports": "not-run", "chunks": "paragraph-v1"},
            outputs={},
        )

    @classmethod
    def check_unchanged(cls):
        if {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in cls.paths} != cls.initial_hashes:
            raise AssertionError("A supplied source file or manifest changed during the bridge checks.")

    def test_real_bridge_extracts_all_facts_and_matches_original_inline_tags(self):
        result = parse_facts(self.context)
        self.assertEqual((len(result.items), len(result.rejected)), (1622, 0))
        self.assertTrue(all(record.filing_id == UUID(int=1) and record.source_document_id == UUID(int=2)
                            and record.extraction_version == "facts-v1" for record in result.items))
        html = next(record for record in self.manifest["files"] if record["role"] == "main_inline_xbrl_filing")
        comparison = self.facts_module.check_inline_correspondence(ROOT / html["local_path"], result.items)
        self.assertEqual(comparison["matched"], 1622)
        self.assertEqual(comparison["numeric_values_equal"], 1426)
        self.assertEqual(comparison["numeric_values_not_compared"], 0)
        self.assertEqual(comparison["mismatches"], [])
        self.assertEqual(comparison["inline_ids_without_fact"], [])

    def test_real_extraction_write_bridge_and_table_id_mapping_use_recorded_storage(self):
        conn = object()
        storage_calls = []

        def record_storage(connection, facts):
            self.assertIs(connection, conn)
            storage_calls.append(facts)
            mapping = {fact.occurrence_key: UUID(int=10000 + index) for index, fact in enumerate(facts)}
            return self.facts_module.StoredFacts(
                filing_id=UUID(int=1), source_document_id=UUID(int=2), extraction_version="facts-v1",
                fact_ids=dict(reversed(list(mapping.items()))),
            )

        with patch("sec_pipeline.fact_extraction.store_facts", side_effect=record_storage):
            result = write_facts(conn, self.context)
        self.assertEqual(len(storage_calls), 1)
        self.assertEqual(len(result.parsed.items), 1622)
        self.assertEqual(result.stored_ids, tuple(UUID(int=10000 + index) for index in range(1622)))
        mapping = fact_ids_for_tables(replace(self.context, outputs={"facts": result}))
        self.assertEqual(len(mapping), 1622)
        revenue = [fact for fact in result.parsed.items if fact.numeric_value == Decimal("82886000000")]
        self.assertEqual(len(revenue), 4)
        self.assertEqual(len({mapping[fact.occurrence_key] for fact in revenue}), 4)


if __name__ == "__main__":
    unittest.main()
