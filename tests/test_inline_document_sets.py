"""Complete source-set correspondence retains original per-document evidence."""

from dataclasses import asdict, replace
import hashlib
from html import escape
import json
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

from lxml import html

from sec_pipeline import fact_extraction as fx
from sec_pipeline.inline_document_sets import (
    InlineDocumentSetError, InlineSourceDocument, MARKUP_POLICY, read_inline_document_set,
)
from tests.test_fact_extraction import DEFINITIONS, EX, HEAD, FILING, SOURCE


ROOT = Path(__file__).resolve().parents[1]
XII = 'http://www.xbrl.org/inlineXBRL/transformation/2020-02-12'
NAMESPACES = f'''xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="{fx.IX}"
 xmlns:xbrli="{fx.XBRLI}" xmlns:xbrldi="{fx.XBRLDI}" xmlns:xsi="{fx.XSI}"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:ex="{EX}" xmlns:ixt="{XII}"
 xmlns:sec="http://www.sec.gov/inlineXBRL/transformation/2015-08-31"'''


def numeric(identifier='f1', text='12', **attrs):
    attributes = {'id': identifier, 'name': 'ex:Assets', 'contextRef': 'day', 'unitRef': 'usd', 'decimals': 'INF', **attrs}
    return '<ix:nonFraction ' + ' '.join(f'{key}="{value}"' for key, value in attributes.items()) + '>' + text + '</ix:nonFraction>'


def nonnumeric(identifier='f1', text='Original text.', **attrs):
    attributes = {'id': identifier, 'name': 'ex:Remark', 'contextRef': 'day', **attrs}
    return '<ix:nonNumeric ' + ' '.join(f'{key}="{value}"' for key, value in attributes.items()) + '>' + text + '</ix:nonNumeric>'


def target_numeric(identifier='f1', value='12', **attrs):
    attributes = {'id': identifier, 'contextRef': 'day', 'unitRef': 'usd', 'decimals': 'INF', **attrs}
    return '<ex:Assets ' + ' '.join(f'{key}="{value}"' for key, value in attributes.items()) + '>' + value + '</ex:Assets>'


def target_text(identifier='f1', value='Original text.', **attrs):
    attributes = {'id': identifier, 'contextRef': 'day', **attrs}
    return '<ex:Remark ' + ' '.join(f'{key}="{value}"' for key, value in attributes.items()) + '>' + escape(value, quote=False) + '</ex:Remark>'


