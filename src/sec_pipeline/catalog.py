"""Explicit, immutable processing selections for convenient filing reads.

A set selects existing artifact versions; it never assigns a new identity to
those artifacts. Storing a set seals its selected scopes. Validation describes
checks performed by the caller, while coverage describes how much was loaded.
Neither storing nor preferring a set asserts human review. Helpers never commit.
Artifact writes and selections use read committed isolation so a writer waiting
for publication rechecks the committed seal; read-only isolation is unrestricted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .database import DataConflict, DatabaseError


STAGES = frozenset({"facts", "tables", "reports", "chunks"})
COVERAGE_VALUES = frozenset({"absent", "unknown", "partial", "complete"})


@dataclass(frozen=True, kw_only=True)
class ProcessingSetInput:
    filing_id: UUID
    set_key: str
    coverage: dict[str, str]
    validation_status: str
    validation: dict[str, Any]
    configuration: dict[str, Any]
    fact_source_document_id: UUID | None = None
    fact_extraction_version: str | None = None
    table_source_document_id: UUID | None = None
    table_extraction_version: str | None = None
    report_id: UUID | None = None
    chunking_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.set_key, str) or not self.set_key.strip():
            raise DatabaseError("A processing set needs a nonempty set_key.")
        if not isinstance(self.coverage, dict) or set(self.coverage) != STAGES:
            raise DatabaseError("Coverage must explicitly describe facts, tables, reports, and chunks.")
        if any(not isinstance(value, str) or value not in COVERAGE_VALUES for value in self.coverage.values()):
            raise DatabaseError("Coverage values must be absent, unknown, partial, or complete.")
        for document, version in (
            (self.fact_source_document_id, self.fact_extraction_version),
            (self.table_source_document_id, self.table_extraction_version),
        ):
            if (document is None) != (version is None):
                raise DatabaseError("A selected fact/table stage needs both source document and version.")
        for version in (self.fact_extraction_version, self.table_extraction_version, self.chunking_version):
            if version is not None and (not isinstance(version, str) or not version.strip()):
                raise DatabaseError("Selected versions must be nonempty strings.")
        if self.chunking_version is not None and self.report_id is None:
            raise DatabaseError("A chunk selection needs its exact report_id.")
        selected = {
            "facts": self.fact_source_document_id is not None,
            "tables": self.table_source_document_id is not None,
            "reports": self.report_id is not None,
            "chunks": self.chunking_version is not None,
        }
        if not any(selected.values()):
            raise DatabaseError("A processing set must select at least one stage.")
        if any((self.coverage[stage] == "absent") == present for stage, present in selected.items()):
            raise DatabaseError("Coverage must be absent exactly when its stage is not selected.")
        if not isinstance(self.validation_status, str) or self.validation_status not in {"unvalidated", "passed", "failed"}:
            raise DatabaseError("Validation status must be explicitly unvalidated, passed, or failed.")
        if not isinstance(self.validation, dict) or not isinstance(self.configuration, dict):
            raise DatabaseError("Validation and configuration must be JSON objects.")
        checks = self.validation.get("checks")
        if self.validation_status == "passed" and (
            not isinstance(checks, dict) or not checks or any(value is not True for value in checks.values())
            or any(not isinstance(name, str) or not name.strip() for name in checks)
        ):
            raise DatabaseError("Passed validation requires nonempty named checks with boolean true results.")


def store_processing_set(conn: psycopg.Connection, record: ProcessingSetInput) -> UUID:
    """Seal an explicit selection and return its ID, including on identical replay.

    The database computes counts and validates selected scopes under a filing
    lock. A changed set_key identity raises DataConflict. Existing output IDs
    are reused. Partial coverage may pass validation of its declared scope.
    """
    if conn.autocommit:
        raise DatabaseError("Processing set writes require caller-managed transactions.")
    if not isinstance(record, ProcessingSetInput):
        raise DatabaseError("Expected ProcessingSetInput.")
    data = asdict(record)
    # Validate again because the dictionaries held by the frozen dataclass may
    # have been mutated by the caller since construction.
    ProcessingSetInput(**data)
    conn.execute("SELECT sec.lock_processing_filing(%s)", (record.filing_id,))
    columns = list(data)
    values = [Jsonb(data[name]) if name in {"coverage", "validation", "configuration"}
              else data[name] for name in columns]
    existing = conn.execute(
        "SELECT id FROM sec.filing_processing_sets WHERE filing_id=%s AND set_key=%s",
        (record.filing_id, record.set_key),
    ).fetchone()
    if existing:
        predicates = sql.SQL(" AND ").join(
            sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(name)) for name in columns
        )
        identical = conn.execute(sql.SQL(
            "SELECT id FROM sec.filing_processing_sets WHERE {}"
        ).format(predicates), values).fetchone()
        if identical:
            return identical[0]
        raise DataConflict("Conflicting processing set identity; use a new set_key.")
    row = conn.execute(sql.SQL(
        "INSERT INTO sec.filing_processing_sets ({}) VALUES ({}) RETURNING id"
    ).format(sql.SQL(", ").join(map(sql.Identifier, columns)),
             sql.SQL(", ").join(sql.Placeholder() for _ in columns)), values).fetchone()
    return row[0]


def select_preferred_processing_set(
    conn: psycopg.Connection, filing_id: UUID, processing_set_id: UUID,
) -> None:
    """Explicitly select a passed set; partial coverage remains visible in views.

    Selecting this set does not claim it contains a complete filing. The caller
    must supply validation and coverage when creating the set; no result is
    automatically promoted by ingestion or by this module.
    """
    if conn.autocommit:
        raise DatabaseError("Processing set writes require caller-managed transactions.")
    conn.execute("SELECT sec.lock_processing_filing(%s)", (filing_id,))
    conn.execute("""INSERT INTO sec.preferred_processing_sets (filing_id, processing_set_id)
        VALUES (%s, %s) ON CONFLICT (filing_id) DO UPDATE
        SET processing_set_id = EXCLUDED.processing_set_id,
            selected_at = CASE WHEN sec.preferred_processing_sets.processing_set_id
                = EXCLUDED.processing_set_id THEN sec.preferred_processing_sets.selected_at ELSE now() END""",
        (filing_id, processing_set_id))
