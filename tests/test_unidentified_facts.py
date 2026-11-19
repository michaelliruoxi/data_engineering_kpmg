"""Facts a filing agent left without an id are paired with their inline tags; nothing else is relaxed."""

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from uuid import UUID

import pytest

from sec_pipeline.database import FactInput
from sec_pipeline.fact_extraction import check_inline_correspondence, extract_facts, occurrence_key_is_source_identity
from sec_pipeline.source_cells_v3 import SourceBindings, SourceCellError
from sec_pipeline.tables import TableExtractionError, without_unidentified_cover_facts, xml_evidence


ROOT = Path(__file__).resolve().parents[1]
DEI = 'http://xbrl.sec.gov/dei/2023'
GAAP = 'http://fasb.org/us-gaap/2023'
REGISTRY = 'http://www.xbrl.org/inlineXBRL/transformation/2022-02-16'
SHARES = {'numerator': ['{http://www.xbrl.org/2003/instance}shares'], 'denominator': []}
CORNING_10Q = ROOT / 'data' / 'raw' / 'sec' / '0001437749-23-011533'


def document(body):
    return (f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" '
            f'xmlns:dei="{DEI}" xmlns:us-gaap="{GAAP}" xmlns:ixt="{REGISTRY}"><body>{body}</body></html>')


def number_tag(value, *, identifier=None, concept='dei:EntityCommonStockSharesOutstanding', context='asof'):
    marker = f'id="{identifier}" ' if identifier else ''
    return (f'<p><ix:nonFraction {marker}name="{concept}" contextRef="{context}" unitRef="shares" decimals="INF" '
            f'format="ixt:num-dot-decimal">{value}</ix:nonFraction></p>')


def text_tag(value, *, identifier=None):
    marker = f'id="{identifier}" ' if identifier else ''
    return f'<p><ix:nonNumeric {marker}name="dei:DocumentType" contextRef="fy">{value}</ix:nonNumeric></p>'


def number_fact(value, order, *, identifier=None, namespace=DEI, name='EntityCommonStockSharesOutstanding', context='asof'):
    path = f'/*/*[{order}]'
    reference = {'xpath': path, 'document_order': order, 'context_ref': context, 'unit_ref': 'shares'}
    if identifier:
        reference = {'element_id': identifier, **reference}
    return FactInput(filing_id=UUID(int=1), source_document_id=UUID(int=2), extraction_version='facts-v1',
                     occurrence_key=identifier or f'xpath:{path}', concept_namespace=namespace, concept_name=name,
                     period_kind='instant', instant_date=date(2024, 2, 23), numeric_value=Decimal(value),
                     raw_value=value, unit=SHARES, source_reference=reference)


def text_fact(value, order, *, identifier=None):
    path = f'/*/*[{order}]'
    reference = {'xpath': path, 'document_order': order, 'context_ref': 'fy'}
    if identifier:
        reference = {'element_id': identifier, **reference}
    return FactInput(filing_id=UUID(int=1), source_document_id=UUID(int=2), extraction_version='facts-v1',
                     occurrence_key=identifier or f'xpath:{path}', concept_namespace=DEI, concept_name='DocumentType',
                     period_kind='duration', period_start=date(2023, 1, 1), period_end=date(2023, 12, 31),
                     raw_value=value, source_reference=reference)


def compare(tmp_path, body, facts, **options):
    source = tmp_path / 'filing.htm'
    source.write_text(document(body), encoding='utf-8')
    return check_inline_correspondence(source, facts, **options)


def test_facts_without_an_id_are_paired_by_concept_context_and_unit(tmp_path):
    body = text_tag('10-K') + number_tag('206,376,661') + number_tag('1,000', identifier='held', context='prior')
    facts = [text_fact('10-K', 3), number_fact('206376661', 4), number_fact('1000', 5, identifier='held', context='prior')]
    result = compare(tmp_path, body, facts, pair_unidentified=True)
    assert result['mismatches'] == [] and result['inline_ids_without_fact'] == []
    assert result['matched'] == len(facts) == result['inline_elements'] == 3
    assert result['numeric_values_equal'] == 2


