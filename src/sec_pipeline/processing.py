"""Source bindings and the transaction controller for ingestion adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4


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
    reference: Mapping[str, Any] = field(default_factory=dict, kw_only=True)


@dataclass(frozen=True)
class ValidationResult:
    """Explicit check results for the exact requested processing versions."""

    versions: Mapping[str, str]
    checks: Mapping[str, bool | None]


def _processing_options(
    versions: Mapping[str, str], required_checks: Sequence[str]
) -> tuple[dict[str, str], tuple[str, ...]]:
    if not isinstance(versions, Mapping) or set(versions) != {"facts", "tables", "reports", "chunks"}:
        raise ProcessingError("Supply facts, tables, reports, and chunks versions.")
    expected = dict(versions)
    if any(not isinstance(value, str) or not value.strip() for value in expected.values()):
        raise ProcessingError("Every processing version must be non-empty.")
    if not isinstance(required_checks, Sequence) or isinstance(required_checks, str):
        raise ProcessingError("Supply a non-empty sequence of required check names.")
    checks = tuple(required_checks)
    if not checks or any(not isinstance(name, str) or not name.strip() for name in checks):
        raise ProcessingError("Supply a non-empty sequence of required check names.")
    if len(set(checks)) != len(checks):
        raise ProcessingError("Required check names must be unique.")
    return expected, checks


def _check_validation(
    validation: ValidationResult, versions: Mapping[str, str], checks: Sequence[str]
) -> None:
    if not isinstance(validation, ValidationResult):
        raise ProcessingError("Validation did not return explicit check results.")
    if not isinstance(validation.versions, Mapping) or dict(validation.versions) != dict(versions):
        raise ProcessingError("Validation used different processing versions.")
    if not isinstance(validation.checks, Mapping):
        raise ProcessingError("Validation checks must be a mapping.")
    if set(checks) - validation.checks.keys():
        raise ProcessingError("Required validation checks are missing.")
    if any(value is not True for value in validation.checks.values()):
        raise ProcessingError("Validation failed or contains unverified checks.")


def _check_manifest(manifest: Mapping[str, Any], manifest_path: Path) -> None:
    if not isinstance(manifest, Mapping) or manifest.get("status") != "verified":
        raise ProcessingError("The caller must provide a verified manifest.")
    try:
        on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProcessingError("Cannot read the verified manifest.") from exc
    if on_disk != dict(manifest):
        raise ProcessingError("The manifest changed after input verification.")


def _require_active_transaction(conn: Any, phase: str) -> None:
    if conn.closed or conn.info.transaction_status.name != "INTRANS":
        raise ProcessingError(f"The ingestion transaction is inactive or aborted after {phase}.")


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
    reference: Mapping[str, Any] | None = None,
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
    expected_versions, checks = _processing_options(versions, required_checks)
    if not isinstance(stages, PipelineStages) or any(
        not callable(getattr(stages, name)) for name in (*stage_names, "validate")
    ):
        raise ProcessingError("All processing and validation adapters are required.")
    manifest_path, data_root = Path(manifest_path), Path(data_root).resolve()
    _check_manifest(manifest, manifest_path)
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
            _require_active_transaction(conn, phase)
            context = PipelineContext(
                manifest=MappingProxyType(deepcopy(dict(manifest))),
                data_root=data_root,
                source_ids=resolve_source_ids(manifest, seed),
                versions=MappingProxyType(expected_versions),
                outputs=MappingProxyType(outputs),
                reference=MappingProxyType(deepcopy(dict(reference or {}))),
            )
            for phase in stage_names:
                result = getattr(stages, phase)(conn, context)
                if result is None:
                    raise ProcessingError(f"The {phase} adapter returned no result.")
                _require_active_transaction(conn, phase)
                outputs[phase] = result
            phase = "validation"
            validation = stages.validate(conn, context)
            _require_active_transaction(conn, phase)
            _check_validation(validation, expected_versions, checks)
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


@dataclass(frozen=True)
class ParseResult:
    """Materialized parser items and diagnostics, without storage operations."""

    items: Sequence[Any]
    rejected: Sequence[Any] = ()
    unresolved_cells: int = 0

    def __post_init__(self):
        for name in ("items", "rejected"):
            value = getattr(self, name)
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise ProcessingError(f"ParseResult.{name} must be a sequence of records.")
            object.__setattr__(self, name, tuple(value))
        if type(self.unresolved_cells) is not int or self.unresolved_cells < 0:
            raise ProcessingError("Unresolved-cell counts must be non-negative integers.")


@dataclass(frozen=True)
class ParsedReport:
    """Cleaned text and Unicode character sections for the actual chunker."""

    full_text: str
    sections: Sequence[Mapping[str, Any]]

    def __post_init__(self):
        if not isinstance(self.full_text, str) or not self.full_text.strip():
            raise ProcessingError("The report parser must return non-empty cleaned text.")
        if not isinstance(self.sections, Sequence) or isinstance(self.sections, (str, bytes)):
            raise ProcessingError("Report sections must be a sequence.")
        object.__setattr__(self, "sections", tuple(self.sections))


@dataclass(frozen=True)
class DryRunContext(PipelineContext):
    """Pure adapters receive temporary IDs and references, never a connection."""

    report_id: UUID


def chunk_dry_run_report(context: DryRunContext) -> ParseResult:
    """Use Ruby's real chunker against the final cleaned report text."""
    from .chunking import chunk_report

    result = context.outputs["report"]
    if len(result.items) != 1 or not isinstance(result.items[0], ParsedReport):
        raise ProcessingError("The report adapter must return one ParsedReport.")
    report = result.items[0]
    return ParseResult(chunk_report(
        filing_id=context.source_ids.filing_id, report_id=context.report_id,
        full_text=report.full_text, sections=report.sections,
        chunking_version=context.versions["chunks"],
    ))


