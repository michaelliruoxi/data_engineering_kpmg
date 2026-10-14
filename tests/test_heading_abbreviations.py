"""Observed abbreviated continuation and unit markers remain source bounded."""

from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from lxml import etree, html
import pytest

from sec_pipeline import filing_adapter_v3 as adapter
from sec_pipeline.filing_sources import verify_filing
from sec_pipeline.processing import DryRunContext, SourceIds
from sec_pipeline.statement_headings import normalized_heading, recover_headings, statement_kind
from sec_pipeline.tables import TableExtractionError, select_statement_segments, select_statements
from tests.test_statement_segments import fixture as segments_fixture


ROOT = Path(__file__).resolve().parents[1]
DATA = '<table id="data"><tr><td><ix:nonFraction>42</ix:nonFraction></td></tr></table>'


def evidence(markup):
    tree = html.document_fromstring(markup).getroottree()
    before = etree.tostring(tree)
    result = recover_headings(tree.xpath('//table[@id="data"]')[0])
    assert etree.tostring(tree) == before
    return result


@pytest.mark.parametrize('marker', ['(cont.)', '(CONT.)', '( cont. )', '(Continued)', 'Continued'])
def test_only_observed_terminal_marker_preserves_heading_identity(marker):
    title = f'Consolidated Statements of Cash Flows {marker}'
    [result] = evidence(f'<p>{title}</p>{DATA}')
    assert result.text == title and result.signature == 'consolidated statements of cash flows'
    assert result.kind == 'cash_flow' and result.continued is True and result.consolidated is True
    assert result.source_xpath == '/html/body/p'


@pytest.mark.parametrize('title', [
    'See Consolidated Statements of Cash Flows (cont.)',
    'Notes to Consolidated Statements of Cash Flows (cont.)',
    'Parent Company Statements of Cash Flows (cont.)',
    'Supplemental Consolidating Statements of Cash Flows (cont.)',
    'Selected Financial Statements of Cash Flows (cont.)',
    'Consolidated Statements of Cash Flows (cont. from note 1)',
    'Consolidated Statements of Cash Flows (cont.) for the parent company',
    'Consolidated Statements of Cash Flows cont.',
    'Consolidated Statements of Cash Flows (continuing)',
    'Consolidated Statements of Cash Flows (contract.)',
])
def test_abbreviation_does_not_turn_prose_or_other_qualifiers_into_a_title(title):
    assert statement_kind(title) is None
    assert evidence(f'<p>{title}</p>{DATA}') == ()


@pytest.mark.parametrize('barrier', [
    '<p>Note 3. Cash disclosures</p>',
    '<p>Parent company only</p>',
    '<p>These amounts continue the reconciliation.</p>',
    '<table><tr><td><ix:nonFraction>10</ix:nonFraction></td></tr></table>',
])
def test_continuation_cannot_cross_a_note_prose_or_financial_table(barrier):
    assert evidence('<p>Consolidated Statements of Cash Flows (cont.)</p>' + barrier + DATA) == ()


def test_exact_thousands_marker_permits_wrapper_ascent_without_rewriting_it():
    [result] = evidence('<p>Consolidated Statements of Cash Flows</p><div><span>($000)</span>' + DATA + '</div>')
    assert result.kind == 'cash_flow' and result.continued is False
    assert result.text == 'Consolidated Statements of Cash Flows'
    assert result.source_xpath == '/html/body/p'


@pytest.mark.parametrize('marker', ['($000 total)', '($000 per subsidiary)', '($0000)', '($100)', '($000; note 1)', '($000, parent company only)'])
def test_unit_marker_does_not_discard_substantive_text(marker):
    assert evidence('<p>Consolidated Statements of Cash Flows</p><div><span>' + marker + '</span>' + DATA + '</div>') == ()