def test_without_the_option_a_fact_without_an_id_stays_unmatched(tmp_path):
    body = text_tag('10-K') + number_tag('206,376,661')
    facts = [text_fact('10-K', 3), number_fact('206376661', 4)]
    result = compare(tmp_path, body, facts)
    assert [item['problem'] for item in result['mismatches']] == ['0 inline elements share this id'] * 2
    assert result['matched'] == 0


@pytest.mark.parametrize('tag, problem', [
    (number_tag('206,376,660'), 'value: HTML shows 206376660, XML has 206376661'),
    (number_tag('206,376,661', context='prior'), '1 facts and 0 inline elements without an id share this concept, context and unit'),
    (number_tag('206,376,661', concept='dei:EntityPublicFloat'),
     '1 facts and 0 inline elements without an id share this concept, context and unit'),
    (number_tag('206,376,661', identifier='shares'), '1 facts and 0 inline elements without an id share this concept, context and unit'),
])
def test_a_paired_fact_is_compared_like_any_other(tmp_path, tag, problem):
    result = compare(tmp_path, tag, [number_fact('206376661', 3)], pair_unidentified=True)
    assert [item['problem'] for item in result['mismatches']] == [problem]
    assert result['matched'] == 0


def test_a_tag_with_an_id_is_not_given_to_a_fact_without_one(tmp_path):
    result = compare(tmp_path, number_tag('206,376,661', identifier='shares'), [number_fact('206376661', 3)],
                     pair_unidentified=True)
    assert result['inline_ids_without_fact'] == ['shares'] and result['matched'] == 0


def test_repeated_facts_pair_in_document_order_and_each_pair_is_compared(tmp_path):
    body = number_tag('5') + number_tag('7') + text_tag('10-K') + text_tag('10-K')
    facts = [number_fact('5', 3), number_fact('7', 4), text_fact('10-K', 5), text_fact('10-K', 6)]
    result = compare(tmp_path, body, list(reversed(facts)), pair_unidentified=True)
    assert result['mismatches'] == [] and result['matched'] == 4 == result['inline_elements']
    crossed = compare(tmp_path, body, [number_fact('7', 3), number_fact('5', 4), *facts[2:]], pair_unidentified=True)
    assert sorted(item['problem'] for item in crossed['mismatches']) == [
        'value: HTML shows 5, XML has 7', 'value: HTML shows 7, XML has 5']


@pytest.mark.parametrize('tags, values', [(1, ('5', '5')), (2, ('5',)), (0, ('5',))])
def test_unequal_numbers_of_facts_and_tags_stay_unmatched(tmp_path, tags, values):
    facts = [number_fact(value, order) for order, value in enumerate(values, 3)]
    result = compare(tmp_path, number_tag('5') * tags or '<p/>', facts, pair_unidentified=True)
    assert [item['problem'] for item in result['mismatches']] == [
        f'{len(values)} facts and {tags} inline elements without an id share this concept, context and unit'] * len(values)
    assert not result['matched'] == len(facts) == result['inline_elements']


def test_the_positional_key_is_a_source_identity_only_where_asked():
    named, unnamed = number_fact('5', 3, identifier='shares'), number_fact('5', 4)
    assert occurrence_key_is_source_identity(named) and occurrence_key_is_source_identity(named, unidentified=True)
    assert not occurrence_key_is_source_identity(unnamed)
    assert occurrence_key_is_source_identity(unnamed, unidentified=True)
    moved = number_fact('5', 4)
    moved.source_reference['xpath'] = '/*/*[5]'
    assert not occurrence_key_is_source_identity(moved, unidentified=True)
    renamed = number_fact('5', 3, identifier='shares')
    renamed.source_reference['element_id'] = 'other'
    assert not occurrence_key_is_source_identity(renamed, unidentified=True)


