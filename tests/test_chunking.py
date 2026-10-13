from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from lxml import html

from sec_pipeline.chunking import chunk_report
from sec_pipeline.report_cleaning import clean_report


def make_section(text: str, section_id: str, start: int, end: int) -> dict:
    return {
        "section_id": section_id,
        "title": section_id,
        "offset_start": start,
        "offset_end": end,
        "source_locator": None,
    }


def run_chunking(text: str, sections: list[dict], **options):
    return chunk_report(
        filing_id=uuid4(),
        report_id=uuid4(),
        full_text=text,
        sections=sections,
        **options,
    )


class ChunkReportTests(unittest.TestCase):
    def test_chunks_are_exact_slices_and_within_max_chars(self):
        text = "Revenue grew steadily throughout the year. Costs also increased."
        sections = [make_section(text, "item_1", 0, len(text))]

        chunks = run_chunking(text, sections, max_chars=20, overlap=4)

        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertEqual(
                chunk.text,
                text[chunk.offset_start:chunk.offset_end],
            )
            self.assertLessEqual(len(chunk.text), 20)
            self.assertEqual(chunk.section, "item_1")

        self.assertEqual(
            [chunk.chunk_index for chunk in chunks],
            list(range(len(chunks))),
        )

    def test_chunks_do_not_cross_sections(self):
        text = "First section text. Second section text."
        split_at = text.index("Second")
        sections = [
            make_section(text, "first", 0, split_at),
            make_section(text, "second", split_at, len(text)),
        ]

        chunks = run_chunking(text, sections, max_chars=12, overlap=3)

        self.assertEqual(
            {chunk.section for chunk in chunks},
            {"first", "second"},
        )

        for chunk in chunks:
            expected_section = (
                "first" if chunk.offset_start < split_at else "second"
            )
            self.assertEqual(chunk.section, expected_section)

    def test_unicode_offsets_match_python_slices(self):
        text = "營收 café e\u0301 increased 📈 during the period."
        sections = [make_section(text, "unicode", 0, len(text))]

        chunks = run_chunking(text, sections, max_chars=10, overlap=2)

        for chunk in chunks:
            self.assertEqual(
                chunk.text,
                text[chunk.offset_start:chunk.offset_end],
            )
            self.assertLessEqual(len(chunk.text), 10)

        covered = {
            position
            for chunk in chunks
            for position in range(chunk.offset_start, chunk.offset_end)
        }

        for position, character in enumerate(text):
            if not character.isspace():
                self.assertIn(position, covered)

    def test_overlap_does_not_repeat_same_chunk_end(self):
        text = ("A" * 1498) + "\n\n" + ("B" * 1802)
        sections = [make_section(text, "long_section", 0, len(text))]

        chunks = run_chunking(text, sections)

        chunk_ends = [chunk.offset_end for chunk in chunks]

        self.assertGreater(len(chunks), 1)
        self.assertEqual(len(chunk_ends), len(set(chunk_ends)))
        self.assertTrue(
            all(
                later > earlier
                for earlier, later in zip(chunk_ends, chunk_ends[1:])
            )
        )

    def test_chunk_metadata_preserves_settings(self):
        text = "Revenue grew steadily throughout the year."
        sections = [make_section(text, "item_1", 0, len(text))]

        chunks = run_chunking(
            text,
            sections,
            max_chars=20,
            overlap=4,
        )

        for chunk in chunks:
            self.assertEqual(chunk.metadata["max_chars"], 20)
            self.assertEqual(chunk.metadata["overlap"], 4)

    def test_rejects_invalid_section_range(self):
        text = "A short report."
        sections = [make_section(text, "bad", 0, len(text) + 1)]

        with self.assertRaises(ValueError):
            run_chunking(text, sections)

    def test_rejects_non_whitespace_text_outside_sections(self):
        text = "Covered text. Missing text."
        sections = [make_section(text, "partial", 0, 13)]

        with self.assertRaises(ValueError):
            run_chunking(text, sections)

    def test_rejects_invalid_chunk_settings(self):
        text = "A short report."
        sections = [make_section(text, "item", 0, len(text))]

        with self.assertRaises(ValueError):
            run_chunking(text, sections, max_chars=0)

        with self.assertRaises(ValueError):
            run_chunking(text, sections, max_chars=10, overlap=10)


