"""Reviewed changes use fresh identities and truthful current-parser receipts."""
from dataclasses import replace
from copy import deepcopy

import pytest

from sec_pipeline import filing_parser_v4 as parser, filing_ingestion_v6
from sec_pipeline.extraction_profiles import (
    LEGACY_PROFILE, CORRECTED_PROFILE, REVIEWED_PROFILE, corrected_tables,
    supported_versions, versions_for,
)
from sec_pipeline.parser_policy import policy_metadata
from sec_pipeline.v4_table_cells import v4_relational_cells
from tests.test_corrected_profile import original_sample_set
from tests.test_v4_table_cells import conflict_fixture


def test_profiles_have_distinct_immutable_artifact_identities():
    legacy, corrected, reviewed = [versions_for(p) for p in
                                 (LEGACY_PROFILE, CORRECTED_PROFILE, REVIEWED_PROFILE)]
    assert all(supported_versions(v) for v in (legacy, corrected, reviewed))
    assert reviewed == {'facts': 'facts-v1', 'tables': 'tables-v6',
                        'reports': 'report-v3', 'chunks': 'paragraph-v2'}
    assert len({policy_metadata(v)['parser_revisions']['reports']
                for v in (legacy, corrected, reviewed)}) == 3
    assert policy_metadata(reviewed)['parser_revisions']['chunks'] == 'token-boundary-overlap-v2'
    assert not supported_versions(reviewed | {'reports': 'report-v2'})


def test_reviewed_cell_issue_ids_do_not_collide_with_v4_or_v5():
    table, _, _ = conflict_fixture()
    old = deepcopy(table)
    corrected, = corrected_tables([table])
    reviewed, = corrected_tables([table], profile=REVIEWED_PROFILE)
    assert table == old
    assert reviewed.extraction_version == 'tables-v6'
    assert reviewed.structure['rows'][0][1]['quality_issue_keys'] == [
        'tables-v6:' + key for key in table.structure['rows'][0][1]['quality_issue_keys']]
    assert reviewed.structure['rows'][0][1]['source_fact_references'] == corrected.structure['rows'][0][1]['source_fact_references']
    assert v4_relational_cells(table.filing_id, reviewed)


def test_reviewed_original_source_rebuild_and_dry_run(original_sample_set):
    path, root = original_sample_set
    item = parser.parse_filing_set(path, root, profile=REVIEWED_PROFILE)
    assert item.versions == filing_ingestion_v6.VERSIONS
    assert {table.extraction_version for table in item.tables} == {'tables-v6'}
    assert {doc.report.extraction_version for doc in item.documents} == {'report-v3'}
    assert item.semantic_checks['primary_section_boundaries'] is True
    for document in item.documents:
        text = document.report.full_text
        for chunk in document.chunks:
            assert chunk.chunking_version == 'paragraph-v2'
            assert chunk.text == text[chunk.offset_start:chunk.offset_end]
            assert chunk.offset_start == 0 or not (
                text[chunk.offset_start - 1].isalnum() and text[chunk.offset_start].isalnum())
    result = filing_ingestion_v6.configuration_from_manifest(item.manifest, item.manifest_sha256)
    assert result['extraction_profile'] == REVIEWED_PROFILE
    assert result['section_validation_policy'] == 'source-boundaries-v2'
    assert result['parser_revisions'] == policy_metadata(item.versions)['parser_revisions']


@pytest.mark.parametrize('present', [False, True])
def test_reviewed_preflight_stops_on_missing_or_mismatched_migration(monkeypatch, present):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from sec_pipeline.filing_ingestion_v5 import MigrationPrerequisiteError
    statements = []
    def execute(sql, parameters):
        statements.append((sql, parameters))
        return SimpleNamespace(fetchone=lambda: ('0' * 64,) if present else None)
    conn = SimpleNamespace(autocommit=False, closed=False,
        info=SimpleNamespace(transaction_status=SimpleNamespace(name='IDLE')),
        transaction=lambda: nullcontext(), execute=execute)
    with pytest.raises(MigrationPrerequisiteError):
        filing_ingestion_v6.preflight(conn)
    assert len(statements) == 1
    assert statements[0][0].startswith('SELECT sha256')
