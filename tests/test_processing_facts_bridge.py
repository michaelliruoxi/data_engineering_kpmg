"""Facts bridge contracts with recorder parser/storage calls; no live SQL."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_pipeline import facts_bridge as bridge
from sec_pipeline.database import FactInput
from sec_pipeline.processing import (
    DryRunContext, ParseResult, PipelineContext, ProcessingError,
    SourceIds, WriteResult, store_ingestion_chunks,
)


class FactsBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = json.loads((ROOT / "source_manifest.json").read_text())
        self.context = PipelineContext(
            manifest=self.manifest, data_root=self.root,
            source_ids=SourceIds(UUID(int=10), UUID(int=11), UUID(int=12)),
            versions={"facts": "facts-v1", "tables": "not-run", "reports": "not-run", "chunks": "paragraph-v1"},
            outputs={},
        )
        self.facts = [FactInput(
            filing_id=self.context.source_ids.filing_id,
            source_document_id=self.context.source_ids.xml_document_id,
            extraction_version="facts-v1", occurrence_key=key,
            concept_namespace="https://example.invalid/fixture", concept_name="SameValue",
            numeric_value=Decimal("100"), period_kind="instant", instant_date=date(2026, 3, 31),
            source_reference={"element_id": key},
        ) for key in ("F_A", "F_B")]
        self.extraction = SimpleNamespace(facts=self.facts, failures=[])
        self.extraction.require_complete = Mock(return_value=self.extraction)
        self.stored = SimpleNamespace(
            filing_id=self.context.source_ids.filing_id,
            source_document_id=self.context.source_ids.xml_document_id,
            extraction_version="facts-v1", fact_ids={"F_B": UUID(int=202), "F_A": UUID(int=201)},
        )
        self.parser = Mock(return_value=self.extraction)
        self.storage = Mock(return_value=self.stored)
        module = ModuleType("sec_pipeline.fact_extraction")
        module.extract_facts, module.store_facts = self.parser, self.storage
        dependency = patch.dict(sys.modules, {module.__name__: module})
        dependency.start()
        self.addCleanup(dependency.stop)
        connection = patch("psycopg.connect", side_effect=AssertionError("Bridge must not connect"))
        self.pg_connect = connection.start()
        self.addCleanup(connection.stop)

    def test_parse_uses_manifest_xml_registered_source_ids_and_facts_v1(self):
        result = bridge.parse_facts(self.context)
        xml = next(r for r in self.manifest["files"] if r["role"] == bridge.XML_ROLE)
        self.parser.assert_called_once_with(
            (self.root / xml["local_path"]).resolve(),
            filing_id=self.context.source_ids.filing_id,
            source_document_id=self.context.source_ids.xml_document_id,
            extraction_version="facts-v1",
        )
        self.assertEqual(result.items, tuple(self.facts))
        self.storage.assert_not_called()
        self.pg_connect.assert_not_called()

    def test_dry_stage_preserves_rejections_without_storage_or_require_complete(self):
        self.extraction.failures = ["fixture unsupported occurrence"]
        dry = DryRunContext(
            manifest=self.context.manifest, data_root=self.context.data_root,
            source_ids=self.context.source_ids, versions=self.context.versions,
            outputs={}, report_id=UUID(int=13),
        )
        result = bridge.parse_facts(dry)
        self.assertEqual(result.rejected, tuple(self.extraction.failures))
        self.extraction.require_complete.assert_not_called()
        self.storage.assert_not_called()
        self.pg_connect.assert_not_called()

    def test_write_requires_complete_and_keeps_ids_in_record_order(self):
        conn = object()
        result = bridge.write_facts(conn, self.context)
        self.extraction.require_complete.assert_called_once_with()
        self.storage.assert_called_once_with(conn, self.facts)
        self.assertEqual(result.stored_ids, (UUID(int=201), UUID(int=202)))
        self.assertEqual(result.parsed.items, tuple(self.facts))
        self.pg_connect.assert_not_called()

    def test_incomplete_extraction_stops_before_any_storage(self):
        self.extraction.require_complete.side_effect = ValueError("fixture extraction failure")
        with self.assertRaisesRegex(ValueError, "fixture extraction failure"):
            bridge.write_facts(object(), self.context)
        self.storage.assert_not_called()

    def test_other_or_missing_facts_version_stops_before_parser_and_storage(self):
        for version in ("facts-v2", None, ""):
            with self.subTest(version=version):
                context = replace(self.context, versions=dict(self.context.versions) | {"facts": version})
                for operation in (lambda: bridge.parse_facts(context), lambda: bridge.write_facts(object(), context)):
                    with self.assertRaisesRegex(ProcessingError, "facts-v1"):
                        operation()
        self.parser.assert_not_called()
        self.storage.assert_not_called()

    def test_manifest_file_order_does_not_change_xml_binding(self):
        self.manifest["files"].reverse()
        bridge.parse_facts(self.context)
        self.assertTrue(str(self.parser.call_args.args[0]).endswith("msft-20260331_htm.xml"))

    def test_duplicate_missing_or_escaping_xml_paths_stop_before_parser(self):
        xml = next(r for r in self.manifest["files"] if r["role"] == bridge.XML_ROLE)
        others = [r for r in self.manifest["files"] if r["role"] != bridge.XML_ROLE]
        for records in (others, [*others, xml, dict(xml)], [*others, dict(xml, local_path="../outside.xml")],
                        [*others, dict(xml, local_path="/outside.xml")]):
            with self.subTest(records=records):
                context = replace(self.context, manifest=dict(self.manifest) | {"files": records})
                with self.assertRaises(ProcessingError):
                    bridge.parse_facts(context)
        self.parser.assert_not_called()

    def test_wrong_stored_filing_source_and_version_are_rejected(self):
        for name, value in (("filing_id", UUID(int=999)), ("source_document_id", UUID(int=999)),
                            ("extraction_version", "facts-v2")):
            with self.subTest(field=name):
                stored = SimpleNamespace(**vars(self.stored))
                setattr(stored, name, value)
                self.storage.return_value = stored
                with self.assertRaisesRegex(ProcessingError, "different filing"):
                    bridge.write_facts(object(), self.context)

    def test_missing_or_extra_returned_fact_keys_are_rejected(self):
        for ids in ({"F_A": UUID(int=201)}, dict(self.stored.fact_ids, F_C=UUID(int=203))):
            with self.subTest(ids=ids):
                self.storage.return_value = SimpleNamespace(**(vars(self.stored) | {"fact_ids": ids}))
                with self.assertRaisesRegex(ProcessingError, "exactly the accepted occurrence keys"):
                    bridge.write_facts(object(), self.context)

    def test_same_amounts_resolve_by_inline_id_to_distinct_current_database_uuids(self):
        result = bridge.write_facts(object(), self.context)
        context = replace(self.context, outputs={"facts": result})
        mapping = bridge.fact_ids_for_tables(context)
        self.assertEqual(mapping, {"F_A": UUID(int=201), "F_B": UUID(int=202)})
        self.assertNotEqual(mapping["F_A"], mapping["F_B"])
        self.assertIsNone(mapping.get("missing-inline-id"))

    def test_tables_cannot_use_pure_dry_run_outputs_as_stored_ids(self):
        context = replace(self.context, outputs={"facts": ParseResult(self.facts)})
        with self.assertRaisesRegex(ProcessingError, "preceding facts stage"):
            bridge.fact_ids_for_tables(context)

    def test_tables_reject_other_filing_source_and_version_records(self):
        for change in ({"filing_id": UUID(int=999)}, {"source_document_id": UUID(int=12)},
                       {"extraction_version": "facts-v2"}):
            with self.subTest(change=change):
                records = [replace(self.facts[0], **change), self.facts[1]]
                context = replace(self.context, outputs={"facts": WriteResult(ParseResult(records), [UUID(int=201), UUID(int=202)])})
                with self.assertRaisesRegex(ProcessingError, "this filing, XML source"):
                    bridge.fact_ids_for_tables(context)

    def test_duplicate_keys_or_disagreeing_inline_provenance_cannot_link(self):
        for records in ([self.facts[0], self.facts[0]], [replace(self.facts[0], source_reference={"element_id": "different"}), self.facts[1]]):
            with self.subTest(records=records):
                context = replace(self.context, outputs={"facts": WriteResult(ParseResult(records), [UUID(int=201), UUID(int=202)])})
                with self.assertRaises(ProcessingError):
                    bridge.fact_ids_for_tables(context)

    def test_plan_builders_wire_real_facts_and_chunks_without_invoking_stages(self):
        stages = {name: Mock() for name in ("tables", "report", "validate")}
        pure = bridge.build_dry_run_plan(**stages, required_checks=("facts",))
        write = bridge.build_ingestion_plan(**stages, required_checks=("facts",))
        self.assertIs(pure.stages.facts, bridge.parse_facts)
        self.assertIs(write.stages.facts, bridge.write_facts)
        self.assertIs(write.stages.chunks, store_ingestion_chunks)
        for stage in stages.values():
            stage.assert_not_called()
        self.parser.assert_not_called()
        self.storage.assert_not_called()

    def test_plan_builders_cannot_skip_missing_tables_report_or_validation(self):
        for name in ("tables", "report", "validate"):
            for builder in (bridge.build_dry_run_plan, bridge.build_ingestion_plan):
                with self.subTest(stage=name, builder=builder):
                    stages = {key: Mock() for key in ("tables", "report", "validate")}
                    stages[name] = None
                    with self.assertRaisesRegex(ProcessingError, "Supply the table"):
                        builder(**stages, required_checks=("facts",))


if __name__ == "__main__":
    unittest.main()