class CleanedReportChunkTests(unittest.TestCase):
    def assert_chunk_contract(self, source, report, chunks, max_chars):
        original = html.parse(str(source), parser=html.HTMLParser(encoding="utf-8", no_network=True))
        sections = {section["section_id"]: section for section in report.sections}
        covered = set()
        previous_ends = {}

        self.assertTrue(chunks)
        self.assertEqual([chunk.chunk_index for chunk in chunks], list(range(len(chunks))))
        for chunk in chunks:
            section = sections[chunk.section]
            self.assertGreaterEqual(chunk.offset_start, section["offset_start"])
            self.assertLessEqual(chunk.offset_end, section["offset_end"])
            self.assertLessEqual(len(chunk.text), max_chars)
            self.assertEqual(chunk.text, report.full_text[chunk.offset_start:chunk.offset_end])
            self.assertGreater(chunk.offset_end, previous_ends.get(chunk.section, -1))
            previous_ends[chunk.section] = chunk.offset_end
            covered.update(range(chunk.offset_start, chunk.offset_end))

            locator = chunk.metadata["source_locator"]
            self.assertEqual(locator, section["source_locator"])
            self.assertEqual(locator["source_file"], source.name)
            nodes = original.xpath(locator["xpath"])
            self.assertEqual(len(nodes), 1)
            if "element_id" in locator:
                self.assertEqual(nodes[0].get("id"), locator["element_id"])

        missing = [
            position for position, character in enumerate(report.full_text)
            if not character.isspace() and position not in covered
        ]
        self.assertEqual(missing, [])

    def test_cleaner_paragraphs_and_original_locators_reach_chunks(self):
        first_paragraph = "Revenue grew across regions. 營收 café e\u0301 📈."
        second_paragraph = "Costs include research, infrastructure, and customer support."
        source_text = f"""<html><body>
          <p style="display: none">Hidden preceding sibling</p>
          <p id="cover">Quarterly report</p>
          <p id="part-one">PART I. FINANCIAL INFORMATION</p>
          <p id="statements"><span>ITEM 1.</span> <b>FINANCIAL STATEMENTS</b></p>
          <p>{first_paragraph}</p>
          <p>{second_paragraph}</p>
          <p id="part-two">PART II. OTHER INFORMATION</p>
          <p id="risks"><span>ITEM 1A.</span> <b>RISK FACTORS</b></p>
          <p>Markets change.<br>Operations may be affected.</p>
        </body></html>"""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "quarterly.htm"
            source.write_text(source_text, encoding="utf-8")
            report = clean_report(source)
            self.assertNotIn("Hidden preceding sibling", report.full_text)
            self.assertIn(first_paragraph + "\n\n" + second_paragraph, report.full_text)
            self.assertIn("Markets change.\nOperations may be affected.", report.full_text)

            expected_ids = {
                "front-matter": "cover",
                "part-i": "part-one",
                "part-i-item-1": "statements",
                "part-ii": "part-two",
                "part-ii-item-1a": "risks",
            }
            self.assertEqual({section["section_id"] for section in report.sections}, set(expected_ids))
            for section in report.sections:
                self.assertEqual(section["source_locator"]["element_id"], expected_ids[section["section_id"]])

            statement = next(s for s in report.sections if s["section_id"] == "part-i-item-1")
            paragraph_end = report.full_text.index(first_paragraph) + len(first_paragraph) + 2
            max_chars = paragraph_end - statement["offset_start"] + 8
            options = dict(
                filing_id=uuid4(), report_id=uuid4(), full_text=report.full_text,
                sections=report.sections, max_chars=max_chars, overlap=6,
            )
            chunks = chunk_report(**options)
            self.assertEqual(chunks, chunk_report(**options))
            self.assertEqual(report, clean_report(source))
            statement_chunks = [chunk for chunk in chunks if chunk.section == "part-i-item-1"]
            self.assertGreater(len(statement_chunks), 1)
            self.assertEqual(statement_chunks[0].offset_end, paragraph_end)
            self.assert_chunk_contract(source, report, chunks, max_chars)

    def test_supplied_filing_cleaner_and_chunker_preserve_citation_contract(self):
        source = Path(__file__).resolve().parents[1] / (
            "data/raw/sec/0001193125-26-191507/msft-20260331.htm"
        )
        report = clean_report(source)
        options = dict(
            filing_id=uuid4(), report_id=uuid4(), full_text=report.full_text,
            sections=report.sections, max_chars=1500, overlap=150,
        )
        chunks = chunk_report(**options)
        self.assertEqual(chunks, chunk_report(**options))
        self.assert_chunk_contract(source, report, chunks, 1500)


if __name__ == "__main__":
    unittest.main()
