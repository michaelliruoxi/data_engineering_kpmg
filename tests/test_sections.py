"""Section taxonomy, body detection, Unicode ranges, and legacy compatibility."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from uuid import uuid4

from sec_pipeline.chunking import chunk_report
from sec_pipeline.database import ChunkInput, DatabaseError, ReportInput
from sec_pipeline.report_cleaning import clean_report, detect_sections
from sec_pipeline.sections import normalize_sections, section_id_for_chunk


class SectionsTests(unittest.TestCase):
    def report(self, text="alpha 😀\n风险 Café\u0301", sections=None):
        return ReportInput(filing_id=uuid4(), source_document_id=uuid4(),
                           extraction_version="sections-test", full_text=text,
                           sections=sections if sections is not None else [{
                               "section_id": "legacy", "title": "Legacy",
                               "offset_start": 0, "offset_end": len(text),
                               "source_locator": "source.htm"}])

    def test_legacy_labels_are_unverified_unknown_with_preserved_locator(self):
        section = normalize_sections(self.report())[0]
        self.assertEqual((section.section_key, section.section_type, section.validation_status),
                         ("legacy", "unknown", "unverified"))
        self.assertEqual(section.source_locator, {"legacy_locator": "source.htm"})

    def test_legacy_descriptor_without_a_key_gets_a_stable_ordinal_identity(self):
        report = self.report(sections=[{"title": "Results", "offset_start": 0, "offset_end": 5}])
        section = normalize_sections(report)[0]
        self.assertEqual(section.section_key, "legacy-section-0")
        self.assertEqual(section.section_type, "unknown")
        self.assertEqual(section.validation_status, "unverified")
        self.assertEqual(normalize_sections(report)[0], section)

    def test_invalid_ranges_and_duplicate_keys_are_rejected(self):
        base = self.report()
        for changes in ({"offset_start": -1}, {"offset_end": 999}, {"offset_start": True},
                        {"offset_end": 0}, {"section_type": "imagined"},
                        {"validation_status": "trusted"}):
            with self.subTest(changes=changes), self.assertRaises(DatabaseError):
                normalize_sections(replace(base, sections=[base.sections[0] | changes]))
        with self.assertRaises(DatabaseError):
            normalize_sections(replace(base, sections=base.sections * 2))

    def test_annual_and_quarterly_mda_have_one_canonical_type(self):
        for form, part, item in (("10-Q", "I", "2"), ("10-K", "II", "7")):
            text = f"FORM {form}\nPART {part}\nItem {item}. Management’s Discussion and Analysis\nRevenue rose 😀."
            sections = detect_sections(text)
            mda = next(s for s in sections if s["section_type"] == "mda")
            self.assertEqual((mda["part"], mda["item"]), (part, item))
            self.assertEqual(mda["validation_status"], "detected")
            self.assertTrue(text[mda["offset_start"]:].startswith(f"Item {item}."))

    def test_governance_preserves_proxy_incorporation_as_unresolved(self):
        text = ("FORM 10-K\nPART III\nItem 10. Directors, Executive Officers and Corporate Governance\n"
                "Information is incorporated by reference from the proxy statement.\n"
                "Item 11. Executive Compensation\nCompensation discussion.")
        sections = detect_sections(text)
        governance = next(s for s in sections if s["section_type"] == "directors_governance")
        self.assertEqual(governance["source_locator"]["incorporation_status"], "unresolved")
        self.assertEqual(next(s for s in sections if s.get("item") == "11")["section_type"], "executive_compensation")

    def test_plain_and_table_toc_entries_are_not_body_headings(self):
        text = "\n".join([
            "FORM 10-K", "TABLE OF CONTENTS", "PART I", "Item 1. Business", "1",
            "Item 1A. Risk Factors ........ 4", "PART II", "Item 7. Management’s Discussion | 8 |",
            "PART I", "Item 1. Business", "We make products.",
            "Item 1A. Risk Factors", "Demand could fall.",
            "PART II", "Item 7. Management’s Discussion", "Results improved.",
        ])
        sections = detect_sections(text)
        for item, heading in (("1", "Item 1. Business"), ("1A", "Item 1A. Risk Factors"),
                              ("7", "Item 7. Management’s Discussion")):
            section = next(s for s in sections if s.get("item") == item)
            self.assertEqual(section["offset_start"], text.rfind(heading))

    def test_bare_repeated_item_page_label_does_not_steal_heading_offset(self):
        text = "ITEM 2\nITEM 2. MANAGEMENT’S DISCUSSION\nRevenue rose.\nITEM 2. MANAGEMENT’S DISCUSSION\nMore revenue."
        sections = detect_sections(text, form="10-Q")
        mda = next(s for s in sections if s["section_type"] == "mda")
        self.assertEqual(mda["offset_start"], text.index("ITEM 2. MANAGEMENT"))
        self.assertEqual(sum(s["section_type"] == "mda" for s in sections), 1)

    def test_nonstandard_quarterly_layout_retains_unknown_prefix_not_toc_topic(self):
        text = ("FORM 10-Q\nPART I. FINANCIAL INFORMATION\nItem 2.\n"
                "| Management’s Discussion and Analysis\n| 4\nINTRODUCTION\n"
                "The following is management discussion in an unusual layout.\n"
                "Item 3. Quantitative and Qualitative Disclosures About Market Risk\nSee market risk.\n")
        sections = detect_sections(text)
        self.assertEqual(sections[0]["section_type"], "unknown")
        self.assertFalse(any(s["section_type"] == "mda" for s in sections))
        self.assertFalse(any(s["section_type"] == "part_heading" for s in sections))

    def test_toc_with_subsection_rows_does_not_assign_the_remaining_report_to_mda(self):
        text = ("FORM 10-Q\nTABLE OF CONTENTS\nItem 1. | Financial Statements | |\n"
                "| Consolidated statements of income | 93 |\n"
                "Item 2. | Management’s Discussion and Analysis | |\n"
                "| Introduction | 4 |\n| Executive overview | 5 |\n"
                "INTRODUCTION\nManagement discussion without an Item heading.\n"
                "Item 3. Quantitative and Qualitative Disclosures About Market Risk\nRisk discussion.")
        sections = detect_sections(text)
        self.assertEqual(sections[0]["section_type"], "unknown")
        self.assertFalse(any(s["section_type"] in {"mda", "financial_statements"} for s in sections))

    def test_chunk_ranges_remain_unicode_exact_and_do_not_cross_sections(self):
        text = "FORM 10-Q\nPART I — FINANCIAL INFORMATION\nItem 2. Management’s Discussion\n😀 Revenue.\nItem 3. Quantitative and Qualitative Disclosures\n风险 Café\u0301."
        sections = detect_sections(text)
        chunks = chunk_report(filing_id=uuid4(), report_id=uuid4(), full_text=text,
                              sections=sections, max_chars=45, overlap=5)
        bounds = {s["section_id"]: s for s in sections}
        for chunk in chunks:
            self.assertEqual(chunk.text, text[chunk.offset_start:chunk.offset_end])
            section = bounds[chunk.section]
            self.assertLessEqual(section["offset_start"], chunk.offset_start)
            self.assertGreaterEqual(section["offset_end"], chunk.offset_end)

    def test_clean_report_accepts_known_form_without_cover_heading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.htm"
            path.write_text("<h1>Item 1. Business</h1><p>We sell products.</p>", encoding="utf-8")
            report = clean_report(path, form="10-K")
        self.assertEqual(report.sections[0]["section_type"], "business")
        self.assertEqual(report.sections[0]["source_locator"]["document"], "report.htm")

    def test_legacy_chunk_resolution_is_report_scoped_and_checks_containment(self):
        conn = Mock()
        section_id = uuid4()
        conn.execute.return_value.fetchone.return_value = (section_id, 0, 5)
        chunk = ChunkInput(filing_id=uuid4(), report_id=uuid4(), chunking_version="v1",
                           chunk_index=0, text="alpha", offset_start=0, offset_end=5, section="legacy")
        self.assertEqual(section_id_for_chunk(conn, chunk), section_id)
        self.assertEqual(conn.execute.call_args.args[1], (chunk.report_id, chunk.filing_id, "legacy"))
        with self.assertRaises(DatabaseError):
            section_id_for_chunk(conn, replace(chunk, offset_end=6))
        conn.execute.return_value.fetchone.return_value = None
        self.assertIsNone(section_id_for_chunk(conn, chunk))

    def test_unknown_form_does_not_receive_a_guessed_taxonomy(self):
        sections = detect_sections("Item 2. Management’s Discussion\nContent.", form="8-K")
        self.assertEqual(sections[0]["section_type"], "unknown")
        self.assertEqual(sections[0]["validation_status"], "unverified")


if __name__ == "__main__":
    unittest.main()