INSTANCE = f'''<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:dei="{DEI}" xmlns:us-gaap="{GAAP}"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
 <context id="fy"><entity><identifier scheme="http://www.sec.gov/CIK">0000000001</identifier></entity>
  <period><startDate>2023-01-01</startDate><endDate>2023-12-31</endDate></period></context>
 <unit id="usd"><measure>iso4217:USD</measure></unit>
 <dei:DocumentType contextRef="fy">10-K</dei:DocumentType>
 <us-gaap:Revenues {{marker}}contextRef="fy" unitRef="usd" decimals="0">100</us-gaap:Revenues>
</xbrl>'''


def test_statement_evidence_leaves_out_only_cover_facts_without_an_id(tmp_path):
    path = tmp_path / 'instance.xml'
    path.write_text(INSTANCE.format(marker='id="f1" '), encoding='utf-8')
    with pytest.raises(TableExtractionError, match='Missing or duplicate XML fact ID'):
        xml_evidence(path)
    assert list(xml_evidence(path, unidentified_cover_facts=True)) == ['f1']
    path.write_text(INSTANCE.format(marker=''), encoding='utf-8')
    with pytest.raises(TableExtractionError, match='Missing or duplicate XML fact ID'):
        xml_evidence(path, unidentified_cover_facts=True)


def test_a_statement_fact_without_an_id_is_kept_for_the_selection_to_refuse():
    cover, named = text_fact('10-K', 3), number_fact('5', 4, identifier='shares')
    statement = number_fact('5', 5, namespace=GAAP, name='Revenues')
    assert without_unidentified_cover_facts([cover, named, statement]) == [named, statement]


def test_bindings_leave_out_a_fact_without_an_id_only_where_asked():
    source = document(text_tag('10-K') + number_tag('5', identifier='shares')).encode()
    facts = [text_fact('10-K', 3), number_fact('5', 4, identifier='shares')]
    with pytest.raises(SourceCellError, match='unique original element IDs'):
        SourceBindings(source, facts)
    assert list(SourceBindings(source, facts, unidentified=True).facts) == ['shares']
    with pytest.raises(SourceCellError, match='unique original element IDs'):
        SourceBindings(source, [facts[1], facts[1]], unidentified=True)


@pytest.mark.skipif(not (CORNING_10Q / 'source_manifest.json').is_file(), reason='the archived filing is not on this machine')
def test_archived_filing_whose_cover_tags_have_no_id():
    manifest = json.loads((CORNING_10Q / 'source_manifest.json').read_text(encoding='utf-8'))
    files = {record['role']: ROOT / record['local_path'] for record in manifest['files']}
    facts = extract_facts(files['extracted_xbrl_instance'], filing_id=UUID(int=1), source_document_id=UUID(int=2)).facts
    unnamed = [fact for fact in facts if 'element_id' not in fact.source_reference]
    assert (len(facts), len(unnamed)) == (714, 27)
    assert all(fact.concept_namespace.startswith('http://xbrl.sec.gov/') for fact in unnamed)
    assert all(occurrence_key_is_source_identity(fact, unidentified=True) for fact in facts)
    strict = check_inline_correspondence(files['main_inline_xbrl_filing'], facts)
    assert len(strict['mismatches']) == 27 and strict['matched'] == 687
    paired = check_inline_correspondence(files['main_inline_xbrl_filing'], facts, pair_unidentified=True)
    assert paired['mismatches'] == [] and paired['inline_ids_without_fact'] == []
    assert paired['matched'] == 714 == paired['inline_elements']
    assert len(xml_evidence(files['extracted_xbrl_instance'], unidentified_cover_facts=True)) == 687
    assert len(without_unidentified_cover_facts(facts)) == 687
