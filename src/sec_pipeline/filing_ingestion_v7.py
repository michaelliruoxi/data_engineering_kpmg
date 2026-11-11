"""Audited extraction: reviewed boundaries with corrected section metadata and statement inventories."""

from . import filing_ingestion_v4 as runner
from .database import DatabaseError, migration_checksum_matches, _packaged_migrations
from .extraction_profiles import AUDITED_PROFILE, versions_for
from .filing_ingestion_v5 import MigrationPrerequisiteError

VERSIONS = versions_for(AUDITED_PROFILE)
REQUIRED_SOURCE_CHECKS = runner.REQUIRED_SOURCE_CHECKS
MIGRATIONS = ("011_corrected_layout_versions.sql", "012_publication_readers.sql", "013_reviewed_layout_versions.sql",
              "014_reader_contracts.sql", "015_parser_revision_contracts.sql", "016_audited_layout_versions.sql")


def configuration_from_manifest(manifest, manifest_sha256):
    return runner.configuration_from_manifest(manifest, manifest_sha256, profile=AUDITED_PROFILE)


def run_dry_run(manifest_path, data_root):
    return runner.run_dry_run(manifest_path, data_root, profile=AUDITED_PROFILE)


def preflight(conn):
    if conn.autocommit or conn.closed or conn.info.transaction_status.name != "IDLE":
        raise DatabaseError("Audited ingestion requires an idle caller-owned connection.")
    packaged = dict(_packaged_migrations())
    with conn.transaction():
        for name in MIGRATIONS:
            row = conn.execute("SELECT sha256 FROM sec.schema_migrations WHERE name=%s", (name,)).fetchone()
            if row is None or not migration_checksum_matches(row[0], packaged[name]):
                raise MigrationPrerequisiteError("Apply the audited layout migration before ingestion.")


def run_ingestion(conn, *, manifest_path, data_root, processing_set_key="automated-v7", coverage=None):
    preflight(conn)
    return runner.run_ingestion(conn, manifest_path=manifest_path, data_root=data_root,
                                processing_set_key=processing_set_key, coverage=coverage,
                                profile=AUDITED_PROFILE)
