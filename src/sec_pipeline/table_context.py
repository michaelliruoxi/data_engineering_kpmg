"""Read-only retrieval context for tables flattened into cleaned report text.

This is a sidecar, not a new chunking version. Neither ChunkInput nor storage
is changed. Pipe runs are inferred candidates, never asserted HTML table IDs.
Every context segment carries its own exact cleaned-report citation offsets.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

from .database import ChunkInput

CONTEXT_VERSION = "table-context-v1"


@dataclass(frozen=True)
class _Line:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Table:
    section_key: str
    start: int
    end: int
    source_start: int
    header_end: int
    lines: tuple[_Line, ...]
    locator: Any


def _caption(text: str) -> bool:
    value = text.strip()
    return bool(value) and len(value) <= 240 and (
        value.endswith(":") or value.isupper()
        or bool(re.match(r"^\((?:in\b|unaudited\b)", value, re.I))
    )


def _data_row(text: str) -> bool:
    cells = [cell.strip() for cell in text.split("|") if cell.strip()]
    if len(cells) < 2:
        return False
    # Date/period column headings are not data rows.
    if any(re.search(r"\b(?:january|february|march|april|may|june|july|august|"
                     r"september|october|november|december|months? ended|years? ended)\b",
                     cell, re.I) for cell in cells):
        return False
    if all(re.fullmatch(r"(?:19|20)\d{2}", cell) for cell in cells[1:]):
        return False
    return any(re.fullmatch(r"\(?[−–+\-]?\d[\d,]*(?:\.\d+)?\)?%?|[—–-]", cell)
               for cell in cells[1:])


def _tables(full_text: str, section: Mapping[str, Any]) -> list[_Table]:
    start, end = section["offset_start"], section["offset_end"]
    lines = []
    offset = start
    for raw in full_text[start:end].splitlines(keepends=True):
        value = raw.rstrip("\r\n")
        lines.append(_Line(offset, offset + len(value), value))
        offset += len(raw)
    tables = []
    i = 0
    while i < len(lines):
        if lines[i].text.count("|") < 2:
            i += 1
            continue
        first = i
        rows = []
        j = i
        while j < len(lines):
            line = lines[j]
            if line.text.count("|") >= 2:
                rows.append(line)
            elif line.text.strip():
                break
            j += 1
        i = j
        if len(rows) < 2:
            continue
        source_start = rows[0].start
        previous = first - 1
        # Only adjacent, short caption/qualifier lines; stop at prose or section.
        for _ in range(6):
            while previous >= 0 and not lines[previous].text.strip():
                previous -= 1
            if previous < 0 or not _caption(lines[previous].text):
                break
            source_start = lines[previous].start
            previous -= 1
        first_data = next((row for row in rows if _data_row(row.text)), None)
        # Missing/ambiguous headers stay explicit; never borrow a different table.
        header_end = first_data.start if first_data else rows[0].start
        tables.append(_Table(str(section["section_id"]), rows[0].start,
                             rows[-1].end, source_start, header_end, tuple(rows),
                             section.get("source_locator")))
    return tables


def build_table_contexts(
    *, full_text: str, sections: Sequence[Mapping[str, Any]],
    chunks: Sequence[ChunkInput], max_context_chars: int = 12000,
) -> dict[int, dict[str, Any]]:
    """Build one sidecar per supplied chunk; no DB access or UUID assignment.

    For small pipe tables include the caption and whole table. For oversized
    tables include the header and any row split by a chunk boundary. Unresolved
    or over-budget context is flagged rather than silently truncated. Rendering
    must cite each segment with that segment's offsets, not the chunk offsets.
    """
    if type(max_context_chars) is not int or max_context_chars < 1:
        raise ValueError("max_context_chars must be a positive integer")
    by_key = {}
    previous_end = 0
    for section in sections:
        a, b = section.get("offset_start"), section.get("offset_end")
        key = section.get("section_id")
        if (not isinstance(key, str) or not key or key in by_key
                or type(a) is not int or type(b) is not int
                or not (previous_end <= a < b <= len(full_text))):
            raise ValueError("Sections need unique keys and ordered valid ranges")
        by_key[key] = section
        previous_end = b
    scopes = {(c.filing_id, c.report_id, c.chunking_version) for c in chunks}
    if len(scopes) > 1:
        raise ValueError("Build context for one filing/report/chunking version at a time")
    indexes = [c.chunk_index for c in chunks]
    if any(type(i) is not int or i < 0 for i in indexes) or len(indexes) != len(set(indexes)):
        raise ValueError("Chunk indexes must be unique non-negative integers")
    candidates = {key: _tables(full_text, section) for key, section in by_key.items()}
    result = {}
    for chunk in chunks:
        section = by_key.get(chunk.section)
        a, b = chunk.offset_start, chunk.offset_end
        if (section is None or type(a) is not int or type(b) is not int
                or not (section["offset_start"] <= a < b <= section["offset_end"])
                or chunk.text != full_text[a:b]):
            raise ValueError("Chunk must be an exact slice inside its declared section")
        contexts = []
        segments = []
        warnings = []
        remaining = max_context_chars
        for table in candidates[chunk.section]:
            if not (a < table.end and b > table.start):
                continue
            ref = f"cleaned:{chunk.report_id}:{table.section_key}:{table.start}:{table.end}"
            partial = [line for line in table.lines if
                       (line.start < a < line.end) or (line.start < b < line.end)]
            ranges = [(table.source_start, table.end, "complete_table")]
            if table.end - table.source_start > remaining:
                ranges = []
                if table.source_start < table.header_end:
                    ranges.append((table.source_start, table.header_end, "table_header"))
                ranges.extend((line.start, line.end, "boundary_row") for line in partial)
            included = []
            table_warnings = []
            if table.header_end == table.start:
                table_warnings.append("header_not_identified")
            for left, right, role in ranges:
                if right - left > remaining:
                    table_warnings.append("context_budget_exceeded")
                    continue
                segment = {"table_ref": ref, "role": role, "offset_start": left,
                           "offset_end": right, "text": full_text[left:right]}
                segments.append(segment)
                included.append(segment)
                remaining -= right - left
            if not included:
                table_warnings.append("no_context_available")
            contexts.append({
                "table_ref": ref, "table_offset_start": table.start,
                "table_offset_end": table.end, "section_key": table.section_key,
                "mapping_method": "inferred_cleaned_pipe_run", "html_table_id": None,
                "source_locator": table.locator, "locator_scope": "section",
                "is_continuation": a > table.start,
                "starts_inside_row": any(line.start < a < line.end for line in table.lines),
                "ends_inside_row": any(line.start < b < line.end for line in table.lines),
                "segments": included, "warnings": sorted(set(table_warnings)),
            })
            warnings.extend(table_warnings)
        context_text = "\n\n".join(segment["text"] for segment in segments)
        result[chunk.chunk_index] = {
            "context_version": CONTEXT_VERSION,
            "identity": {"report_id": str(chunk.report_id),
                         "chunking_version": chunk.chunking_version,
                         "chunk_index": chunk.chunk_index},
            "table_contexts": contexts,
            "retrieval_text": ("[Table context]\n" + context_text + "\n\n[Chunk]\n"
                               + chunk.text) if segments else chunk.text,
            "warnings": sorted(set(warnings)),
        }
    return result
