"""Only exact original same-document links may use source preservation."""

from copy import deepcopy
import hashlib
import unittest
from uuid import UUID

from sec_pipeline.inline_document_sets import InlineDocumentSetError, MARKUP_POLICY, read_inline_document_set
from tests import test_inline_document_sets as fixtures
from tests.test_inline_document_sets import nonnumeric, target_text


class OriginalFragmentLinkTests(unittest.TestCase):
    fixture = fixtures.InlineDocumentSetTests.fixture
    read = fixtures.InlineDocumentSetTests.read

    def test_source_and_xml_keep_the_identical_local_fragment_with_explicit_proof(self):
        markup = '<p id="destination">Original text.</p><a href="#destination">See text</a>'
        docs, facts, digest = self.fixture(nonnumeric(text=markup, escape='true'), target_text(value=markup))
        before = deepcopy(facts)
        result = read_inline_document_set(docs, facts, xml_sha256=digest)
        evidence = result.occurrences[0]['evidence']
        self.assertEqual(facts, before)
        self.assertEqual(evidence['inline_mapped_value'], markup)
        self.assertEqual(evidence['value_comparison'], MARKUP_POLICY)
        self.assertEqual(evidence['inline_canonical_sha256'], evidence['xml_canonical_sha256'])
        self.assertEqual(evidence['inline_uri_mapping']['strategy'], 'preserved-original-local-fragments-v1')
        [link] = evidence['inline_uri_mapping']['links']
        self.assertEqual((link['href'], link['target_id'], link['source_document_id']), ('#destination', 'destination', str(UUID(int=10))))
        self.assertEqual(link['source_url'], docs[0].source_url)
        self.assertEqual(link['source_dom'], 'original_xhtml')
        self.assertEqual(evidence['continuation_fragments'][0]['mapped_fragment_sha256'], hashlib.sha256(markup.encode()).hexdigest())

    def test_already_accepted_resolved_links_keep_the_complete_previous_evidence(self):
        source = '<p id="destination">Original text.</p><a href="#destination">See text</a>'
        base = 'https://www.sec.gov/Archives/edgar/data/1/000000000126000001/primary.htm'
        target = source.replace('#destination', base + '#destination')
        result = self.read(nonnumeric(text=source, escape='true'), target_text(value=target))
        evidence = result.occurrences[0]['evidence']
        self.assertEqual(evidence['inline_mapped_value'], target)
        self.assertEqual(evidence['value_comparison'], 'exact')
        self.assertNotIn('inline_uri_mapping', evidence)
        self.assertNotIn('inline_canonical_sha256', evidence)
        self.assertEqual(evidence['continuation_fragments'][0]['mapped_fragment_sha256'], hashlib.sha256(target.encode()).hexdigest())

    def test_changed_fragment_host_path_and_markup_remain_failures(self):
        source = '<p id="destination">Original text.</p><p id="other">Other text.</p><a href="#destination">See text</a>'
        for target in (source.replace('href="#destination"', 'href="#other"'),
                       source.replace('href="#destination"', 'href="secondary.htm#destination"'),
                       source.replace('href="#destination"', 'href="https://example.invalid/primary.htm#destination"'),
                       source.replace('href="#destination"', 'href="https://www.sec.gov/other/primary.htm#destination"'),
                       source.replace('Original text.', 'Changed text.'), source.replace('See text', 'Different link text')):
            with self.subTest(target=target), self.assertRaises(InlineDocumentSetError):
                self.read(nonnumeric(text=source, escape='true'), target_text(value=target))

    def test_missing_cross_document_duplicate_and_empty_fragment_targets_fail(self):
        source = '<a href="#destination">See text</a>'
        cases = [(source, source, ''), (source, source, '<p id="destination">Elsewhere</p>'),
                 ('<p id="destination">A</p><p id="destination">B</p>' + source,
                  '<p id="destination">A</p><p id="destination">B</p>' + source, ''),
                 ('<a href="#">Empty</a>', '<a href="#">Empty</a>', '')]
        for body, target, extra in cases:
            with self.subTest(body=body, extra=extra), self.assertRaises(InlineDocumentSetError):
                self.read(nonnumeric(text=body, escape='true'), target_text(value=target), extra=extra)

    def test_absolute_original_url_cannot_be_downgraded_to_fragment(self):
        base = 'https://www.sec.gov/Archives/edgar/data/1/000000000126000001/primary.htm'
        source = '<p id="destination">Text</p><a href="' + base + '#destination">link</a>'
        with self.assertRaises(InlineDocumentSetError):
            self.read(nonnumeric(text=source, escape='true'), target_text(value=source.replace(base + '#destination', '#destination')))

    def test_local_link_proof_keeps_cross_document_continuation_ownership(self):
        first = '<p id="first">A</p><a href="#first">First</a>'
        second = '<p id="second">B</p><a href="#second">Second</a>'
        result = self.read(nonnumeric(text=first, escape='true', continuedAt='continued'), target_text(value=first + second),
                           extra='<ix:continuation id="continued">' + second + '</ix:continuation>')
        evidence = result.occurrences[0]['evidence']
        self.assertEqual([link['source_document_id'] for link in evidence['inline_uri_mapping']['links']],
                         [str(UUID(int=10)), str(UUID(int=11))])
        self.assertEqual([link['target_id'] for link in evidence['inline_uri_mapping']['links']], ['first', 'second'])
        altered = second.replace('href="#second"', 'href="#first"')
        with self.assertRaises(InlineDocumentSetError):
            self.read(nonnumeric(text=first, escape='true', continuedAt='continued'), target_text(value=first + altered),
                      extra='<ix:continuation id="continued">' + altered + '</ix:continuation>')

    def test_one_preserved_link_does_not_relax_other_relative_urls(self):
        source = '<p id="destination">Text</p><a href="#destination">local</a><a href="chart.htm">chart</a>'
        base = 'https://www.sec.gov/Archives/edgar/data/1/000000000126000001/'
        target = source.replace('href="chart.htm"', 'href="' + base + 'chart.htm"')
        result = self.read(nonnumeric(text=source, escape='true'), target_text(value=target))
        self.assertEqual(result.occurrences[0]['evidence']['inline_mapped_value'], target)
        for altered in (target.replace(base + 'chart.htm', 'chart.htm'),
                        target.replace(base + 'chart.htm', base + 'other.htm')):
            with self.subTest(target=altered), self.assertRaises(InlineDocumentSetError):
                self.read(nonnumeric(text=source, escape='true'), target_text(value=altered))


if __name__ == '__main__':
    unittest.main()
