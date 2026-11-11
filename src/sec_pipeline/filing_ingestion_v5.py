"""Corrected reports/tables with additive identities and explicit publication."""

from . import filing_ingestion_v4 as runner
from .database import DatabaseError, migration_checksum_matches, _packaged_migrations
from .extraction_profiles import CORRECTED_PROFILE, versions_for

VERSIONS = versions_for(CORRECTED_PROFILE)
REQUIRED_SOURCE_CHECKS = runner.REQUIRED_SOURCE_CHECKS
MIGRATION = "011_corrected_layout_versions.sql"
MIGRATIONS = (MIGRATION, "012_publication_readers.sql", "013_reviewed_layout_versions.sql",
              "014_reader_contracts.sql", "015_parser_revision_contracts.sql")


def configuration_from_manifest(manifest, manifest_sha256):
    return runner.configuration_from_manifest(manifest, manifest_sha256, profile=CORRECTED_PROFILE)


def run_dry_run(manifest_path, data_root):
    return runner.run_dry_run(manifest_path, data_root, profile=CORRECTED_PROFILE)


class MigrationPrerequisiteError(DatabaseError):
    """A global schema prerequisite, not a bad filing."""


def preflight(conn):
    if conn.autocommit or conn.closed or conn.info.transaction_status.name != "IDLE":
        raise DatabaseError("Corrected ingestion requires an idle caller-owned connection.")
    # Confirm the entire atomic migration, including reader gating, before writes.
    with conn.transaction():
        packaged = dict(_packaged_migrations())
        for name in MIGRATIONS:
            row = conn.execute("SELECT sha256 FROM sec.schema_migrations WHERE name=%s", (name,)).fetchone()
            if row is None or not migration_checksum_matches(row[0], packaged[name]):
                raise MigrationPrerequisiteError("Apply the reviewed corrected-layout migrations before ingestion.")


def run_ingestion(conn, *, manifest_path, data_root, processing_set_key="automated-v5", coverage=None):
    preflight(conn)
    return runner.run_ingestion(conn, manifest_path=manifest_path, data_root=data_root,
                                processing_set_key=processing_set_key, coverage=coverage,
                                profile=CORRECTED_PROFILE)
