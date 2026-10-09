"""Offline tests for mapping verified sources to registered IDs."""

import copy
import json
from pathlib import Path
import sys
import unittest
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_pipeline.processing import ProcessingError, resolve_source_ids


class SourceIdTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(
            (ROOT / "source_manifest.json").read_text(encoding="utf-8")
        )
        self.records = [self.manifest["index_document"], *self.manifest["files"]]
        self.filing_id = UUID("17d1109f-c498-477a-9219-e4ecb2bc86f5")
        self.ids = [UUID(int=i + 1) for i in range(len(self.records))]
        self.seed = {
            "filing_id": self.filing_id,
            "document_ids": {
                record["local_path"]: value
                for record, value in zip(self.records, self.ids)
            },
        }
        self.xml_path = self.manifest["files"][1]["local_path"]
        self.html_path = self.manifest["files"][0]["local_path"]

    def test_real_manifest_maps_xml_and_html_to_separate_registered_ids(self):
        result = resolve_source_ids(self.manifest, self.seed)
        self.assertEqual(result.filing_id, self.filing_id)
        self.assertEqual(result.xml_document_id, self.ids[2])
        self.assertEqual(result.html_document_id, self.ids[1])

    def test_file_order_does_not_change_bindings(self):
        self.manifest["files"].reverse()
        result = resolve_source_ids(self.manifest, self.seed)
        self.assertEqual(result.xml_document_id, self.ids[2])
        self.assertEqual(result.html_document_id, self.ids[1])

    def test_serialized_uuids_are_normalized(self):
        self.seed["filing_id"] = str(self.filing_id)
        self.seed["document_ids"] = {
            path: str(value) for path, value in self.seed["document_ids"].items()
        }
        result = resolve_source_ids(self.manifest, self.seed)
        self.assertEqual(result.xml_document_id, self.ids[2])
        self.assertIsInstance(result.filing_id, UUID)

    def test_role_keyed_ids_cannot_be_used_as_path_keyed_ids(self):
        self.seed["document_ids"] = {
            record["role"]: value for record, value in zip(self.records, self.ids)
        }
        with self.assertRaisesRegex(ProcessingError, "No registered ID"):
            resolve_source_ids(self.manifest, self.seed)

    def test_missing_registration_fails(self):
        del self.seed["document_ids"][self.xml_path]
        with self.assertRaisesRegex(ProcessingError, "No registered ID"):
            resolve_source_ids(self.manifest, self.seed)

    def test_duplicate_xml_role_fails(self):
        self.manifest["files"].append(copy.deepcopy(self.manifest["files"][1]))
        with self.assertRaisesRegex(ProcessingError, "exactly one source"):
            resolve_source_ids(self.manifest, self.seed)

    def test_missing_html_role_fails(self):
        del self.manifest["files"][0]
        with self.assertRaisesRegex(ProcessingError, "exactly one source"):
            resolve_source_ids(self.manifest, self.seed)

    def test_invalid_filing_uuid_fails(self):
        self.seed["filing_id"] = "incorrect"
        with self.assertRaisesRegex(ProcessingError, "Invalid UUID for filing_id"):
            resolve_source_ids(self.manifest, self.seed)

    def test_invalid_xml_uuid_fails(self):
        self.seed["document_ids"][self.xml_path] = None
        with self.assertRaisesRegex(ProcessingError, "Invalid UUID"):
            resolve_source_ids(self.manifest, self.seed)

    def test_same_uuid_for_html_and_xml_fails(self):
        self.seed["document_ids"][self.xml_path] = self.ids[1]
        with self.assertRaisesRegex(ProcessingError, "different source IDs"):
            resolve_source_ids(self.manifest, self.seed)

    def test_missing_seed_fields_fail(self):
        del self.seed["filing_id"]
        with self.assertRaisesRegex(ProcessingError, "Missing manifest"):
            resolve_source_ids(self.manifest, self.seed)

    def test_non_mapping_document_ids_fail(self):
        self.seed["document_ids"] = []
        with self.assertRaisesRegex(ProcessingError, "keyed by local_path"):
            resolve_source_ids(self.manifest, self.seed)

    def test_missing_source_path_fails(self):
        del self.manifest["files"][1]["local_path"]
        with self.assertRaisesRegex(ProcessingError, "Missing local_path"):
            resolve_source_ids(self.manifest, self.seed)

    def test_inputs_are_not_mutated(self):
        before = copy.deepcopy((self.manifest, self.seed))
        resolve_source_ids(self.manifest, self.seed)
        self.assertEqual((self.manifest, self.seed), before)


if __name__ == "__main__":
    unittest.main()