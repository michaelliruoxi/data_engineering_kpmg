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


def _flagged_set(layouts, monkeypatch):
    """A stored tables-v8 table of the synthetic cash flow statement, and its original sources.

    With ``layouts`` its column is headed "First Quarter 2026", which only the
    corroborated layouts read, and the table is built and marked as the parser's
    second reading builds and marks it.
    """
    from sec_pipeline import fact_extraction, tables, source_cells_v3
    from sec_pipeline.extraction_profiles import FLAGGED_PROFILE
    from sec_pipeline.statement_continuations import with_omitted_continuations
    from sec_pipeline.tagging_defects import flag_table_cells
    from tests.test_source_cells_v4 import fixture
    v3, facts, source = fixture(heading='First Quarter 2026' if layouts else 'Three months ended March 31, 2026')
    instance = b'<original-instance/>'
    v3 = replace(v3, structure=v3.structure | {'xml_sha256': hashlib.sha256(instance).hexdigest()})
    reviewed = transform_v4([v3], source, facts, tagging_defects=True, corroborated_layouts=layouts)
    flagged, records = flag_table_cells(reviewed, source, facts, version='tables-v8', corroborated_layouts=layouts)
    assert [record['code'] for record in records] == ['instant_context_under_duration_header']
    if layouts:
        flagged = [replace(table, structure=table.structure | {'corroborated_layouts': True}) for table in flagged]
    stored = with_omitted_continuations(corrected_tables(flagged, profile=FLAGGED_PROFILE), source)
    readings = []
    monkeypatch.setattr(tables, 'extract_tables',
                        lambda *a, **kw: readings.append(kw['corroborated_layouts']) or [v3])
    monkeypatch.setattr(source_cells_v3, 'transform_tables', lambda *a, **kw: [v3])
    monkeypatch.setattr(fact_extraction, 'extract_facts', lambda *a, **kw: SimpleNamespace(
        require_complete=lambda: SimpleNamespace(facts=facts)))
    return ({'tables': [canonical(asdict(table)) for table in stored]},
            {str(UUID(int=2)): source, str(UUID(int=3)): instance}, readings)


@pytest.mark.parametrize('layouts', [False, True])
def test_a_v8_set_is_rebuilt_with_the_reading_its_tables_name(monkeypatch, layouts):
    snapshot, sources, readings = _flagged_set(layouts, monkeypatch)
    assert ('corroborated_layouts' in snapshot['tables'][0]['structure']) is layouts
    result = reconstruct_original_tables(snapshot, sources)
    assert result['status'] == 'passed', result
    assert readings == [layouts]


def test_the_established_reading_does_not_rebuild_a_table_of_the_second_reading(monkeypatch):
    snapshot, sources, readings = _flagged_set(True, monkeypatch)
    del snapshot['tables'][0]['structure']['corroborated_layouts']
    result = reconstruct_original_tables(snapshot, sources)
    assert result['status'] == 'failed' and readings == [False]
    [difference] = result['groups'][0]['differences']
    assert difference['cell_key'] == 'r004c001' and 'classification' in difference['fields']
    assert difference['stored']['classification'] == 'source_defect'
    assert difference['reconstructed']['classification'] == 'unresolved'


@pytest.mark.parametrize('claim', ['not_a_boolean', 'tables_disagree', 'another_version'])
def test_a_layout_reading_must_be_named_alike_by_every_table_and_only_by_v8(monkeypatch, claim):
    snapshot, sources, readings = _flagged_set(True, monkeypatch)
    table = snapshot['tables'][0]
    if claim == 'not_a_boolean':
        table['structure']['corroborated_layouts'] = 'yes'
    elif claim == 'tables_disagree':
        other = deepcopy(table)
        other['table_key'] = other['structure']['table_key'] = 'cash_flow.other'
        del other['structure']['corroborated_layouts']
        snapshot['tables'].append(other)
    else:
        table['extraction_version'] = 'tables-v7'
    result = reconstruct_original_tables(snapshot, sources)
    assert result['status'] == 'unverified' and readings == []
    assert result['groups'][0]['reason'] == 'unsupported_layout_reading'
