from uuid import uuid4

import pytest

from sec_pipeline.chunking import chunk_report


def chunks(text, **kwargs):
    return chunk_report(filing_id=uuid4(), report_id=uuid4(), full_text=text,
                        sections=[dict(section_id="financial", offset_start=0,
                                       offset_end=len(text))], **kwargs)


@pytest.mark.parametrize("overlap", [0, 1, 7, 20, 39])
def test_reviewed_chunks_preserve_whole_financial_tokens_and_coverage(overlap):
    text = ("June 11, 2026 | $32,105 | −6,760.91 | café 營收 grew.\n\n" * 15)
    result = chunks(text, chunking_version="paragraph-v2", max_chars=40, overlap=overlap)
    covered = set()
    ends = []
    for chunk in result:
        assert chunk.text == text[chunk.offset_start:chunk.offset_end]
        assert len(chunk.text) <= 40
        assert chunk.offset_start == 0 or text[chunk.offset_start - 1].isspace()
        covered.update(range(chunk.offset_start, chunk.offset_end))
        ends.append(chunk.offset_end)
    assert all(a < b for a, b in zip(ends, ends[1:]))
    assert all(i in covered for i, char in enumerate(text) if not char.isspace())


def test_oversized_indivisible_token_is_not_silently_split():
    with pytest.raises(ValueError, match="token exceeds"):
        chunks("1" * 41, chunking_version="paragraph-v2", max_chars=40, overlap=7)


def test_legacy_chunk_offsets_are_unchanged():
    text = "Revenue grew steadily throughout the year. Costs also increased."
    result = chunks(text, chunking_version="paragraph-v1", max_chars=20, overlap=4)
    assert [(c.offset_start, c.offset_end) for c in result] == [
        (0, 13), (9, 22), (18, 37), (33, 49), (45, 64)]