class InlineDocumentSetTests(unittest.TestCase):
    def fixture(self, body, target, *, extra='', definitions=DEFINITIONS, extra_definitions='', root_attrs='', extra_root_attrs=''):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        xml = HEAD.replace('SCHEMA', 'https://example.invalid/taxonomy/2026/ex.xsd') + DEFINITIONS + target + '</xbrli:xbrl>'
        path = root / 'instance.xml'
        path.write_text(xml, encoding='utf-8')
        facts = fx.extract_facts(path, filing_id=FILING, source_document_id=SOURCE).require_complete().facts
        docs = []
        for index, (name, content, defs, attributes) in enumerate((('primary.htm', body, definitions, root_attrs),
                                                                 ('secondary.htm', extra, extra_definitions, extra_root_attrs))):
            markup = f'<html {NAMESPACES} {attributes}><head><title>Report</title></head><body><ix:header><ix:resources>{defs}</ix:resources></ix:header>{content}</body></html>'
            source = root / name
            source.write_text(markup, encoding='utf-8')
            docs.append(InlineSourceDocument(UUID(int=10 + index), name, source, hashlib.sha256(source.read_bytes()).hexdigest(),
                                             'https://www.sec.gov/Archives/edgar/data/1/000000000126000001/' + name))
        return docs, facts, hashlib.sha256(path.read_bytes()).hexdigest()

    def read(self, body, target, **options):
        docs, facts, digest = self.fixture(body, target, **options)
        return read_inline_document_set(docs, facts, xml_sha256=digest)

    def test_cross_document_resources_continuations_and_executable_original_locators(self):
        docs, facts, digest = self.fixture(nonnumeric(text='A<ix:exclude>excluded</ix:exclude>B', continuedAt='continued'),
                                          target_text(value='ABC D') + target_numeric('f2', '1250000'),
                                          extra='<ix:continuation id="continued">C <b>D</b></ix:continuation>'
                                                + numeric('f2', '1,250', format='ixt:num-dot-decimal', scale='3'))
        before = [asdict(fact) for fact in facts]
        result = read_inline_document_set(docs, facts, xml_sha256=digest)
        self.assertEqual([asdict(fact) for fact in facts], before)
        self.assertEqual((result.inventory['facts'], result.inventory['matched'], result.inventory['continuations']), (2, 2, 1))
        text, number = result.occurrences
        self.assertEqual(text['evidence']['inline_mapped_value'], 'ABC D')
        self.assertEqual([part['source_document_id'] for part in text['fragments']], [str(UUID(int=10)), str(UUID(int=11))])
        self.assertEqual(number['inline_document_id'], str(UUID(int=11)))
        self.assertEqual(number['evidence']['context_source']['source_document_id'], str(UUID(int=10)))
        self.assertEqual(number['evidence']['unit_source']['source_document_id'], str(UUID(int=10)))
        trees = {str(doc.source_document_id): html.document_fromstring(Path(doc.path).read_bytes()).getroottree() for doc in docs}
        for occurrence in result.occurrences:
            self.assertEqual(trees[occurrence['inline_document_id']].xpath(occurrence['source_xpath'])[0].get('id'), occurrence['inline_id'])
            for fragment in occurrence['evidence']['continuation_fragments']:
                tree = trees[fragment['source_document_id']]
                node = tree.xpath(fragment['source_xpath'])[0]
                self.assertEqual(node.get('id'), fragment['element_id'])
                self.assertEqual(tree.getpath(node), fragment['inline_xpath'])

    def test_equal_semantics_with_distinct_occurrence_ids_are_not_deduplicated(self):
        result = self.read(numeric('one') + numeric('two'), target_numeric('one') + target_numeric('two'))
        self.assertEqual([item['inline_id'] for item in result.occurrences], ['one', 'two'])

    def test_nested_numeric_occurrences_keep_both_original_ids(self):
        result = self.read(numeric('outer', numeric('inner')), target_numeric('outer') + target_numeric('inner'))
        self.assertEqual(result.inventory['matched'], 2)

    def test_duplicate_ids_missing_ids_and_nonbijective_sets_fail(self):
        cases = [
            (numeric() + numeric(), target_numeric(), ''),
            (numeric(), target_numeric(), numeric()),
            (numeric().replace('id="f1"', ''), target_numeric(), ''),
            (numeric(), target_numeric() + target_numeric('absent'), ''),
            (numeric() + numeric('unexpected'), target_numeric(), ''),
            (numeric(), target_numeric(), '<span id="day">duplicate context ID</span>'),
        ]
        for body, target, extra in cases:
            with self.subTest(body=body, extra=extra), self.assertRaises(InlineDocumentSetError):
                self.read(body, target, extra=extra)

    def test_missing_shared_or_cyclic_continuation_fragments_fail(self):
        cases = [
            (nonnumeric(continuedAt='missing'), ''),
            (nonnumeric(continuedAt='a'), '<ix:continuation id="a" continuedAt="b"/><ix:continuation id="b" continuedAt="a"/>'),
            (nonnumeric(continuedAt='a') + '<ix:footnote id="foot" continuedAt="a"/>', '<ix:continuation id="a"/>'),
            (nonnumeric(), '<ix:continuation id="orphan"/>'),
            (nonnumeric(text='<ix:continuation id="a">Original text.</ix:continuation>', continuedAt='a'), ''),
            (nonnumeric(continuedAt='a'), '<ix:hidden><ix:continuation id="a"/></ix:hidden>'),
        ]
        for body, extra in cases:
            with self.subTest(body=body, extra=extra), self.assertRaises(InlineDocumentSetError):
                self.read(body, target_text(), extra=extra)

    def test_context_unit_name_language_precision_and_nil_conflicts_fail(self):
        for change in ('contextRef', 'unitRef', 'name', 'decimals', 'nil', 'language', 'entity', 'date', 'unit', 'dimensions'):
            body, definitions = numeric(), DEFINITIONS
            if change == 'contextRef':
                body = numeric(contextRef='missing')
            elif change == 'unitRef':
                body = numeric(unitRef='eps')
            elif change == 'name':
                body = numeric(name='ex:Other')
            elif change == 'decimals':
                body = numeric(decimals='3')
            elif change == 'nil':
                body = numeric(**{'xsi:nil': 'true'})
            elif change == 'language':
                body = numeric(**{'xml:lang': 'en'})
            elif change == 'entity':
                definitions = definitions.replace('0000000001', '0000000002')
            elif change == 'date':
                definitions = definitions.replace('2026-03-31', '2026-03-30')
            elif change == 'unit':
                definitions = definitions.replace('iso4217:USD', 'iso4217:CAD')
            elif change == 'dimensions':
                definitions = definitions.replace('</xbrli:entity>', '<xbrli:segment><xbrldi:explicitMember dimension="ex:Axis">ex:Member</xbrldi:explicitMember></xbrli:segment></xbrli:entity>')
            with self.subTest(change=change), self.assertRaises(InlineDocumentSetError):
                self.read(body, target_numeric(), definitions=definitions)

    def test_nil_empty_and_zero_remain_three_different_values(self):
        result = self.read(numeric('nil', '', **{'xsi:nil': 'true'}) + numeric('zero', '—', format='ixt:fixed-zero')
                           + nonnumeric('empty', ''), target_numeric('nil', '', **{'xsi:nil': 'true'})
                           + target_numeric('zero', '0') + target_text('empty', ''))
        self.assertEqual([item['evidence']['inline_mapped_value'] for item in result.occurrences], [None, '0', ''])
        for body in (numeric(text='0'), numeric(text='<ix:exclude>ignored</ix:exclude>', **{'xsi:nil': 'true'}),
                     numeric(text='', format='ixt:unknown', **{'xsi:nil': 'true'}),
                     numeric(text='', sign='+', **{'xsi:nil': 'true'}), numeric(text='', scale='', **{'xsi:nil': 'true'})):
            with self.subTest(body=body), self.assertRaises(InlineDocumentSetError):
                self.read(body, target_numeric(value='', **{'xsi:nil': 'true'}))

    def test_invalid_nested_numeric_mixed_text_and_attributes_fail(self):
        for body in (numeric('outer', numeric('a', '1') + numeric('b', '2')),
                     numeric('outer', '1' + numeric('a', '2')), numeric('outer', numeric('a', '1') + '2'),
                     numeric('outer', numeric('a', '12', scale='1'))):
            target = target_numeric('outer', '12') + (target_numeric('a', '1') + target_numeric('b', '2') if 'id="b"' in body
                     else target_numeric('a', '120' if 'scale=' in body else ('2' if '>2<' in body else '1')))
            with self.subTest(body=body), self.assertRaises(InlineDocumentSetError):
                self.read(body, target)
        for key, value in [('unitRef', 'usd'), ('scale', '3'), ('sign', '-'), ('decimals', '2'), ('precision', '5')]:
            with self.subTest(attribute=key), self.assertRaises(InlineDocumentSetError):
                self.read(nonnumeric(**{key: value}), target_text())

    def test_unsupported_targets_elements_namespaces_and_markup_fail(self):
        for suffix in ('<ix:tuple id="tuple"/>', '<ix:fraction id="fraction"/>', '<ix:unknown id="unknown"/>',
                       '<base href="https://invalid.test/"/>',
                       '<bad:nonFraction xmlns:bad="https://invalid.test/inline" id="foreign">12</bad:nonFraction>'):
            with self.subTest(suffix=suffix), self.assertRaises(InlineDocumentSetError):
                self.read(numeric() + suffix, target_numeric())
        for attrs in ({'target': ''}, {'tupleRef': 'tuple'}, {'escape': 'false'}, {'format': ''}, {'format': 'ex:num-dot-decimal'}):
            with self.subTest(attributes=attrs), self.assertRaises(InlineDocumentSetError):
                self.read(numeric(**attrs), target_numeric())
        with self.assertRaises(InlineDocumentSetError):
            self.read(numeric(), target_numeric(), root_attrs='xml:base="https://invalid.test/"')

    def test_source_hash_and_original_member_identity_are_not_optional(self):
        docs, facts, digest = self.fixture(numeric(), target_numeric())
        for altered in (replace(docs[0], sha256='0' * 64), replace(docs[0], filename='different.htm'),
                        replace(docs[0], source_document_id=SOURCE)):
            with self.subTest(descriptor=altered), self.assertRaises(InlineDocumentSetError):
                read_inline_document_set([altered, docs[1]], facts, xml_sha256=digest)
        with self.assertRaises(InlineDocumentSetError):
            read_inline_document_set([docs[0], docs[0]], facts, xml_sha256=digest)

    def test_plain_text_keeps_whitespace_exclusions_and_explicit_language(self):
        value = ' First\n line\tlast '
        result = self.read(nonnumeric(text=' First\n <b>line</b><ix:exclude>exclude me</ix:exclude>\tlast ', **{'xml:lang': 'en-US'}),
                           target_text(value=value, **{'xml:lang': 'en-US'}))
        self.assertEqual(result.occurrences[0]['evidence']['inline_mapped_value'], value)
        with self.assertRaises(InlineDocumentSetError):
            self.read(nonnumeric(text=value), target_text(value='First line last'))

    def test_registered_nonnumeric_transform_uses_original_displayed_value(self):
        for content, transform, expected in [('June 30, 2026', 'ixt:date-monthname-day-year-en', '2026-06-30'),
                                              ('☒', 'ixt:fixed-true', 'true'), ('5.3', 'sec:duryear', 'P5Y3M18D')]:
            with self.subTest(transform=transform):
                result = self.read(nonnumeric(text=content, format=transform), target_text(value=expected))
                self.assertEqual(result.occurrences[0]['evidence']['inline_mapped_value'], expected)
                with self.assertRaises(InlineDocumentSetError):
                    self.read(nonnumeric(text=content, format=transform), target_text(value=expected + 'changed'))

    def test_escaped_markup_permits_only_lossless_serialization_differences(self):
        source = '<p id="paragraph" class="example">A &amp; B<br/> C</p>'
        target = '<p class="example" id="paragraph">A &amp; B<br></br> C</p>'
        result = self.read(nonnumeric(text=source, escape='true'), target_text(value=target))
        evidence = result.occurrences[0]['evidence']
        self.assertEqual(evidence['value_comparison'], MARKUP_POLICY)
        self.assertNotEqual(evidence['inline_mapped_value'], evidence['raw_value'])
        self.assertEqual(evidence['inline_canonical_sha256'], evidence['xml_canonical_sha256'])
        for altered in (target.replace(' B', ' D'), target.replace('example', 'other'), target.replace(' C', 'C'),
                        target.replace('<br></br> C', ' C<br/>'), target.replace('id="paragraph"', '')):
            with self.subTest(target=altered), self.assertRaises(InlineDocumentSetError):
                self.read(nonnumeric(text=source, escape='true'), target_text(value=altered))

    def test_relative_markup_links_bind_to_their_own_original_document(self):
        target = '<a href="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/chart.htm">chart</a>'
        result = self.read('', target_text(value=target), extra=nonnumeric(text='<a href="chart.htm">chart</a>', escape='true'))
        self.assertEqual(result.occurrences[0]['evidence']['inline_mapped_value'], target)
        with self.assertRaises(InlineDocumentSetError):
            self.read('', target_text(value=target.replace('chart.htm', 'other.htm')),
                      extra=nonnumeric(text='<a href="chart.htm">chart</a>', escape='true'))

    def test_unsupported_markup_is_never_silently_dropped(self):
        for markup, target in [('<p>A<!-- hidden -->B</p>', '<p>AB</p>'),
                               ('<p>A<?sample text?>B</p>', '<p>AB</p>'),
                               ('<blockquote cite="other.htm">A</blockquote>', '<blockquote cite="other.htm">A</blockquote>')]:
            with self.subTest(markup=markup), self.assertRaises(InlineDocumentSetError):
                self.read(nonnumeric(text=markup, escape='true'), target_text(value=target))

    def test_set_wide_relationship_ids_must_resolve_without_ambiguity(self):
        relationship = '<ix:relationship fromRefs="f1" toRefs="note"/>'
        result = self.read(numeric(), target_numeric(), definitions=DEFINITIONS + relationship,
                           extra='<ix:footnote id="note" xml:lang="en">Original note</ix:footnote>')
        self.assertEqual((result.inventory['relationships'], result.inventory['footnotes']), (1, 1))
        for source, target in [('unknown', 'note'), ('f1', 'unknown'), ('f1', 'f1'), ('f1 f1', 'note')]:
            with self.subTest(source=source, target=target), self.assertRaises(InlineDocumentSetError):
                self.read(numeric(), target_numeric(), definitions=DEFINITIONS + f'<ix:relationship fromRefs="{source}" toRefs="{target}"/>',
                          extra='<ix:footnote id="note" xml:lang="en">Original note</ix:footnote>')


