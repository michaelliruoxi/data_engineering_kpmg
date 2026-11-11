"""Report visible digits outside a numeric tag without changing the XML fact."""

from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
import hashlib
import os
from pathlib import Path
from uuid import UUID

from lxml import etree
import pytest

from sec_pipeline.database import FactInput
from sec_pipeline.fact_extraction import check_inline_correspondence, extract_facts, _inline_number
from sec_pipeline.fact_coverage import assess_filing
from sec_pipeline.inline_value_transforms import numeric_value


ROOT = Path(__file__).resolve().parents[1]
DEI = 'http://xbrl.sec.gov/dei/2023'
REGISTRY = 'http://www.xbrl.org/inlineXBRL/transformation/2022-02-16'
TAG = ('<ix:nonFraction id="shares" name="dei:EntityCommonStockSharesOutstanding" '
       'contextRef="asof" unitRef="shares" decimals="INF" format="ixt:num-dot-decimal">'
       '206,376,66</ix:nonFraction>')


def document(body):
    return (f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" '
            f'xmlns:dei="{DEI}" xmlns:ixt="{REGISTRY}"><body>{body}</body></html>')


@pytest.fixture
def fact():
    return FactInput(filing_id=UUID(int=1), source_document_id=UUID(int=2), extraction_version='facts-v1',
                     occurrence_key='shares', concept_namespace=DEI, concept_name='EntityCommonStockSharesOutstanding',
                     period_kind='instant', instant_date=date(2024, 2, 23), numeric_value=Decimal('20637666'),
                     raw_value='20637666', unit={'numerator': ['{http://www.xbrl.org/2003/instance}shares'], 'denominator': []},
                     source_reference={'element_id': 'shares', 'context_ref': 'asof', 'unit_ref': 'shares'})


@pytest.mark.parametrize('body', [
    f'<p><span>{TAG}</span><span>1 shares outstanding.</span></p>',
    f'<p>{TAG}1 shares outstanding.</p>',
    f'<p><b><span>{TAG}</span></b><span><i>1 shares outstanding.</i></span></p>',
])
def test_contiguous_untagged_digit_is_source_evidence_not_a_corrected_fact(tmp_path, fact, body):
    source = tmp_path / 'filing.htm'
    source.write_text(document(body))
    original, original_fact = source.read_bytes(), asdict(fact)
    result = check_inline_correspondence(source, [fact])
    assert result['mismatches'] == []
    assert result['numeric_values_equal'] == 1
    issue, = result['numeric_tag_boundary_issues']
    assert (issue['tagged_display'], issue['visible_display'], issue['xml_value'], issue['untagged_digits']) == (
        '206,376,66', '206,376,661', '20637666', '1')
    tree = etree.fromstring(original).getroottree()
    inline, = tree.xpath(issue['inline_xpath'])
    adjacent, = tree.xpath(issue['adjacent_xpath'])
    assert ''.join(inline.itertext()) == issue['tagged_display']
    assert getattr(adjacent, issue['adjacent_text_slot']).startswith(issue['untagged_digits'])
    assert asdict(fact) == original_fact and source.read_bytes() == original


@pytest.mark.parametrize('following', [
    '<sup>1</sup>', '<sub>1</sub>', '<a href="#note">1</a>',
    '<span style="vertical-align:super">1</span>', '<span hidden="hidden">1</span>',
    '<span style="display:none">1</span>', '<span style="margin-left:3pt">1</span>',
    '<span> 1 shares</span>', '<span>1st entry</span>',
    '<div>1 shares</div>', '<br/>1 shares',
    '<ix:nonFraction id="other" name="dei:EntityCommonStockSharesOutstanding" contextRef="asof" '
    'unitRef="shares" decimals="INF">1</ix:nonFraction>',
])
def test_footnotes_separate_values_and_separate_blocks_are_not_joined(tmp_path, fact, following):
    source = tmp_path / 'filing.htm'
    source.write_text(document(f'<p><span>{TAG}</span>{following}</p>'))
    assert check_inline_correspondence(source, [fact])['numeric_tag_boundary_issues'] == []


def test_hidden_and_footnote_ancestors_do_not_supply_visible_number_evidence(tmp_path, fact):
    for wrapper in ('<div hidden="hidden">{}</div>', '<div style="display:none">{}</div>', '<sup>{}</sup>',
                    '<div style="display:/**/none">{}</div>',
                    '<div style="visibility:/* hidden ancestor\n */hidden">{}</div>'):
        source = tmp_path / 'filing.htm'
        source.write_text(document(wrapper.format(f'<span>{TAG}1 shares</span>')))
        assert check_inline_correspondence(source, [fact])['numeric_tag_boundary_issues'] == []


