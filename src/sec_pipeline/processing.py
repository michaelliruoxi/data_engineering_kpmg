"""Source bindings and the transaction controller for ingestion adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
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


@dataclass(frozen=True)
class PipelineContext:
    """Verified inputs and prior stage results shared by write adapters."""

    manifest: Mapping[str, Any]
    data_root: Path
    source_ids: SourceIds
    versions: Mapping[str, str]
    outputs: Mapping[str, Any]


@dataclass(frozen=True)
class ValidationResult:
    """Explicit check results for the exact requested processing versions."""

    versions: Mapping[str, str]
    checks: Mapping[str, bool | None]


Stage = Callable[[Any, PipelineContext], Any]


@dataclass(frozen=True)
class PipelineStages:
    """Runner-owned write adapter contract; this is not a parser API.

    Each adapter uses the supplied connection for storage and returns its
    results. Parsers called by an adapter must remain database-independent.
    Facts precede table links; the stored report ID is available to chunks.
    """

    facts: Stage
    tables: Stage
    report: Stage
    chunks: Stage
    validate: Callable[[Any, PipelineContext], ValidationResult]


def run_ingestion_transaction(
    conn: Any,
    *,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    data_root: Path,
    versions: Mapping[str, str],
    stages: PipelineStages,
    required_checks: Sequence[str],
    seed_metadata: Callable[[Any, Path, Path], Mapping[str, Any]] | None = None,
) -> PipelineContext:
    """Run metadata and configured write adapters in one owned transaction.

    The caller must first run load_manifest() and verify_sample(), then pass
    that verified manifest and a fresh, non-autocommit connection. The default
    metadata writer is seed_manifest(), which rechecks source byte hashes.
    All adapters are required: no stage is skipped when a module is missing.
    This controller does not open connections, download files, or implement
    the team's parsers. seed_metadata is an offline-test dependency seam.
    """
    stage_names = ("facts", "tables", "report", "chunks")
    version_names = {"facts", "tables", "reports", "chunks"}
    if not isinstance(versions, Mapping) or set(versions) != version_names:
        raise ProcessingError("Supply facts, tables, reports, and chunks versions.")
    expected_versions = dict(versions)
    if any(not isinstance(v, str) or not v.strip() for v in expected_versions.values()):
        raise ProcessingError("Every processing version must be non-empty.")
    if not isinstance(stages, PipelineStages) or any(
        not callable(getattr(stages, name)) for name in (*stage_names, "validate")
    ):
        raise ProcessingError("All processing and validation adapters are required.")
    if isinstance(required_checks, str):
        raise ProcessingError("Supply a non-empty sequence of required check names.")
    checks = tuple(required_checks)
    if not checks or any(not isinstance(name, str) or not name.strip() for name in checks):
        raise ProcessingError("Supply a non-empty sequence of required check names.")
    if len(set(checks)) != len(checks):
        raise ProcessingError("Required check names must be unique.")
    if not isinstance(manifest, Mapping) or manifest.get("status") != "verified":
        raise ProcessingError("The caller must provide a verified manifest.")
    manifest_path, data_root = Path(manifest_path), Path(data_root).resolve()
    try:
        on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProcessingError("Cannot read the verified manifest.") from exc
    if on_disk != dict(manifest):
        raise ProcessingError("The manifest changed after input verification.")
    if conn.closed:
        raise ProcessingError("The ingestion connection is closed.")
    if conn.autocommit:
        raise ProcessingError("Ingestion requires autocommit=False.")
    if conn.info.transaction_status.name != "IDLE":
        raise ProcessingError("Ingestion requires a fresh idle connection.")
    if seed_metadata is None:
        from .database import seed_manifest

        seed_metadata = seed_manifest

    phase = "transaction"
    completed = False
    outputs: dict[str, Any] = {}
    try:
        with conn.transaction():
            phase = "metadata"
            seed = seed_metadata(conn, manifest_path, data_root)
            context = PipelineContext(
                manifest=MappingProxyType(dict(manifest)),
                data_root=data_root,
                source_ids=resolve_source_ids(manifest, seed),
                versions=MappingProxyType(expected_versions),
                outputs=MappingProxyType(outputs),
            )
            for phase in stage_names:
                result = getattr(stages, phase)(conn, context)
                if result is None:
                    raise ProcessingError(f"The {phase} adapter returned no result.")
                outputs[phase] = result
            phase = "validation"
            validation = stages.validate(conn, context)
            if not isinstance(validation, ValidationResult):
                raise ProcessingError("Validation did not return explicit check results.")
            if dict(validation.versions) != expected_versions:
                raise ProcessingError("Validation used different processing versions.")
            if not isinstance(validation.checks, Mapping):
                raise ProcessingError("Validation checks must be a mapping.")
            if set(checks) - validation.checks.keys():
                raise ProcessingError("Required validation checks are missing.")
            if any(value is not True for value in validation.checks.values()):
                raise ProcessingError("Validation failed or contains unverified checks.")
            outputs["validation"] = validation
            phase = "commit"
            completed = True
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError(f"Ingestion failed during {phase}.") from exc
    if not completed:
        raise ProcessingError("The ingestion transaction was rolled back.")
    return context