class ArchivedInlineDocumentSetTests(unittest.TestCase):
    def test_complete_clorox_and_citi_sets_preserve_every_xml_fact_and_source_member(self):
        from sec_pipeline.document_sets import verify_document_set
        for accession, expected, secondary_count in [('0000021076-26-000034', 2088, 2019), ('0000831001-26-000045', 9437, 27)]:
            path = ROOT / 'data/raw/sec' / accession / 'source_document_set_manifest.json'
            if not path.exists():
                self.skipTest('The approved archived document sets are not available locally.')
            with self.subTest(accession=accession):
                manifest = verify_document_set(path, ROOT)
                records = {record['source_url']: record for record in manifest['files']}
                docs = []
                for index, url in enumerate(manifest['document_set']['member_source_urls']):
                    record = records[url]
                    source = ROOT / record['local_path']
                    docs.append(InlineSourceDocument(UUID(int=10 + index), source.name, source, record['sha256'], url))
                instance = next(record for record in manifest['files'] if record['role'] == fx.XML_ROLE)
                facts = fx.extract_facts(ROOT / instance['local_path'], filing_id=FILING, source_document_id=SOURCE).require_complete().facts
                fingerprint = lambda: hashlib.sha256(json.dumps([asdict(fact) for fact in facts], default=str, sort_keys=True).encode()).hexdigest()
                before = fingerprint()
                result = read_inline_document_set(docs, facts, xml_sha256=instance['sha256'])
                self.assertEqual(fingerprint(), before)
                self.assertEqual((len(facts), result.inventory['matched'], result.inventory['inline_elements']), (expected,) * 3)
                self.assertEqual(sum(item['inline_document_id'] == str(UUID(int=11)) for item in result.occurrences), secondary_count)
                self.assertEqual({item['inline_id'] for item in result.occurrences}, {fact.occurrence_key for fact in facts})
                self.assertTrue(result.inventory['nonnumeric_markup_values_equal'] > 0)
                self.assertEqual(result.inventory['numeric_values_not_compared'], 0)
                self.assertEqual(result.inventory['mismatches'], [])
                self.assertEqual(result.inventory['numeric_values_equal'] + result.inventory['nonnumeric_values_equal']
                                 + result.inventory['nil_values_verified'], expected)
                from sec_pipeline.source_set_storage import FactSourceOccurrenceInput
                for index, item in enumerate(result.occurrences):
                    FactSourceOccurrenceInput(filing_id=FILING, document_set_id=UUID(int=3), fact_id=UUID(int=100 + index),
                        html_document_id=UUID(item['inline_document_id']), inline_id=item['inline_id'], source_xpath=item['source_xpath'],
                        evidence=item['evidence'], fragments=item['fragments'])
