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
