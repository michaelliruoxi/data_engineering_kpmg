from uuid import uuid4

import pytest

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


def test_chunks_are_exact_slices_and_within_max_chars():
    text = "Revenue grew steadily throughout the year. Costs also increased."
    sections = [make_section(text, "item_1", 0, len(text))]

    chunks = run_chunking(text, sections, max_chars=20, overlap=4)

    assert chunks
    for chunk in chunks:
        assert chunk.text == text[chunk.offset_start:chunk.offset_end]
        assert len(chunk.text) <= 20
        assert chunk.section == "item_1"

    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))


def test_chunks_do_not_cross_sections():
    text = "First section text. Second section text."
    split_at = text.index("Second")
    sections = [
        make_section(text, "first", 0, split_at),
        make_section(text, "second", split_at, len(text)),
    ]

    chunks = run_chunking(text, sections, max_chars=12, overlap=3)

    assert {chunk.section for chunk in chunks} == {"first", "second"}
    for chunk in chunks:
        expected_section = (
            "first" if chunk.offset_start < split_at else "second"
        )
        assert chunk.section == expected_section


def test_unicode_offsets_match_python_slices():
    text = "營收 café e\u0301 increased 📈 during the period."
    sections = [make_section(text, "unicode", 0, len(text))]

    chunks = run_chunking(text, sections, max_chars=10, overlap=2)

    for chunk in chunks:
        assert chunk.text == text[chunk.offset_start:chunk.offset_end]
        assert len(chunk.text) <= 10

    covered = {
        position
        for chunk in chunks
        for position in range(chunk.offset_start, chunk.offset_end)
    }
    for position, character in enumerate(text):
        if not character.isspace():
            assert position in covered


def test_rejects_invalid_section_range():
    text = "A short report."
    sections = [make_section(text, "bad", 0, len(text) + 1)]

    with pytest.raises(ValueError):
        run_chunking(text, sections)


def test_rejects_non_whitespace_text_outside_sections():
    text = "Covered text. Missing text."
    sections = [make_section(text, "partial", 0, 13)]

    with pytest.raises(ValueError):
        run_chunking(text, sections)


def test_rejects_invalid_chunk_settings():
    text = "A short report."
    sections = [make_section(text, "item", 0, len(text))]

    with pytest.raises(ValueError):
        run_chunking(text, sections, max_chars=0)

    with pytest.raises(ValueError):
        run_chunking(text, sections, max_chars=10, overlap=10)