def test_abbreviation_is_fallback_only_and_requires_matching_scope_periods_and_core():
    tree, facts = segments_fixture()
    tables = tree.xpath('//table')
    cash = [table for table in tables if table.get('id', '').startswith('cash')]
    assert len(cash) == 2
    title = cash[1].getprevious()
    title.text = 'Consolidated Statements of Cash Flows (cont.)'
    cash[0].getprevious().text = 'Consolidated Statements of Cash Flows'
    with pytest.raises(TableExtractionError):
        select_statements(tree)
    assert len(select_statement_segments(tree, facts)) == 4
    for field, value in [('entity', {'identifier': '999', 'scheme': 'http://www.sec.gov/CIK'}),
                         ('unit', {'numerator': ['{http://www.xbrl.org/2003/iso4217}EUR'], 'denominator': []}),
                         ('period', {'startDate': '2024-01-01', 'endDate': '2024-06-30'})]:
        changed = deepcopy(facts)
        identifier = next(node.get('id') for node in cash[1].iter() if str(node.tag).lower().endswith('nonfraction'))
        changed[identifier][field] = value
        with pytest.raises(TableExtractionError):
            select_statement_segments(tree, changed)


def test_original_fiserv_source_passes_all_stages_with_both_physical_cash_segments():
    path = ROOT / 'data/raw/sec/0000798354-26-000031/source_manifest.json'
    if not path.exists():
        pytest.skip('Optional original Fiserv archive is unavailable.')
    manifest = verify_filing(path, ROOT)
    outputs = {}
    context = DryRunContext(manifest=manifest, data_root=ROOT, source_ids=SourceIds(UUID(int=1), UUID(int=2), UUID(int=3)),
                            versions=adapter.VERSIONS, outputs=outputs, report_id=UUID(int=4))
    plan = adapter.get_dry_run_plan()
    with patch('psycopg.connect', side_effect=AssertionError('No DB access')), \
            patch('socket.create_connection', side_effect=AssertionError('No network access')):
        for stage in ('facts', 'report', 'chunks', 'tables'):
            outputs[stage] = getattr(plan.stages, stage)(context)
        validation = plan.stages.validate(context)
    assert all(value is True for value in validation.checks.values())
    assert len(outputs['facts'].items) == 1426 and len(outputs['tables'].items) == 4
    assert len(outputs['chunks'].items) == 188 and outputs['tables'].unresolved_cells == 0
    cash = [table for table in outputs['tables'].items if table.statement_type == 'cash_flow']
    assert [table.table_key for table in cash] == ['cash_flow.segment_1', 'cash_flow.segment_2']
    assert [table.structure['source_xpath'] for table in cash] == ['/html/body/div[82]/table', '/html/body/div[85]/table']
    group = cash[0].structure['statement_group']
    assert group['column_periods'] == [{'endDate': '2026-06-30', 'startDate': '2026-01-01'},
                                      {'endDate': '2025-06-30', 'startDate': '2025-01-01'}]
    assert group['scope']['entity']['identifier'] == '0000798354' and group['scope']['dimensions'] == []
    assert all(set(item['groups']) == {'0', '1', '2', '3'} for item in group['combined_concept_evidence'])
    assert any(cell['display_text'] == 'Consolidated Statements of Cash Flows (cont.)'
               for row in cash[1].structure['rows'] for cell in row)


def test_original_coherent_unit_marker_recovers_only_its_own_heading():
    path = ROOT / 'data/raw/sec/0000820318-26-000020/source_manifest.json'
    if not path.exists():
        pytest.skip('Optional original Coherent archive is unavailable.')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    source = ROOT / next(item['local_path'] for item in manifest['files'] if item['role'] == 'main_inline_xbrl_filing')
    tree = html.parse(str(source), parser=html.HTMLParser(encoding='utf-8', no_network=True))
    [result] = recover_headings(tree.xpath('/html/body/div[820]/table')[0])
    assert result.source_xpath == '/html/body/div[819]' and result.kind == 'cash_flow'
    assert normalized_heading(result.text) == 'consolidated statements of cash flows'
    assert recover_headings(tree.xpath('/html/body/div[823]/table')[0]) == ()
