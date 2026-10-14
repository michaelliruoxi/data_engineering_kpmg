"""V4 source-set parsing is pure and cannot trust retained payload claims."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest

from sec_pipeline import filing_parser_v4 as parser
from sec_pipeline.document_sets import verify_document_set


ROOT = Path(__file__).resolve().parents[1]
CLX = ROOT / 'data/raw/sec/0000021076-26-000034/source_document_set_manifest.json'


@pytest.fixture(scope='module')
def clorox():
    if not CLX.exists():
        pytest.skip('The approved original Clorox document set is not archived locally.')
    with patch('psycopg.connect', side_effect=AssertionError('Pure parser attempted database IO')), \
            patch('socket.create_connection', side_effect=AssertionError('Pure parser attempted network IO')):
        return parser.parse_filing_set(CLX, ROOT)


def test_original_documents_remain_separate_with_complete_union_facts(clorox):
    assert len(clorox.facts) == len(clorox.occurrences) == 2088
    assert len(clorox.documents) == 2
    assert [(item.filename, len(item.tables)) for item in clorox.documents] == [
        ('clx-20260630.htm', 0), ('clx-20260630_d2.htm', 3)]
    assert {item.source_document_id for item in clorox.documents} == {item.report.source_document_id for item in clorox.documents}
    assert len({item.report_placeholder_id for item in clorox.documents}) == 2
    assert all(item.report.full_text and item.chunks for item in clorox.documents)
    assert all(chunk.report_id == item.report_placeholder_id and chunk.section_id is None
               and chunk.text == item.report.full_text[chunk.offset_start:chunk.offset_end]
               for item in clorox.documents for chunk in item.chunks)
    assert clorox.checks == dict.fromkeys(parser.REQUIRED_CHECKS, True)
    assert clorox.coverage == {'facts': 'complete', 'tables': 'partial', 'reports': 'unknown', 'chunks': 'complete'}
    assert clorox.versions == {'facts': 'facts-v1', 'tables': 'tables-v4', 'reports': 'report-v1', 'chunks': 'paragraph-v1'}
    assert clorox.counts['unresolved_cells'] == 0
    assert len(clorox.issues) == clorox.counts['review_cells'] == 3
    assert clorox.semantic_checks == {'period_header_consistency': False}
    assert clorox.issues == tuple(issue for table in clorox.tables for row in table.structure['rows']
                                 for cell in row for issue in cell.get('quality_issues', []))


def test_retained_manifest_is_immutable_and_ids_have_explicit_dry_scope(clorox):
    before = clorox.manifest_json
    mutable = clorox.manifest
    mutable['files'][0]['sha256'] = '0' * 64
    assert clorox.manifest_json == before
    assert clorox.manifest != mutable
    filing, sources = parser.dry_run_ids(clorox.manifest)
    assert filing == clorox.filing_id
    assert sources[clorox.manifest['document_set']['target_xml_source_url']] == clorox.xml_document_id
    assert all(fact.filing_id == filing and fact.source_document_id == clorox.xml_document_id for fact in clorox.facts)
    for changed in ({}, {**sources, 'extra': UUID(int=99)}, {key: filing for key in sources}):
        with pytest.raises(parser.FilingSetParseError):
            parser._source_ids(clorox.manifest, filing, changed)


def test_fresh_original_parse_rejects_numeric_classification_tampering(clorox):
    documents = deepcopy(list(clorox.documents))
    table = documents[1].tables[0]
    cell = next(cell for row in table.structure['rows'] for cell in row if cell['classification'] == 'resolved')
    cell.update(classification='formatting', fact_references=[], column_period=None)
    table.structure['inventory'] = dict(Counter(cell['classification'] for row in table.structure['rows'] for cell in row))
    changed = replace(clorox, documents=tuple(documents))
    with pytest.raises(parser.FilingSetParseError, match='documents differs from fresh'):
        parser.validate_filing_set(changed, CLX, ROOT)


def test_fresh_original_parse_rejects_omitted_secondary_report(clorox):
    changed = replace(clorox, documents=clorox.documents[:1],
                      counts={**clorox.counts, 'reports': 1, 'members': 1})
    with pytest.raises(parser.FilingSetParseError, match='documents differs from fresh'):
        parser.validate_filing_set(changed, CLX, ROOT)


def test_reconstruction_compares_every_payload_not_only_success_flags(clorox):
    # The expensive real-source rebuild is exercised above; this isolates all
    # remaining DTO boundaries against the already source-verified result.
    expected = replace(clorox, checks={key: value for key, value in clorox.checks.items() if key != 'fresh_source_reconstruction'})
    values = []
    facts = list(clorox.facts)
    facts[0] = replace(facts[0], raw_value='invented')
    values.append(replace(clorox, facts=tuple(facts)))
    occurrences = deepcopy(list(clorox.occurrences))
    occurrences[0]['inline_document_id'] = str(UUID(int=999))
    values.append(replace(clorox, occurrences=tuple(occurrences)))
    documents = list(clorox.documents)
    chunks = list(documents[0].chunks)
    chunks[0] = replace(chunks[0], text='invented', offset_end=chunks[0].offset_start + 8)
    documents[0] = replace(documents[0], chunks=tuple(chunks))
    values.append(replace(clorox, documents=tuple(documents)))
    values.extend([replace(clorox, issues=()), replace(clorox, semantic_checks={'period_header_consistency': True}),
                   replace(clorox, coverage={**clorox.coverage, 'reports': 'complete'}),
                   replace(clorox, checks={**clorox.checks, 'source_bindings_exact': False}),
                   replace(clorox, manifest_sha256='0' * 64)])
    with patch.object(parser, '_build', return_value=expected) as rebuild:
        for changed in values:
            with pytest.raises(parser.FilingSetParseError):
                parser.validate_filing_set(changed, CLX, ROOT)
        assert rebuild.call_count == len(values)


def test_member_selection_rejects_complete_competitors_and_cross_document_splits(clorox):
    manifest = clorox.manifest
    records = {record['source_url']: record for record in manifest['files']}
    members = [records[url] for url in manifest['document_set']['member_source_urls']]
    complete = tuple(SimpleNamespace(statement_type=kind) for kind in parser.PRIMARY_KINDS)
    with patch.object(parser, 'select_statement_segments', return_value=complete):
        with pytest.raises(parser.FilingSetParseError, match='found 2'):
            parser._choose_member(members, ROOT, clorox.facts)
    with patch.object(parser, 'select_statement_segments', side_effect=[complete, ValueError('partial')]), \
            patch.object(parser, '_statement_signals', return_value=[{'statement_type': 'cash_flow', 'source_xpath': '/html/body/table'}]):
        with pytest.raises(parser.FilingSetParseError, match='competing or split'):
            parser._choose_member(members, ROOT, clorox.facts)


def test_member_selection_does_not_invent_a_union_statement(clorox):
    manifest = clorox.manifest
    records = {record['source_url']: record for record in manifest['files']}
    members = [records[url] for url in manifest['document_set']['member_source_urls']]
    with patch.object(parser, 'select_statement_segments', side_effect=ValueError('incomplete')):
        with pytest.raises(parser.FilingSetParseError, match='found 0'):
            parser._choose_member(members, ROOT, clorox.facts)


@pytest.mark.parametrize('accession,facts,tables,issues', [
    ('0001047862-26-000142', 2817, 4, 2), ('0001193125-26-380207', 683, 3, 1),
])
def test_archived_period_conflicts_remain_explicit_review(accession, facts, tables, issues):
    path = ROOT / 'data/raw/sec' / accession / 'source_document_set_manifest.json'
    if not path.exists():
        pytest.skip('The approved original source set is not archived locally.')
    result = parser.parse_filing_set(path, ROOT)
    assert len(result.facts) == len(result.occurrences) == facts
    assert len(result.tables) == tables
    assert len(result.issues) == issues
    assert all(result.checks.values())
    assert result.semantic_checks == {'period_header_consistency': False}
    assert result.counts['unresolved_cells'] == 0


def test_citi_retains_split_primary_tables_and_separate_supplemental_report():
    path = ROOT / 'data/raw/sec/0000831001-26-000045/source_document_set_manifest.json'
    if not path.exists():
        pytest.skip('The approved original Citi document set is not archived locally.')
    result = parser.parse_filing_set(path, ROOT)
    assert len(result.facts) == len(result.occurrences) == 9437
    assert [len(document.tables) for document in result.documents] == [5, 0]
    assert all(document.chunks for document in result.documents)
    assert sum(len(document.chunks) for document in result.documents) == 543
    assert {table.statement_type for table in result.tables} == parser.PRIMARY_KINDS
    assert all(result.checks.values())
    assert result.semantic_checks == {'period_header_consistency': True}
    assert result.counts['unresolved_cells'] == result.counts['review_cells'] == 0
    assert result.issues == ()
