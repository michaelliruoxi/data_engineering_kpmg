from dataclasses import replace
from typing import Any, Mapping, Sequence
from uuid import UUID

import psycopg

from .database import ChunkInput, DatabaseError, store_chunk


def chunk_report(
    *,
    filing_id: UUID,
    report_id: UUID,
    full_text: str,
    sections: Sequence[Mapping[str, Any]],
    chunking_version: str = "paragraph-v1",
    max_chars: int = 1500,
    overlap: int = 150,
) -> list[ChunkInput]:
    """Split cleaned report text into exact, section-bounded chunks."""
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be between 0 and max_chars - 1")
    if not chunking_version.strip():
        raise ValueError("chunking_version cannot be empty")

    checked_sections: list[dict[str, Any]] = []
    previous_end = 0

    for section in sections:
        start = section.get("offset_start")
        end = section.get("offset_end")

        if not isinstance(start, int) or not isinstance(end, int):
            raise ValueError(
                "Each section needs integer offset_start and offset_end"
            )
        if start < 0 or end <= start or end > len(full_text):
            raise ValueError(f"Invalid section range: {start}:{end}")
        if start < previous_end:
            raise ValueError("Sections must be ordered and cannot overlap")

        checked_sections.append(dict(section))
        previous_end = end

    # Every non-whitespace character must belong to a section.
    section_index = 0
    for position, character in enumerate(full_text):
        if character.isspace():
            continue
        while (
            section_index < len(checked_sections)
            and checked_sections[section_index]["offset_end"] <= position
        ):
            section_index += 1
        if (
            section_index == len(checked_sections)
            or checked_sections[section_index]["offset_start"] > position
        ):
            raise ValueError(
                f"Non-whitespace text at offset {position} is outside all sections"
            )

    chunks: list[ChunkInput] = []

    for section in checked_sections:
        section_start = section["offset_start"]
        section_end = section["offset_end"]
        section_text = full_text[section_start:section_end]

        # Ignore sections that contain only whitespace.
        if not section_text.strip():
            continue

        cursor = section_start
        previous_chunk_end = section_start

        while cursor < section_end:
            hard_end = min(cursor + max_chars, section_end)
            end = hard_end

            if hard_end < section_end:
                window = full_text[cursor:hard_end]

                # Prefer a paragraph break only if it advances beyond
                # the end of the previous chunk.
                paragraph_break = window.rfind("\n\n")
                paragraph_end = cursor + paragraph_break + 2

                if (
                    paragraph_break > 0
                    and paragraph_end > previous_chunk_end
                ):
                    end = paragraph_end
                else:
                    whitespace = max(
                        window.rfind(" "),
                        window.rfind("\n"),
                        window.rfind("\t"),
                    )
                    whitespace_end = cursor + whitespace + 1

                    if (
                        whitespace > 0
                        and whitespace_end > previous_chunk_end
                    ):
                        end = whitespace_end

            text = full_text[cursor:end]
            if text.strip():
                metadata = {
                    "section_id": section.get("section_id"),
                    "title": section.get("title"),
                    "source_locator": section.get("source_locator"),
                    "max_chars": max_chars,
                    "overlap": overlap,
                }
                chunks.append(
                    ChunkInput(
                        filing_id=filing_id,
                        report_id=report_id,
                        chunking_version=chunking_version,
                        chunk_index=len(chunks),
                        text=text,
                        offset_start=cursor,
                        offset_end=end,
                        section=section.get("section_id"),
                        metadata=metadata,
                    )
                )

            previous_chunk_end = end

            if end >= section_end:
                break

            # Preserve overlap while ensuring the next chunk moves forward.
            cursor = max(cursor + 1, end - overlap)

    return chunks


def store_chunks(
    conn: psycopg.Connection,
    chunks: Sequence[ChunkInput],
    *,
    bulk: bool = False,
) -> list[UUID]:
    """Store chunks in input order using the caller's existing transaction.

    Bulk mode resolves section keys once per exact filing/report scope, checks
    every resolved range before writing, then uses bounded immutable inserts.
    Explicit section IDs and missing section keys retain the existing database
    constraint behavior. This helper never commits, rolls back, or opens a
    transaction or savepoint.
    """
    if not bulk:
        return [store_chunk(conn, chunk) for chunk in chunks]

    from .bulk_storage import store_many

    if conn.autocommit:
        raise DatabaseError("Write helpers require autocommit=False and caller-managed transactions.")
    if isinstance(chunks, (str, bytes, Mapping)):
        raise DatabaseError("Bulk chunks must contain ChunkInput records.")
    try:
        items = tuple(chunks)
    except TypeError as exc:
        raise DatabaseError("Bulk chunks must contain ChunkInput records.") from exc
    if any(type(chunk) is not ChunkInput for chunk in items):
        raise DatabaseError("Bulk chunks must contain ChunkInput records.")
    if not items:
        return []

    report_filings, scopes = {}, {}
    for chunk in items:
        previous = report_filings.setdefault(chunk.report_id, chunk.filing_id)
        if previous != chunk.filing_id:
            raise DatabaseError("A report cannot belong to multiple filing scopes in one chunk batch.")
        if chunk.section_id is None:
            key = chunk.section or chunk.metadata.get("section_id")
            if isinstance(key, str) and key:
                scopes.setdefault((chunk.filing_id, chunk.report_id), None)

    for filing_id, report_id in scopes:
        rows = conn.execute(
            """SELECT id, section_key, offset_start, offset_end FROM sec.report_sections
               WHERE report_id = %s AND filing_id = %s FOR SHARE""",
            (report_id, filing_id),
        ).fetchall()
        scopes[(filing_id, report_id)] = {
            key: (section_id, start, end) for section_id, key, start, end in rows
        }

    resolved = []
    for chunk in items:
        if chunk.section_id is None:
            key = chunk.section or chunk.metadata.get("section_id")
            if isinstance(key, str) and key:
                section = scopes[(chunk.filing_id, chunk.report_id)].get(key)
                if section is not None:
                    section_id, start, end = section
                    if chunk.offset_start < start or chunk.offset_end > end:
                        raise DatabaseError("Chunk crosses its report section boundary.")
                    chunk = replace(chunk, section_id=section_id)
        resolved.append(chunk)
    return store_many(conn, "chunks", resolved, ("report_id", "chunking_version", "chunk_index"))
