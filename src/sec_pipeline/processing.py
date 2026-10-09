"""Resolve verified filing sources for the ingestion workflow."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID


class ProcessingError(ValueError):
    """The ingestion workflow cannot safely continue."""


@dataclass(frozen=True)
class SourceIds:
    filing_id: UUID
    xml_document_id: UUID
    html_document_id: UUID


def _as_uuid(value: Any, label: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProcessingError(f"Invalid UUID for {label}.") from exc


def resolve_source_ids(
    manifest: Mapping[str, Any],
    seed_result: Mapping[str, Any],
) -> SourceIds:
    """Map a verified manifest to IDs returned by seed_manifest().

    This function does not connect to a database or modify files.
    """
    try:
        records = [manifest["index_document"], *manifest["files"]]
        document_ids = seed_result["document_ids"]
        filing_id = _as_uuid(seed_result["filing_id"], "filing_id")
    except (KeyError, TypeError) as exc:
        raise ProcessingError("Missing manifest or seeded metadata fields.") from exc

    if not isinstance(document_ids, Mapping):
        raise ProcessingError("document_ids must be keyed by local_path.")

    def source_id(role: str) -> UUID:
        if not all(isinstance(record, Mapping) for record in records):
            raise ProcessingError("Invalid source document record.")
        matches = [record for record in records if record.get("role") == role]
        if len(matches) != 1:
            raise ProcessingError(f"Expected exactly one source with role: {role}")
        local_path = matches[0].get("local_path")
        if not isinstance(local_path, str) or not local_path:
            raise ProcessingError(f"Missing local_path for role: {role}")
        if local_path not in document_ids:
            raise ProcessingError(f"No registered ID for local_path: {local_path}")
        return _as_uuid(document_ids[local_path], role)

    xml_id = source_id("extracted_xbrl_instance")
    html_id = source_id("main_inline_xbrl_filing")
    if xml_id == html_id:
        raise ProcessingError("XML and HTML must have different source IDs.")
    return SourceIds(filing_id, xml_id, html_id)