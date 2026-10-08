import unittest
from uuid import uuid4

from sec_pipeline.chunking import chunk_report


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


if __name__ == "__main__":
    unittest.main()