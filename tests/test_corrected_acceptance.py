"""Supplemental text remains source exact without inventing primary headings."""

from copy import deepcopy
from dataclasses import asdict
import hashlib
from uuid import UUID

import pytest

from sec_pipeline import corrected_acceptance as acceptance
from sec_pipeline.chunking import chunk_report
from sec_pipeline.extraction_profiles import CORRECTED_PROFILE, REVIEWED_PROFILE, versions_for
from sec_pipeline.parser_policy import policy_metadata
from sec_pipeline.report_cleaning import clean_report
from sec_pipeline.sections import normalize_sections
from tests.test_corrected_profile import original_sample_set


def snapshot_with_supplemental(tmp_path, original_sample_set, *, profile=CORRECTED_PROFILE):
    manifest_path, original_root = original_sample_set
    import json
    manifest = json.loads(manifest_path.read_text())
    primary_record = next(record for record in manifest['files'] if record['role'] == 'main_inline_xbrl_filing')
    primary = tmp_path / primary_record['local_path'].split('/')[-1]
    primary.write_bytes((original_root / primary_record['local_path']).read_bytes())
    supplemental = tmp_path / 'supplemental.htm'
    supplemental.write_text('<html><body><p>Original supplemental disclosure with no Part or Item headings.</p></body></html>')
    versions = versions_for(profile)
    snapshot = {'selected_processing_set': {'versions': versions,
        'configuration': {'versions': versions, **policy_metadata(versions)},
        'validation_status': 'passed', 'source_validation_status': 'passed',
        'semantic_checks': {'primary_section_boundaries': True}, 'document_set_id': 'set'},
        'filing': {'id': 'filing', 'form': '10-Q'}, 'sources': [], 'source_document_set_members': [],
        'reports': [], 'sections': [], 'chunks': []}
    for number, path in enumerate((primary, supplemental), 1):
        source_id, report_id = f'source-{number}', f'report-{number}'
        content = path.read_bytes()
        snapshot['sources'].append({'id': source_id, 'local_path': path.name,
            'sha256': hashlib.sha256(content).hexdigest(), 'size_bytes': len(content)})
        snapshot['source_document_set_members'].append({'document_set_id': 'set',
            'source_document_id': source_id, 'is_primary': number == 1})
        cleaned = clean_report(path, form='10-Q')
        snapshot['reports'].append({'id': report_id, 'source_document_id': source_id,
            'extraction_version': versions['reports'],
            'full_text': cleaned.full_text, 'sections': deepcopy(cleaned.sections)})
        section_ids = {}
        for index, section in enumerate(normalize_sections(cleaned)):
            identifier = f'{report_id}-section-{index}'
            section_ids[section.section_key] = identifier
            snapshot['sections'].append({'id': identifier, 'report_id': report_id, **asdict(section)})
        for chunk in chunk_report(filing_id=UUID(int=1), report_id=UUID(int=number),
                                  full_text=cleaned.full_text, sections=cleaned.sections,
                                  chunking_version=versions['chunks']):
            snapshot['chunks'].append(asdict(chunk) | {'report_id': report_id,
                'section_id': section_ids[chunk.section]})
    return snapshot


@pytest.mark.parametrize('change', ['none', 'supplemental_chunk', 'supplemental_as_primary'])
def test_supplemental_classification_does_not_weaken_source_fidelity(
        tmp_path, original_sample_set, monkeypatch, change):
    snapshot = snapshot_with_supplemental(tmp_path, original_sample_set)
    if change == 'supplemental_chunk':
        snapshot['chunks'][-1]['text'] += ' altered'
    elif change == 'supplemental_as_primary':
        for member in snapshot['source_document_set_members']:
            member['is_primary'] = not member['is_primary']
    monkeypatch.setattr(acceptance, 'read_snapshot', lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(acceptance, 'audit_snapshot', lambda *args, **kwargs: {'status': 'passed'})
    result = acceptance.audit_corrected_set(None, 'fixture', UUID(int=3), tmp_path)
    if change == 'none':
        assert result['status'] == 'passed', result
        supplement = result['reports'][1]
        assert supplement['status'] == 'unverified'
        assert supplement['heading_inventory_status'] == 'unverified'
        assert supplement['unclassified_supplemental_sections']
        assert supplement['issues'] == []
    else:
        assert result['status'] == 'failed'
        errors = [error for error in result['errors'] if error['check'] == 'stored_report_audit_failed']
        assert errors
        check = 'chunk_not_exact_slice' if change == 'supplemental_chunk' else 'unclassified_section'
        assert any(issue['check'] == check for error in errors for issue in error['issues'])


@pytest.mark.parametrize('profile', [CORRECTED_PROFILE, REVIEWED_PROFILE])
@pytest.mark.parametrize('revision_change', ['current', 'missing', 'old_facts', 'old_reports'])
def test_acceptance_requires_current_revisions_without_rewriting_historical_outputs(
        tmp_path, original_sample_set, monkeypatch, profile, revision_change):
    snapshot = snapshot_with_supplemental(tmp_path, original_sample_set, profile=profile)
    configuration = snapshot['selected_processing_set']['configuration']
    if revision_change == 'missing':
        configuration.pop('parser_revisions')
    elif revision_change == 'old_facts':
        configuration['parser_revisions']['facts'] = 'numeric-tag-boundaries-v2'
    elif revision_change == 'old_reports':
        configuration['parser_revisions']['reports'] = configuration['parser_revisions']['reports'].replace('-v4', '-v3')
    original = deepcopy(snapshot)
    source_bytes = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    monkeypatch.setattr(acceptance, 'read_snapshot', lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(acceptance, 'audit_snapshot', lambda *args, **kwargs: {'status': 'passed'})
    result = acceptance.audit_corrected_set(None, 'fixture', UUID(int=3), tmp_path)
    errors = [error for error in result['errors'] if error['check'] == 'parser_revision_mismatch']
    if revision_change == 'current':
        assert result['status'] == 'passed', result
        assert not errors
    else:
        assert result['status'] == 'failed'
        assert len(errors) == 1
    assert snapshot == original
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == source_bytes