PureStage = Callable[[DryRunContext], ParseResult]


@dataclass(frozen=True)
class DryRunStages:
    """Runner-owned pure adapter API; teammate signatures are adapted here."""

    facts: PureStage
    tables: PureStage
    report: PureStage
    validate: Callable[[DryRunContext], ValidationResult]
    chunks: PureStage = chunk_dry_run_report


@dataclass(frozen=True)
class DryRunPlan:
    stages: DryRunStages
    required_checks: Sequence[str]


def _reference_options(reference: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, int]]:
    if reference is None:
        reference = {}
    if not isinstance(reference, Mapping):
        raise ProcessingError("The reference must be a JSON object.")
    expected = reference.get("expected_counts", {})
    if not isinstance(expected, Mapping) or set(expected) - {"facts", "tables", "reports", "chunks"}:
        raise ProcessingError("Reference expected_counts must name processing stages.")
    if any(type(value) is not int or value < 0 for value in expected.values()):
        raise ProcessingError("Reference counts must be non-negative integers.")
    return deepcopy(dict(reference)), dict(expected)


def run_dry_run(
    *,
    manifest_path: Path,
    data_root: Path,
    versions: Mapping[str, str],
    stages: DryRunStages,
    required_checks: Sequence[str],
    verify_inputs: Callable[[Path, Path], Mapping[str, Any]],
    reference: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify and parse all stages, returning counts without stored records.

    verify_inputs must use the existing load_manifest()/verify_sample() checks.
    Pure adapters accept only DryRunContext, without credentials or a connection.
    Temporary UUIDs and parsed records remain internal; the returned summary
    contains neither and cannot be used as an input for database storage.
    """
    expected_versions, checks = _processing_options(versions, required_checks)
    names = ("facts", "tables", "report", "chunks")
    if not isinstance(stages, DryRunStages) or any(
        not callable(getattr(stages, name)) for name in (*names, "validate")
    ):
        raise ProcessingError("All pure processing and validation adapters are required.")
    if not callable(verify_inputs):
        raise ProcessingError("A local input verifier is required for dry-run.")
    reference, expected_counts = _reference_options(reference)
    manifest_path, data_root = Path(manifest_path), Path(data_root).resolve()
    phase = "input verification"
    outputs: dict[str, ParseResult] = {}
    try:
        manifest = verify_inputs(manifest_path, data_root)
        _check_manifest(manifest, manifest_path)
        temporary_ids = SourceIds(uuid4(), uuid4(), uuid4())
        context = DryRunContext(
            manifest=MappingProxyType(deepcopy(dict(manifest))), data_root=data_root,
            source_ids=temporary_ids, versions=MappingProxyType(expected_versions),
            outputs=MappingProxyType(outputs), report_id=uuid4(),
            reference=MappingProxyType(deepcopy(dict(reference))),
        )
        for phase in names:
            result = getattr(stages, phase)(context)
            if not isinstance(result, ParseResult):
                raise ProcessingError(f"The {phase} adapter must return ParseResult.")
            if phase == "report" and (
                len(result.items) != 1 or not isinstance(result.items[0], ParsedReport)
            ):
                raise ProcessingError("The report adapter must return one ParsedReport.")
            outputs[phase] = result
        phase = "validation"
        validation = stages.validate(context)
        _check_validation(validation, expected_versions, checks)
        counts = {
            ("reports" if name == "report" else name): {
                "expected": expected_counts.get("reports" if name == "report" else name),
                "extracted": len(result.items), "stored": 0,
                "rejected": len(result.rejected), "unresolved_cells": result.unresolved_cells,
            }
            for name, result in outputs.items()
        }
        if any(item["expected"] is not None and item["expected"] != item["extracted"] for item in counts.values()):
            raise ProcessingError("Extracted counts do not match reference expected_counts.")
        phase = "source verification after parsing"
        if verify_inputs(manifest_path, data_root) != manifest:
            raise ProcessingError("Inputs changed during parsing.")
        _check_manifest(manifest, manifest_path)
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError(f"Dry-run failed during {phase}.") from exc
    records = [manifest["index_document"], *manifest["files"]]
    return {
        "mode": "dry-run", "accession_number": manifest["accession_number"],
        "source_hashes": {record["local_path"]: record["sha256"] for record in records},
        "versions": expected_versions, "counts": counts,
        "report_characters": len(outputs["report"].items[0].full_text),
        "validation": {
            "passed": True, "required_checks": list(checks), "checks": dict(validation.checks)
        },
    }


@dataclass(frozen=True)
class WriteResult:
    """Accepted parser records and their storage IDs, in matching order.

    IDs returned on replay count as stored records, not newly inserted rows.
    Rejected parser records remain diagnostics and must not be written.
    """

    parsed: ParseResult
    stored_ids: Sequence[UUID]

    def __post_init__(self):
        if not isinstance(self.parsed, ParseResult):
            raise ProcessingError("A write adapter must supply its ParseResult.")
        if not isinstance(self.stored_ids, Sequence) or isinstance(self.stored_ids, (str, bytes)):
            raise ProcessingError("Stored IDs must be a materialized sequence.")
        ids = tuple(_as_uuid(value, "stored record") for value in self.stored_ids)
        if len(ids) != len(self.parsed.items) or len(set(ids)) != len(ids):
            raise ProcessingError("Each accepted record must have one distinct stored ID.")
        object.__setattr__(self, "stored_ids", ids)


@dataclass(frozen=True)
class IngestionPlan:
    """Runner-owned write bridge, separate from teammates' pure parser APIs."""

    stages: PipelineStages
    required_checks: Sequence[str]


def check_ingestion_options(
    plan: IngestionPlan, versions: Mapping[str, str], reference: Mapping[str, Any] | None,
) -> None:
    """Reject missing configuration before the CLI opens a connection."""
    if not isinstance(plan, IngestionPlan) or not isinstance(plan.stages, PipelineStages) or any(
        not callable(getattr(plan.stages, name))
        for name in ("facts", "tables", "report", "chunks", "validate")
    ):
        raise ProcessingError("All ingestion and validation adapters are required in IngestionPlan.")
    _processing_options(versions, plan.required_checks)
    _reference_options(reference)


def store_ingestion_chunks(conn: Any, context: PipelineContext) -> WriteResult:
    """Chunk the stored report using Ruby's real parser and storage helper."""
    from .chunking import chunk_report, store_chunks

    report_result = context.outputs["report"]
    report = report_result.parsed.items[0]
    chunks = chunk_report(
        filing_id=context.source_ids.filing_id, report_id=report_result.stored_ids[0],
        full_text=report.full_text, sections=report.sections,
        chunking_version=context.versions["chunks"],
    )
    return WriteResult(ParseResult(chunks), store_chunks(conn, chunks))


def _check_write_result(name: str, result: Any, context: PipelineContext) -> None:
    from .database import ChunkInput, FactInput, FinancialTableInput, ReportInput

    if not isinstance(result, WriteResult):
        raise ProcessingError(f"The {name} adapter must return WriteResult.")
    types = {"facts": FactInput, "tables": FinancialTableInput, "report": ReportInput, "chunks": ChunkInput}
    version_key = "reports" if name == "report" else name
    source_id = context.source_ids.xml_document_id if name == "facts" else context.source_ids.html_document_id
    if name == "report" and len(result.parsed.items) != 1:
        raise ProcessingError("The report adapter must store exactly one ReportInput.")
    for record in result.parsed.items:
        if not isinstance(record, types[name]) or record.filing_id != context.source_ids.filing_id:
            raise ProcessingError(f"The {name} records must use the registered filing ID and storage input type.")
        if name == "chunks":
            if record.report_id != context.outputs["report"].stored_ids[0] or record.chunking_version != context.versions[version_key]:
                raise ProcessingError("Chunks must use the stored report ID and requested chunk version.")
        elif record.source_document_id != source_id or record.extraction_version != context.versions[version_key]:
            raise ProcessingError(f"The {name} records must use the registered source ID and requested version.")


def _stored_ids(conn: Any, name: str, context: PipelineContext) -> set[UUID]:
    """Inspect the actual selected rows inside the import transaction."""
    if name == "chunks":
        statement = """SELECT id FROM sec.chunks
            WHERE filing_id = %s AND report_id = %s AND chunking_version = %s"""
        parameters = (context.source_ids.filing_id, context.outputs["report"].stored_ids[0], context.versions["chunks"])
    else:
        statements = {
            "facts": "SELECT id FROM sec.financial_facts WHERE filing_id = %s AND source_document_id = %s AND extraction_version = %s",
            "tables": "SELECT id FROM sec.financial_tables WHERE filing_id = %s AND source_document_id = %s AND extraction_version = %s",
            "report": "SELECT id FROM sec.reports WHERE filing_id = %s AND source_document_id = %s AND extraction_version = %s",
        }
        statement = statements[name]
        source_id = context.source_ids.xml_document_id if name == "facts" else context.source_ids.html_document_id
        parameters = (context.source_ids.filing_id, source_id, context.versions["reports" if name == "report" else name])
    return {row[0] for row in conn.execute(statement, parameters).fetchall()}


def run_ingestion(
    conn: Any,
    *,
    manifest_path: Path,
    data_root: Path,
    versions: Mapping[str, str],
    plan: IngestionPlan,
    verify_inputs: Callable[[Path, Path], Mapping[str, Any]],
    reference: Mapping[str, Any] | None = None,
    seed_metadata: Callable[[Any, Path, Path], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Import with actual-row counts, source checks, and validation before commit.

    The CLI owns the connection; this function reuses the tested outer
    transaction controller. Adapters parse with registered IDs during this run,
    never using records or temporary IDs retained from a previous dry-run.
    """
    check_ingestion_options(plan, versions, reference)
    if not callable(verify_inputs):
        raise ProcessingError("A local input verifier is required for ingestion.")
    expected_versions, checks = _processing_options(versions, plan.required_checks)
    reference, expected_counts = _reference_options(reference)
    manifest_path, data_root = Path(manifest_path), Path(data_root).resolve()
    try:
        manifest = verify_inputs(manifest_path, data_root)
        _check_manifest(manifest, manifest_path)
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError("Ingestion failed during input verification.") from exc
    summary: dict[str, Any] = {}

    def writer(name):
        def write_stage(connection, context):
            result = getattr(plan.stages, name)(connection, context)
            _check_write_result(name, result, context)
            return result
        return write_stage

    def validate(connection, context):
        validation = plan.stages.validate(connection, context)
        _require_active_transaction(connection, "validation")
        _check_validation(validation, expected_versions, checks)
        counts = {}
        for name in ("facts", "tables", "report", "chunks"):
            result = context.outputs[name]
            actual_ids = _stored_ids(connection, name, context)
            if actual_ids != set(result.stored_ids):
                raise ProcessingError(f"Stored {name} rows do not match the adapter's accepted records.")
            key = "reports" if name == "report" else name
            counts[key] = {
                "expected": expected_counts.get(key), "extracted": len(result.parsed.items),
                "stored": len(actual_ids), "rejected": len(result.parsed.rejected),
                "unresolved_cells": result.parsed.unresolved_cells,
            }
            if counts[key]["expected"] is not None and counts[key]["expected"] != counts[key]["extracted"]:
                raise ProcessingError("Extracted counts do not match reference expected_counts.")
        try:
            if verify_inputs(manifest_path, data_root) != manifest:
                raise ProcessingError("Inputs changed during ingestion.")
            _check_manifest(manifest, manifest_path)
        except ProcessingError:
            raise
        except Exception as exc:
            raise ProcessingError("Source verification failed before ingestion commit.") from exc
        records = [manifest["index_document"], *manifest["files"]]
        summary.update({
            "mode": "ingest", "accession_number": manifest["accession_number"],
            "source_hashes": {record["local_path"]: record["sha256"] for record in records},
            "versions": expected_versions, "counts": counts,
            "report_characters": len(context.outputs["report"].parsed.items[0].full_text),
            "validation": {"passed": True, "required_checks": list(checks), "checks": dict(validation.checks)},
        })
        # A malformed summary must fail inside the transaction, before commit.
        json.dumps(summary)
        return validation

    run_ingestion_transaction(
        conn, manifest=manifest, manifest_path=manifest_path, data_root=data_root,
        versions=expected_versions,
        stages=PipelineStages(
            facts=writer("facts"), tables=writer("tables"), report=writer("report"),
            chunks=writer("chunks"), validate=validate,
        ),
        required_checks=checks, seed_metadata=seed_metadata, reference=reference,
    )
    return summary
