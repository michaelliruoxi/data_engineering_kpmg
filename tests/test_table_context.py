from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from sec_pipeline.chunking import chunk_report
from sec_pipeline.database import ChunkInput
from sec_pipeline.table_context import build_table_contexts


@pytest.fixture
def sample():
    text = ("Analysis.\n\nCost of Product Revenues (in millions, except percentages):\n\n"
            "| | Three Months Ended | |\n\n"
            "| | July 31, 2026 | | July 25, 2025 | | % Change |\n\n"
            "Cost of product revenues | $ | 450 | $ | 302 | 49 | % |\n\n"
            "Hybrid Cloud | 448 | 301 | 49 | % |\n\nUnallocated | 2 | 1 | 100 | % |\n\n"
            "Discussion continues. 營收 café 📈.")
    section = dict(section_id="mda", offset_start=0, offset_end=len(text),
                   source_locator={"xpath": "/html/body/p[1]"})
    chunk = ChunkInput(filing_id=uuid4(), report_id=uuid4(), chunking_version="paragraph-v2",
                       chunk_index=61, offset_start=text.index("450") + 2,
                       offset_end=text.index("Discussion"), text=text[text.index("450") + 2:text.index("Discussion")],
                       section="mda", metadata={"overlap": 150})
    return text, [section], chunk


def build(sample, **options):
    text, sections, chunk = sample
    return build_table_contexts(full_text=text, sections=sections, chunks=[chunk], **options)[61]


def test_continuation_restores_complete_amount_label_dates_and_units(sample):
    output = build(sample)
    context = output["table_contexts"][0]
    assert context["starts_inside_row"] and context["is_continuation"]
    for expected in ("450", "Cost of product revenues", "July 31, 2026", "July 25, 2025", "in millions"):
        assert expected in context["segments"][0]["text"]
    assert output["retrieval_text"].endswith(sample[2].text)
    assert output["warnings"] == []


def test_context_has_own_exact_citation_offsets_and_no_invented_table_uuid(sample):
    output = build(sample)
    context = output["table_contexts"][0]
    for segment in context["segments"]:
        assert segment["text"] == sample[0][segment["offset_start"]:segment["offset_end"]]
    assert context["html_table_id"] is None
    assert context["mapping_method"] == "inferred_cleaned_pipe_run"
    assert context["locator_scope"] == "section"


def test_sidecar_does_not_mutate_chunks_or_uuid_identity(sample):
    before = asdict(sample[2])
    first = build(sample)
    assert asdict(sample[2]) == before
    assert first == build(sample)
    assert first["identity"] == {"report_id": str(sample[2].report_id),
                                 "chunking_version": "paragraph-v2", "chunk_index": 61}
    assert "chunk_id" not in first


@pytest.mark.parametrize("version", ["paragraph-v1", "paragraph-v2"])
def test_context_preserves_actual_chunker_outputs_and_coverage(sample, version):
    text, sections, c = sample
    chunks = chunk_report(filing_id=c.filing_id, report_id=c.report_id, full_text=text,
                          sections=sections, chunking_version=version, max_chars=120, overlap=35)
    before = [asdict(chunk) for chunk in chunks]
    result = build_table_contexts(full_text=text, sections=sections, chunks=chunks)
    assert len(result) == len(chunks)
    assert [asdict(chunk) for chunk in chunks] == before
    assert any(r["table_contexts"] for r in result.values())
    covered = {i for c in chunks for i in range(c.offset_start, c.offset_end)}
    assert all(i in covered for i, char in enumerate(text) if not char.isspace())


def test_prose_has_no_guessed_table_context():
    text = "Ordinary prose with a | separator.\n\nAnother paragraph."
    section = dict(section_id="s", offset_start=0, offset_end=len(text))
    c = ChunkInput(filing_id=uuid4(), report_id=uuid4(), chunking_version="paragraph-v2",
                   chunk_index=0, offset_start=0, offset_end=len(text), text=text, section="s")
    result = build_table_contexts(full_text=text, sections=[section], chunks=[c])[0]
    assert result["table_contexts"] == []
    assert result["retrieval_text"] == text