@pytest.mark.parametrize('body', [f'<ix:hidden>{TAG}1</ix:hidden>', f'<ix:hidden>9{TAG}</ix:hidden>'])
def test_inline_hidden_ancestor_does_not_supply_visible_adjacency(tmp_path, fact, body):
    source = tmp_path / 'filing.htm'
    source.write_text(document(body))
    assert check_inline_correspondence(source, [fact])['numeric_tag_boundary_issues'] == []


@pytest.mark.parametrize('style', [
    'display:block', 'margin-right:3em', 'padding-right:2px', 'position:absolute',
    'vertical-align:super', 'display:/**/none',
])
def test_numeric_tag_layout_does_not_establish_contiguous_digits(tmp_path, fact, style):
    styled = TAG.replace('<ix:nonFraction ', f'<ix:nonFraction style="{style}" ')
    source = tmp_path / 'filing.htm'
    source.write_text(document(f'<p>{styled}1 shares</p>'))
    result = check_inline_correspondence(source, [fact])
    assert result['numeric_values_equal'] == 1
    assert result['mismatches'] == []
    assert result['numeric_tag_boundary_issues'] == []


@pytest.mark.parametrize('spacing', [' ', '\n', '\xa0'])
def test_whitespace_inside_numeric_tag_separates_following_token(tmp_path, fact, spacing):
    spaced = TAG.replace('206,376,66', '206,376,66' + spacing)
    source = tmp_path / 'filing.htm'
    source.write_text(document(f'<p><span>{spaced}</span><span>1 shares</span></p>'))
    assert check_inline_correspondence(source, [fact])['numeric_tag_boundary_issues'] == []


def test_correspondence_and_source_set_transforms_share_modern_lexical_rules():
    for displayed in ('206,376,66', '1.2,3', '1.', '1\u2003234'):
        element = etree.fromstring(document(f'<p>{TAG.replace("206,376,66", displayed)}</p>')).xpath(
            '//*[@id="shares"]')[0]
        expected = Decimal('20637666') if displayed == '206,376,66' else None
        assert _inline_number(element) == expected


def test_original_ntap_xml_and_visible_sentence_remain_distinct():
    directory = Path(os.environ.get('SEC_NTAP_REVIEW_ROOT', ROOT / 'data/ntap-review/0000950170-24-023115'))
    source, xml = directory / 'ntap-20240126.htm', directory / 'ntap-20240126_htm.xml'
    if not source.is_file() or not xml.is_file():
        pytest.skip('Set SEC_NTAP_REVIEW_ROOT to the archived original NTAP HTML/XML directory.')
    assert hashlib.sha256(source.read_bytes()).hexdigest() == '4ff6e129b394f279b925152039a223722df76b1d9d6ce26402e5dbc1a3630548'
    assert hashlib.sha256(xml.read_bytes()).hexdigest() == '4b562dfcc3d66fc7d5580411a81838d26d8cfe58bbc2c43c5b5d0d40779f5318'
    facts = extract_facts(xml, filing_id=UUID(int=1), source_document_id=UUID(int=2)).require_complete().facts
    fact = next(f for f in facts if f.concept_name == 'EntityCommonStockSharesOutstanding')
    result = check_inline_correspondence(source, facts)
    issue, = result['numeric_tag_boundary_issues']
    assert issue['visible_display'] == '206,376,661'
    assert issue['tagged_display'] == '206,376,66'
    assert fact.raw_value == issue['xml_value'] == '20637666'
    assert fact.numeric_value == numeric_value(issue['tagged_display'], '{' + REGISTRY + '}num-dot-decimal')
    assert fact.instant_date == date(2024, 2, 23)
    assert result['mismatches'] == [] and result['numeric_values_equal'] == 1026
    manifest = {'cik': '0001002047', 'form': '10-Q', 'accession_number': '0000950170-24-023115',
                'filing_date': '2024-02-29', 'reporting_period_end': '2024-01-26', 'files': [
                    {'role': 'main_inline_xbrl_filing', 'local_path': source.name,
                     'sha256': hashlib.sha256(source.read_bytes()).hexdigest()},
                    {'role': 'extracted_xbrl_instance', 'local_path': xml.name,
                     'sha256': hashlib.sha256(xml.read_bytes()).hexdigest()}]}
    assessment = assess_filing(manifest, directory, 'NTAP')
    assert assessment['status'] == 'review' and assessment['problems'] == []
    assert any("visible number is '206,376,661'" in reason and 'unchanged XML value is 20637666' in reason
               for reason in assessment['review'])
    assert assessment['inline_correspondence']['numeric_tag_boundary_issues'] == [issue]


@pytest.mark.parametrize('suffix', ['1.23', '1,234', '1<b>2</b> shares'])
def test_contiguous_plain_numeric_suffix_requires_review(tmp_path, fact, suffix):
    source = tmp_path / 'filing.htm'
    source.write_text(document(f'<p><span>{TAG}</span><span>{suffix}</span></p>'))
    assert check_inline_correspondence(source, [fact])['numeric_tag_boundary_issues']


