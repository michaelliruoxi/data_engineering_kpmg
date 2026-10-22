import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sec_pipeline.report_cleaning import clean_report

from uuid import uuid4
from sec_pipeline.chunking import chunk_report

source_path = Path(
    "data/raw/sec/0001193125-26-191507/msft-20260331.htm"
)

output_path = Path("cleaning_manifest.json")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Step 1: Run cleaning
report = clean_report(source_path)

# Generate chunks using the current local pipeline.
chunks = chunk_report(
    filing_id=uuid4(),
    report_id=uuid4(),
    full_text=report.full_text,
    sections=report.sections,
)

# Validate section boundaries.
assert report.sections[0]["offset_start"] == 0
assert report.sections[-1]["offset_end"] == len(report.full_text)

for current, following in zip(report.sections, report.sections[1:]):
    assert current["offset_end"] == following["offset_start"]

# Validate chunk offsets.
for chunk in chunks:
    assert chunk.text == report.full_text[
        chunk.offset_start:chunk.offset_end
    ]

# Validate section coverage and overlaps.
overlap_lengths = []

for section in report.sections:
    section_chunks = sorted(
        [c for c in chunks if c.section == section["section_id"]],
        key=lambda c: c.offset_start
    )

    assert section_chunks
    assert section_chunks[0].offset_start == section["offset_start"]
    assert section_chunks[-1].offset_end == section["offset_end"]

    for current, following in zip(section_chunks, section_chunks[1:]):
        overlap = current.offset_end - following.offset_start

        assert overlap > 0
        assert current.text[-overlap:] == following.text[:overlap]

        overlap_lengths.append(overlap)

validation = {
    "section_boundaries_valid": True,
    "chunk_offsets_valid": True,
    "section_coverage_valid": True,
    "chunk_overlap_valid": True,
    "chunk_count": len(chunks),
    "overlap_pairs_checked": len(overlap_lengths),
    "min_overlap_chars": min(overlap_lengths) if overlap_lengths else None,
    "max_overlap_chars": max(overlap_lengths) if overlap_lengths else None,
}

# Step 2: Get the current Git commit
git_commit = subprocess.check_output(
    ["git", "rev-parse", "HEAD"],
    text=True
).strip()

# Step 3: Build manifest
manifest = {
    "accession_number": source_path.parent.name,
    "source_file": str(source_path),
    "source_sha256": sha256_bytes(source_path.read_bytes()),
    "cleaned_text_sha256": sha256_bytes(
        report.full_text.encode("utf-8")
    ),
    "git_commit": git_commit,
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "section_count": len(report.sections),
    "chunking_version": "paragraph-v1",
    "max_chars": 1500,
    "configured_overlap_chars": 150,
    "validation": validation,
    "sections": [
        {
            "section_id": section["section_id"],
            "offset_start": section["offset_start"],
            "offset_end": section["offset_end"],
        }
        for section in report.sections
    ],
}

# Step 4: Save JSON
output_path.write_text(
    json.dumps(manifest, indent=2, ensure_ascii=False),
    encoding="utf-8"
)

print(f"Manifest saved to: {output_path}")
print(f"Section count: {manifest['section_count']}")
print(f"Git commit: {git_commit}")