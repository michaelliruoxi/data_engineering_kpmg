"""Original-source replay follows the complete versioned transform chain."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from sec_pipeline.extraction_profiles import CORRECTED_PROFILE, REVIEWED_PROFILE, corrected_tables
from sec_pipeline.fact_extraction import extract_facts
from sec_pipeline.source_cells_v3 import transform_tables as transform_v3
from sec_pipeline.source_cells_v4 import transform_tables as transform_v4
from sec_pipeline.table_sections import canonical
from sec_pipeline.table_source_reconstruction import reconstruct_original_tables
from sec_pipeline.tables import extract_tables

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def original_tables():
    manifest = json.loads((ROOT / 'source_manifest.json').read_text())
    html = next(r for r in manifest['files'] if r['role'] == 'main_inline_xbrl_filing')
    xml = next(r for r in manifest['files'] if r['role'] == 'extracted_xbrl_instance')
    html_path, xml_path = ROOT / html['local_path'], ROOT / xml['local_path']
    source, instance = html_path.read_bytes(), xml_path.read_bytes()
    ids = {'filing_id': UUID(int=1), 'html_document_id': UUID(int=2), 'xml_document_id': UUID(int=3)}
    facts = extract_facts(xml_path, filing_id=ids['filing_id'], source_document_id=ids['xml_document_id']).require_complete().facts
    v2 = extract_tables(html_path, xml_path, **ids, facts_version='facts-v1', tables_version='tables-v2')
    v3 = transform_v3(v2, source, facts)
    v4 = transform_v4(v3, source, facts)
    return {'tables-v3': v3, 'tables-v4': v4,
            'tables-v5': corrected_tables(v4, profile=CORRECTED_PROFILE),
            'tables-v6': corrected_tables(v4, profile=REVIEWED_PROFILE)}, {str(UUID(int=2)): source, str(UUID(int=3)): instance}


@pytest.mark.parametrize('version', ['tables-v3', 'tables-v4', 'tables-v5', 'tables-v6'])
def test_real_original_source_supports_complete_versioned_chain(original_tables, version):
    versions, sources = original_tables
    snapshot = {'tables': [canonical(asdict(t)) for t in versions[version]]}
    before = deepcopy(snapshot)
    result = reconstruct_original_tables(snapshot, sources)
    assert result['status'] == 'passed', result
    assert result['groups'][0]['cells_compared'] > 2000
    assert snapshot == before


@pytest.mark.parametrize('field', ['quality_issue_keys', 'quality_issues', 'source_fact_references'])
def test_versioned_review_evidence_is_compared_against_reconstruction(monkeypatch, field):
    from sec_pipeline import fact_extraction, tables, source_cells_v3
    from tests.test_source_cells_v4 import fixture
    v3, facts, source = fixture()
    instance = b'<original-instance/>'
    v3 = replace(v3, structure=v3.structure | {'xml_sha256': hashlib.sha256(instance).hexdigest()})
    v4 = transform_v4([v3], source, facts)
    expected = corrected_tables(v4, profile=REVIEWED_PROFILE)
    snapshot = {'tables': [canonical(asdict(t)) for t in expected]}
    monkeypatch.setattr(tables, 'extract_tables', lambda *a, **kw: [v3])
    monkeypatch.setattr(source_cells_v3, 'transform_tables', lambda *a, **kw: [v3])
    monkeypatch.setattr(fact_extraction, 'extract_facts', lambda *a, **kw: SimpleNamespace(
        require_complete=lambda: SimpleNamespace(facts=facts)))
    sources = {str(UUID(int=2)): source, str(UUID(int=3)): instance}
    assert reconstruct_original_tables(snapshot, sources)['status'] == 'passed'
    snapshot['tables'][0]['structure']['rows'][4][1][field] = []
    result = reconstruct_original_tables(snapshot, sources)
    assert result['status'] == 'failed'
    assert any(field in diff.get('fields', []) for diff in result['groups'][0]['differences'])
