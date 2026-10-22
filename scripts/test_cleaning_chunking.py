
from pathlib import Path
from uuid import uuid4

from sec_pipeline.report_cleaning import clean_report
from sec_pipeline.chunking import chunk_report


# Microsoft 10-Q sample
html_path = Path(
    "data/raw/sec/0001193125-26-191507/msft-20260331.htm"
)

# Step 1: Emma's report cleaning
report = clean_report(html_path)

print("CLEANING RESULTS")
print("Text length:", len(report.full_text))
print("Sections:", len(report.sections))

# Step 2: Ruby's chunking
chunks = chunk_report(
    filing_id=uuid4(),
    report_id=uuid4(),
    full_text=report.full_text,
    sections=report.sections,
)

print("\nCHUNKING RESULTS")
print("Chunks generated:", len(chunks))

# Step 3: Basic integration checks
assert len(chunks) > 0
assert len(report.sections) > 0

# Step 4: Display the first few chunks
print("\nFIRST 3 CHUNKS")

for chunk in chunks[:3]:
    print(chunk)

print("\nPASS: Cleaning and chunking integration completed")

# Verify that each chunk points to the correct text.
for chunk in chunks:
    start = chunk.offset_start
    end = chunk.offset_end

    assert 0 <= start < end <= len(report.full_text)
    assert chunk.text == report.full_text[start:end]

print("PASS: All chunk text and offsets match")

print("\nSECTION COVERAGE VALIDATION")

for section in report.sections:
    section_id = section["section_id"]
    section_start = section["offset_start"]
    section_end = section["offset_end"]

    section_chunks = [
        chunk
        for chunk in chunks
        if chunk.section == section_id
    ]

    assert len(section_chunks) > 0

    section_chunks.sort(key=lambda chunk: chunk.offset_start)

    assert section_chunks[0].offset_start == section_start
    assert section_chunks[-1].offset_end == section_end

    for current, following in zip(section_chunks, section_chunks[1:]):
        assert following.offset_start <= current.offset_end
        assert following.offset_end > current.offset_end

print("PASS: All sections are fully covered by chunks")

print("\nCHUNK OVERLAP VALIDATION")

overlap_lengths = []

for section in report.sections:
    section_chunks = sorted(
        [
            chunk for chunk in chunks
            if chunk.section == section["section_id"]
        ],
        key=lambda chunk: chunk.offset_start
    )

    for current, following in zip(
        section_chunks, section_chunks[1:]
    ):
        actual_overlap = current.offset_end - following.offset_start

        assert actual_overlap > 0

        assert (
            current.text[-actual_overlap:]
            == following.text[:actual_overlap]
        )

        overlap_lengths.append(actual_overlap)

print("PASS: Chunk overlaps are consistent")
print("Overlap pairs checked:", len(overlap_lengths))

if overlap_lengths:
    print("Minimum overlap:", min(overlap_lengths))
    print("Maximum overlap:", max(overlap_lengths))