def test_adjacent_table_context_does_not_borrow_other_table_headers():
    a = "Table A (in millions):\n\nLabel | 2026 | 2025 |\n\nA | 450 | 302 |\n\n"
    b = "Table B (in thousands):\n\nLabel | 2026 | 2025 |\n\nB | 450 | 302 |"
    text = a + b
    start = text.index("B | 450") + 5
    c = ChunkInput(filing_id=uuid4(), report_id=uuid4(), chunking_version="paragraph-v2",
                   chunk_index=0, offset_start=start, offset_end=len(text), text=text[start:], section="s")
    result = build_table_contexts(full_text=text, sections=[dict(section_id="s",offset_start=0,offset_end=len(text))], chunks=[c])[0]
    assert len(result["table_contexts"]) == 1
    segment = result["table_contexts"][0]["segments"][0]["text"]
    assert "in thousands" in segment and "Table A" not in segment and "in millions" not in segment


def test_context_stays_inside_section():
    first = "Table A:\n\nLabel | 2026 | 2025 |\n\nA | 450 | 302 |\n\n"
    second = "Label | 2026 | 2025 |\n\nB | 450 | 302 |"
    text = first + second
    sections = [dict(section_id="a",offset_start=0,offset_end=len(first)),
                dict(section_id="b",offset_start=len(first),offset_end=len(text))]
    start = text.index("B |")
    c = ChunkInput(filing_id=uuid4(), report_id=uuid4(), chunking_version="paragraph-v2",
                   chunk_index=0, offset_start=start, offset_end=len(text), text=text[start:], section="b")
    result = build_table_contexts(full_text=text, sections=sections, chunks=[c])[0]
    assert "Table A" not in result["retrieval_text"]
    assert all(s["offset_start"] >= len(first) for t in result["table_contexts"] for s in t["segments"])


def test_large_table_keeps_header_and_complete_boundary_row():
    heading = "Long table (in millions):\n\nLabel | 2026 | 2025 |\n\n"
    rows = "\n\n".join(f"Row {i} | 450 | 302 |" for i in range(100))
    text = heading + rows
    start = text.index("Row 50") + 11
    end = text.index("Row 51") + 12
    c = ChunkInput(filing_id=uuid4(), report_id=uuid4(), chunking_version="paragraph-v2",
                   chunk_index=0, offset_start=start, offset_end=end, text=text[start:end], section="s")
    result = build_table_contexts(full_text=text, sections=[dict(section_id="s",offset_start=0,offset_end=len(text))], chunks=[c], max_context_chars=200)[0]
    contexts = result["table_contexts"][0]
    assert {s["role"] for s in contexts["segments"]} == {"table_header", "boundary_row"}
    assert "Row 50 | 450 | 302 |" in result["retrieval_text"]
    assert "Row 51 | 450 | 302 |" in result["retrieval_text"]
    assert "in millions" in result["retrieval_text"]
    assert "Label | 2026 | 2025 |" in result["retrieval_text"]


def test_context_budget_failure_is_explicit(sample):
    result = build(sample, max_context_chars=1)
    assert "context_budget_exceeded" in result["warnings"]
    assert "no_context_available" in result["warnings"]
    assert result["retrieval_text"] == sample[2].text


def test_missing_header_is_flagged_instead_of_inventing_dates_or_units():
    text = "Revenue | 450 | 302 |\n\nCosts | 120 | 100 |"
    c = ChunkInput(filing_id=uuid4(), report_id=uuid4(), chunking_version="paragraph-v2",
                   chunk_index=0, offset_start=10, offset_end=len(text), text=text[10:], section="s")
    result = build_table_contexts(full_text=text,
        sections=[dict(section_id="s", offset_start=0, offset_end=len(text))], chunks=[c])[0]
    assert "header_not_identified" in result["warnings"]
    assert "July" not in result["retrieval_text"] and "millions" not in result["retrieval_text"]


@pytest.mark.parametrize("change", ["text", "outside", "duplicate"])
def test_rejects_invalid_or_ambiguous_chunk_scope(sample, change):
    text, sections, c = sample
    chunks = [replace(c, text="wrong")] if change == "text" else [c, c] if change == "duplicate" else [replace(c, section="other")]
    with pytest.raises(ValueError):
        build_table_contexts(full_text=text, sections=sections, chunks=chunks)


def test_rejects_mixed_reports(sample):
    text, sections, c = sample
    with pytest.raises(ValueError, match="one filing/report"):
        build_table_contexts(full_text=text, sections=sections,
                             chunks=[c, replace(c, report_id=uuid4(), chunk_index=62)])
