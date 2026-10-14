"""Constrained English-number support must not guess at unsupported grammar."""

from decimal import Decimal

from lxml import etree
import pytest

from sec_pipeline.fact_extraction import _inline_number, _small_english_number


@pytest.mark.parametrize('phrase,expected', [
    ('five million', '5000000'), ('Twenty-One thousand', '21000'),
    ('one billion', '1000000000'), ('ninety-nine trillion', '99000000000000'),
    ('none', '0'), ('forty two', '42'),
])
def test_supported_magnitude_words(phrase, expected):
    assert _small_english_number(phrase) == expected


@pytest.mark.parametrize('phrase', ['million', 'zero million', 'five million million',
    'five millions', 'two hundred million', 'five million and two', 'minus five million'])
def test_unsupported_phrases_are_not_guessed(phrase):
    assert _small_english_number(phrase) is None


def test_signed_scaled_words_keep_exact_decimal_value():
    node = etree.fromstring(b'<n xmlns:t="http://www.sec.gov/inlineXBRL/transformation/2015-08-31" format="t:numwordsen" scale="-3" sign="-">five million</n>')
    assert _inline_number(node) == Decimal('-5000')


@pytest.mark.parametrize('scheme', ['http', 'https'])
@pytest.mark.parametrize('phrase,expected', [('five', '5'), ('nil', '0'), ('one hundred', '100')])
def test_sec_number_words_accept_either_registry_scheme(scheme, phrase, expected):
    namespace = f'{scheme}://www.sec.gov/inlineXBRL/transformation/2015-08-31'.encode()
    node = etree.fromstring(b'<n xmlns:t="' + namespace + b'" format="t:numwordsen">' + phrase.encode() + b'</n>')
    assert _inline_number(node) == Decimal(expected)


def test_number_words_from_other_namespaces_are_not_compared():
    node = etree.fromstring(b'<n xmlns:t="https://invalid.test/transformation" format="t:numwordsen">five</n>')
    assert _inline_number(node) is None
