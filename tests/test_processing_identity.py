"""Manifest provenance changes retain strict source and parser identity checks."""

from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest

from sec_pipeline import backfill_v4
from sec_pipeline.database import DataConflict
from sec_pipeline.parser_policy import policy_metadata
from sec_pipeline.processing_identity import same_processing_identity
from sec_pipeline.source_set_storage import ReportMembershipInput, store_v4_processing_set
from tests.test_backfill_v4 import ReceiptConnection, receipt, registered_documents
from tests.test_source_set_storage import Recorder, selection


def configuration():
    expected, _ = receipt()
    return expected | policy_metadata(expected['versions']) | {
        'core_manifest_sha256': 'f' * 64,
        'document_set': {'set_key': 'primary-inline-set', 'member_source_urls': [
            record['source_url'] for record in expected['source_documents'].values()
            if record['role'] in {'main_inline_xbrl_filing', 'supplemental_inline_xbrl_document'}]},
    }


@pytest.mark.parametrize('provenance', [
    {'document_set_manifest_sha256': '9' * 64},
    {'core_manifest_sha256': '8' * 64},
    {'document_set_manifest_sha256': '9' * 64, 'core_manifest_sha256': '8' * 64},
])
def test_digest_only_reacquisition_reuses_receipt_and_original_provenance(provenance):
    original = configuration()
    current = original | provenance
    _, row = receipt()
    row[0] = deepcopy(original)
    conn = ReceiptConnection(row, registered_documents(original))
    result = backfill_v4.stored_receipt(conn, 'accession', 'filing-import-v4', current, ['complete'])
    assert result is not None
    assert result['configuration'] == original
    assert result['document_set_manifest_sha256'] == original['document_set_manifest_sha256']
    assert result['processing_set_id'] == 'processing-set-id'
    assert current == original | provenance


@pytest.mark.parametrize('change', [
    lambda value: value['source_hashes'].update({'main.htm': '0' * 64}),
    lambda value: value['source_documents']['main.htm'].update(size_bytes=124),
    lambda value: value['source_documents']['main.htm'].update(role='supplemental_inline_xbrl_document'),
    lambda value: value['document_set']['member_source_urls'].pop(),
    lambda value: value['versions'].update(tables='tables-v5'),
    lambda value: value['parser_revisions'].update(reports='stale-parser'),
    lambda value: value.update(extraction_profile='another-profile'),
    lambda value: value.update(section_validation_policy='another-policy'),
    lambda value: value.update(core_manifest_sha256='invalid'),
    lambda value: value.update(document_set_manifest_sha256='invalid'),
])
def test_reacquisition_never_hides_changed_identity_or_invalid_provenance(change):
    original = configuration()
    current = deepcopy(original)
    current['document_set_manifest_sha256'] = '9' * 64
    change(current)
    assert not same_processing_identity(original, current)


@pytest.mark.parametrize('missing', [
    'versions', 'parser_revisions', 'source_hashes', 'source_documents', 'document_set', 'core_manifest_sha256',
])
def test_sparse_receipts_still_require_exact_configuration(missing):
    original = configuration()
    original.pop(missing)
    assert same_processing_identity(original, deepcopy(original))
    assert not same_processing_identity(original, original | {'document_set_manifest_sha256': '9' * 64})


@pytest.mark.parametrize('stored,current', [
    ({'fixture': 1}, {'fixture': True}),
    ({'fixture': {'nested': [0]}}, {'fixture': {'nested': [False]}}),
])
def test_seal_replay_preserves_json_boolean_and_number_distinction(stored, current):
    assert not same_processing_identity(stored, current)
    conn = Recorder()
    record = replace(selection(), configuration=stored)
    members = {'table_ids': [UUID(int=5)], 'reports': [ReportMembershipInput(report_id=UUID(int=7))]}
    store_v4_processing_set(conn, record, **members)
    before = deepcopy(conn.tables)
    with pytest.raises(DataConflict):
        store_v4_processing_set(conn, replace(record, configuration=current), **members)
    assert conn.tables == before


def test_digest_only_comparison_also_preserves_nested_json_types():
    original = configuration() | {'extra': {'value': [1]}}
    current = original | {'document_set_manifest_sha256': '9' * 64, 'extra': {'value': [True]}}
    assert not same_processing_identity(original, current)


def test_explicit_seal_replay_keeps_first_configuration_and_every_member():
    conn = Recorder()
    original = configuration()
    record = replace(selection(), configuration=original)
    reports = [ReportMembershipInput(report_id=UUID(int=7))]
    members = {'table_ids': [UUID(int=5)], 'reports': reports}
    identifier = store_v4_processing_set(conn, record, **members)
    before = deepcopy(conn.tables)
    reacquired = replace(record, configuration=original | {
        'document_set_manifest_sha256': '9' * 64, 'core_manifest_sha256': '8' * 64})
    assert store_v4_processing_set(conn, reacquired, **members) == identifier
    assert conn.tables == before
    for altered in [
        replace(reacquired, configuration=reacquired.configuration | {'source_hashes': {'changed': '0' * 64}}),
        replace(reacquired, configuration=reacquired.configuration | {'parser_revisions': {'reports': 'stale'}}),
    ]:
        with pytest.raises(DataConflict):
            store_v4_processing_set(conn, altered, **members)
        assert conn.tables == before
    with pytest.raises(DataConflict):
        store_v4_processing_set(conn, reacquired, table_ids=[UUID(int=99)], reports=reports)
    assert conn.tables == before