@pytest.mark.parametrize('suffix, expected', [('.7 billion', '16.7'), (',700 million', '16,700'), ('7 billion', '167')])
def test_regular_numeric_tag_fraction_is_reviewed_without_altering_fact(tmp_path, fact, suffix, expected):
    source = tmp_path / 'filing.htm'
    tag = TAG.replace('206,376,66', '16')
    source.write_text(document(f'<p>{tag}{suffix}</p>'))
    fact = replace(fact, numeric_value=Decimal('16'), raw_value='16')
    result = check_inline_correspondence(source, [fact])
    assert result['numeric_values_equal'] == 1
    issue, = result['numeric_tag_boundary_issues']
    assert issue['visible_display'] == expected
    assert fact.numeric_value == Decimal('16')


@pytest.mark.parametrize('body', [
    '<p>9{tag}</p>',
    '<p><span>9</span><span>{tag}</span></p>',
    '<p><b><span><i>9</i></span></b><span>{tag}</span></p>',
    '<p><b>Leading text <i>prefix</i>9</b><span>{tag}</span></p>',
    '<p><sup>1</sup>9{tag}</p>',
])
def test_contiguous_numeric_prefix_requires_review_without_changing_fact(tmp_path, fact, body):
    source = tmp_path / 'filing.htm'
    tag = TAG.replace('206,376,66', '1,234')
    source.write_text(document(body.format(tag=tag)))
    fact = replace(fact, numeric_value=Decimal('1234'), raw_value='1234')
    original, original_fact = source.read_bytes(), asdict(fact)
    result = check_inline_correspondence(source, [fact])
    assert result['mismatches'] == []
    assert result['numeric_values_equal'] == 1
    issue, = result['numeric_tag_boundary_issues']
    assert (issue['tagged_display'], issue['visible_display'], issue['xml_value'], issue['untagged_digits']) == (
        '1,234', '91,234', '1234', '9')
    tree = etree.fromstring(original).getroottree()
    adjacent, = tree.xpath(issue['adjacent_xpath'])
    assert getattr(adjacent, issue['adjacent_text_slot']).endswith(issue['untagged_digits'])
    assert asdict(fact) == original_fact and source.read_bytes() == original


@pytest.mark.parametrize('prefix,expected', [('9.', '9.16'), ('9,', '9,16'), ('9,700', '9,70016')])
def test_numeric_prefix_preserves_decimal_and_grouping_evidence(tmp_path, fact, prefix, expected):
    source = tmp_path / 'filing.htm'
    source.write_text(document(f'<p>{prefix}{TAG.replace("206,376,66", "16")}</p>'))
    fact = replace(fact, numeric_value=Decimal('16'), raw_value='16')
    issue, = check_inline_correspondence(source, [fact])['numeric_tag_boundary_issues']
    assert issue['visible_display'] == expected


def test_numeric_prefix_before_tagged_fraction_requires_review(tmp_path, fact):
    source = tmp_path / 'filing.htm'
    source.write_text(document(f'<p>9{TAG.replace("206,376,66", ".5")}</p>'))
    fact = replace(fact, numeric_value=Decimal('0.5'), raw_value='0.5')
    result = check_inline_correspondence(source, [fact])
    assert result['numeric_values_equal'] == 1
    issue, = result['numeric_tag_boundary_issues']
    assert issue['visible_display'] == '9.5'


@pytest.mark.parametrize('preceding', [
    '<sup>1</sup>', '<sub>1</sub>', '<a href="#note">1</a>',
    '<span style="vertical-align:super">1</span>', '<span hidden="hidden">1</span>',
    '<span style="display:none">1</span>', '<span style="margin-right:3pt">1</span>',
    '<span>1 </span>', '<span>1st</span>', '<span>item1</span>',
    '<div>1</div>', '1<br/>',
    '<ix:nonFraction id="other" name="dei:EntityCommonStockSharesOutstanding" contextRef="asof" '
    'unitRef="shares" decimals="INF">1</ix:nonFraction>',
])
def test_prefix_guard_does_not_join_notes_values_or_blocks(tmp_path, fact, preceding):
    source = tmp_path / 'filing.htm'
    source.write_text(document(f'<p>{preceding}<span>{TAG}</span></p>'))
    assert check_inline_correspondence(source, [fact])['numeric_tag_boundary_issues'] == []


@pytest.mark.parametrize('spacing', [' ', '\n', '\xa0'])
def test_leading_space_inside_tag_separates_preceding_value(tmp_path, fact, spacing):
    source = tmp_path / 'filing.htm'
    spaced = TAG.replace('206,376,66', spacing + '206,376,66')
    source.write_text(document(f'<p>9<span>{spaced}</span></p>'))
    assert check_inline_correspondence(source, [fact])['numeric_tag_boundary_issues'] == []
