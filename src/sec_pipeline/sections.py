"""Canonical report sections with exact offsets into a specific extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .database import ChunkInput, DataConflict, DatabaseError, ReportInput


# Keep codes in sync with the migration's seeded section_types vocabulary.
SECTION_TYPES = {
    "unknown", "front_matter", "part_heading", "business", "risk_factors",
    "cybersecurity", "properties", "legal_proceedings", "mine_safety",
    "market_equity", "equity_sales", "defaults", "other_information", "exhibits",
    "financial_statements", "mda", "market_risk", "controls", "accounting_changes",
    "directors_governance", "executive_compensation", "ownership",
    "related_party_transactions", "audit_fees", "summary",
}


@dataclass(frozen=True)
class ReportSection:
    section_key: str
    section_type: str
    original_title: str
    part: str | None
    item: str | None
    section_order: int
    offset_start: int
    offset_end: int
    source_locator: dict[str, Any]
    validation_status: str


def normalize_sections(report: ReportInput) -> list[ReportSection]:
    """Validate extractor descriptors without inventing labels for legacy rows."""
    result: list[ReportSection] = []
    keys: set[str] = set()
    previous_end = 0
    for index, descriptor in enumerate(report.sections):
        if not isinstance(descriptor, dict):
            raise DatabaseError("Each report section must be an object.")
        key = descriptor.get("section_key")
        if key is None:
            key = descriptor.get("section_id")
        if key is None:
            key = f"legacy-section-{index}"
        if not isinstance(key, str) or not key.strip() or key in keys:
            raise DatabaseError("Report sections need distinct nonempty section keys.")
        start, end = descriptor.get("offset_start"), descriptor.get("offset_end")
        if (type(start) is not int or type(end) is not int
                or start < previous_end or end <= start or end > len(report.full_text)):
            raise DatabaseError("Section offsets must be ordered nonoverlapping report character ranges.")
        section_type = descriptor.get("section_type", "unknown")
        if section_type not in SECTION_TYPES:
            raise DatabaseError(f"Unknown canonical section type: {section_type}")
        status = descriptor.get("validation_status", "unverified")
        if status not in {"unverified", "detected", "validated"}:
            raise DatabaseError("Invalid section validation status.")
        title = descriptor.get("original_title", descriptor.get("title", key))
        if not isinstance(title, str) or not title.strip():
            raise DatabaseError("A section title must be nonempty text.")
        part, item = descriptor.get("part"), descriptor.get("item")
        if any(value is not None and (not isinstance(value, str) or not value.strip())
               for value in (part, item)):
            raise DatabaseError("Section part and item must be nonempty text or null.")
        locator = descriptor.get("source_locator")
        if locator is None:
            locator = {}
        elif isinstance(locator, str):
            locator = {"legacy_locator": locator}
        elif not isinstance(locator, dict):
            raise DatabaseError("A section source locator must be an object or legacy string.")
        result.append(ReportSection(key, section_type, title, part, item, index,
                                    start, end, locator, status))
        keys.add(key)
        previous_end = end
    return result


def store_report_sections(
    conn: psycopg.Connection, report: ReportInput, *, report_id: UUID,
) -> dict[str, UUID]:
    """Store section descriptors after their report; never commit or overwrite."""
    if conn.autocommit:
        raise DatabaseError("Write helpers require autocommit=False and caller-managed transactions.")
    sections = normalize_sections(report)
    stored_report = conn.execute(
        "SELECT filing_id, full_text, sections FROM sec.reports WHERE id = %s FOR SHARE",
        (report_id,),
    ).fetchone()
    if stored_report != (report.filing_id, report.full_text, report.sections):
        raise DataConflict("Section input does not match the stored report extraction.")
    ids: dict[str, UUID] = {}
    for section in sections:
        data = vars(section) | {"filing_id": report.filing_id, "report_id": report_id}
        columns = list(data)
        values = [Jsonb(value) if name == "source_locator" else value
                  for name, value in data.items()]
        row = conn.execute(sql.SQL(
            "INSERT INTO sec.report_sections ({}) VALUES ({}) "
            "ON CONFLICT (report_id, section_key) DO NOTHING RETURNING id"
        ).format(sql.SQL(", ").join(map(sql.Identifier, columns)),
                 sql.SQL(", ").join(sql.Placeholder() for _ in columns)), values).fetchone()
        if row is None:
            row = conn.execute(sql.SQL(
                "SELECT id FROM sec.report_sections WHERE {} FOR SHARE"
            ).format(sql.SQL(" AND ").join(
                sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(name))
                for name in columns)), values).fetchone()
        if row is None:
            raise DataConflict("Conflicting report section identity; use a new extraction version.")
        ids[section.section_key] = row[0]
    return ids


def section_id_for_chunk(conn: psycopg.Connection, chunk: ChunkInput) -> UUID | None:
    """Resolve a legacy key within the chunk's exact report and character range."""
    explicit_id = getattr(chunk, "section_id", None)
    if explicit_id is not None:
        # Database constraints verify the report identity and containment even
        # when the caller supplies a UUID instead of a legacy key.
        return explicit_id
    key = chunk.section or chunk.metadata.get("section_id")
    if not isinstance(key, str) or not key:
        return None
    row = conn.execute(
        """SELECT id, offset_start, offset_end FROM sec.report_sections
           WHERE report_id = %s AND filing_id = %s AND section_key = %s FOR SHARE""",
        (chunk.report_id, chunk.filing_id, key),
    ).fetchone()
    if row is None:
        return None
    if chunk.offset_start < row[1] or chunk.offset_end > row[2]:
        raise DatabaseError("Chunk crosses its report section boundary.")
    return row[0]
