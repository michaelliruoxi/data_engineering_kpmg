"""Supported registry subsets transform source values without target borrowing."""

from decimal import Decimal, localcontext
import unittest

from sec_pipeline.inline_value_transforms import numeric_value, nonnumeric_value, validate_transform, UnsupportedInlineTransform


XII = '{http://www.xbrl.org/inlineXBRL/transformation/2020-02-12}'
XII22 = '{http://www.xbrl.org/inlineXBRL/transformation/2022-02-16}'
TR3 = '{http://www.xbrl.org/inlineXBRL/transformation/2015-02-26}'
SEC = '{http://www.sec.gov/inlineXBRL/transformation/2015-08-31}'


class InlineValueTransformTests(unittest.TestCase):
    def test_state_province_names_are_exact_postal_mappings(self):
        examples = {'Michigan': 'MI', 'Idaho': 'ID', 'Georgia': 'GA', 'Ohio': 'OH',
                    'Washington': 'WA', 'District of Columbia': 'DC', 'New York': 'NY',
                    'North Dakota': 'ND', 'Alberta': 'AB', 'Ontario': 'ON', 'Quebec': 'QC',
                    'Newfoundland and Labrador': 'NL', 'Northwest Territories': 'NT', 'Yukon': 'YT'}
        for namespace in (SEC, SEC.replace('http:', 'https:')):
            for name, code in examples.items():
                with self.subTest(name=name, namespace=namespace):
                    self.assertEqual(nonnumeric_value(name, namespace + 'stateprovnameen'), code)
                    self.assertEqual(nonnumeric_value('  ' + name.upper() + '  ', namespace + 'stateprovnameen'), code)
        for value in ('MI', 'Idaho or Michigan', 'United States', 'Canada', 'Michigan State', 'Ontario, Canada', ''):
            with self.subTest(value=value), self.assertRaises(UnsupportedInlineTransform):
                nonnumeric_value(value, SEC + 'stateprovnameen')
        with self.assertRaises(UnsupportedInlineTransform):
            nonnumeric_value('Michigan', '{https://invalid.test}stateprovnameen')

    def test_numeric_scaling_sign_and_precision_are_exact(self):
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(numeric_value('1,234,567.890123456789', XII + 'num-dot-decimal', scale='6', sign='-'),
                             Decimal('-1234567890123.456789'))
        for text, transform, result in [('—', XII + 'fixed-zero', '0'), ('five million', SEC + 'numwordsen', '5000000'),
                                        ('1 234.50', XII + 'num-dot-decimal', '1234.50'), ('0.000001', None, '0.000001')]:
            with self.subTest(text=text):
                self.assertEqual(numeric_value(text, transform), Decimal(result))

    def test_numeric_bad_grammar_namespace_and_kind_fail(self):
        for text, transform, options in [('1,23', XII + 'num-dot-decimal', {}), ('-1', None, {}),
                ('NaN', None, {}), ('INF', None, {}), ('1', '{https://invalid.test}num-dot-decimal', {}),
                ('1', None, {'sign': '+'}), ('1', None, {'scale': ''}), ('1', None, {'scale': '1.5'}), ('1', None, {'scale': '100001'}),
                ('1', '{http://www.xbrl.org/inlineXBRL/transformation/2010-04-20}numdotdecimal', {}),
                ('-', '{http://www.xbrl.org/inlineXBRL/transformation/2011-07-31}numdash', {}),
                ('12', XII + 'fixed-true', {}), ('many', SEC + 'numwordsen', {})]:
            with self.subTest(text=text, transform=transform, options=options), self.assertRaises(UnsupportedInlineTransform):
                numeric_value(text, transform, **options)
        with self.assertRaises(UnsupportedInlineTransform):
            validate_transform(XII + 'num-dot-decimal', 'nonnumeric')

    def test_nonnumeric_registry_dates_booleans_categories_and_durations(self):
        examples = [('June 30, 26', XII + 'date-monthname-day-year-en', '2026-06-30'),
                    ('2/29', XII + 'date-month-day', '--02-29'), ('6/30/2026', XII + 'date-month-day-year', '2026-06-30'),
                    ('☒', XII + 'fixed-true', 'true'), ('☐', SEC + 'boolballotbox', 'false'),
                    ('☑', SEC + 'yesnoballotbox', 'Yes'), ('Large Accelerated Filer', SEC + 'entityfilercategoryen', 'Large Accelerated Filer'),
                    ('New York Stock Exchange', SEC + 'exchnameen', 'NYSE'), ('Delaware', SEC + 'stateprovnameen', 'DE'),
                    ('5.3', SEC + 'duryear', 'P5Y3M18D'), ('-1.5', SEC + 'durmonth', '-P1M15D'),
                    ('0', SEC + 'durday', 'P0D'), ('three-year', SEC + 'durwordsen', 'P3Y'),
                    ('five years, two months and four days', SEC + 'durwordsen', 'P5Y2M4D')]
        for text, transform, expected in examples:
            with self.subTest(text=text, transform=transform):
                self.assertEqual(nonnumeric_value(text, transform), expected)
        self.assertEqual(nonnumeric_value('  literal\ntext\t'), '  literal\ntext\t')

    def test_values_match_edgar_extracted_instances(self):
        # Pairs observed in archived filings: inline text -> EDGAR's extracted XBRL value.
        examples = [
            ('0.5', SEC + 'duryear', 'P0Y6M'), ('0', SEC + 'duryear', 'P0Y'), ('0.13', SEC + 'duryear', 'P0Y1M17D'),
            ('2.58', SEC + 'duryear', 'P2Y6M29D'), ('4.07', SEC + 'duryear', 'P4Y25D'), ('5.98', SEC + 'duryear', 'P5Y11M23D'),
            ('22.3456', SEC + 'duryear', 'P22Y4M4D'), ('0', SEC + 'durmonth', 'P0M'), ('22.2', SEC + 'durmonth', 'P22M6D'),
            ('4.9', SEC + 'durday', 'P4DT21H'), ('1.1', SEC + 'durday', 'P1DT2H'), ('364', SEC + 'durday', 'P364D'),
            ('13', SEC + 'durweek', 'P91D'), ('53', SEC + 'durweek', 'P371D'),
            ('Q1 2028', SEC + 'datequarterend', '2028-03-31'), ('Q4 2027', SEC + 'datequarterend', '2027-12-31'),
            ('January 2028', XII + 'date-monthname-year-en', '2028-01'), ('Jan-24', XII22 + 'date-monthname-year-en', '2024-01'),
            ('December, 2026', XII22 + 'date-monthname-year-en', '2026-12'), ('Sept-24', XII22 + 'date-monthname-year-en', '2024-09'),
            ('31 December', XII + 'date-day-monthname-en', '--12-31'),
            ('31 March 2026', XII + 'date-day-monthname-year-en', '2026-03-31'),
            ('2026-03', XII22 + 'date-year-month', '2026-03'), ('2027-06-01', XII22 + 'date-year-month-day', '2027-06-01'),
            ('Jan. 11, 2028', TR3 + 'datemonthdayyearen', '2028-01-11'), ('adopted', TR3 + 'booleanfalse', 'false'),
            ('x', TR3 + 'booleantrue', 'true'), ('Ireland', SEC + 'countrynameen', 'IE'),
            ('England', SEC + 'countrynameen', 'GB'), ('United States of America', SEC + 'countrynameen', 'US'),
            ('Ireland', SEC + 'edgarprovcountryen', 'L2'), ('Bermuda', SEC + 'edgarprovcountryen', 'D0'),
            ('Jersey', SEC + 'edgarprovcountryen', 'Y9'), ('Delaware', SEC + 'edgarprovcountryen', 'DE'),
            ('Ontario', SEC + 'edgarprovcountryen', 'A6'), ('NYSE American LLC', SEC + 'exchnameen', 'NYSEAMER'),
            ('Nasdaq', SEC + 'exchnameen', 'NASDAQ'), ('NYSE Arca, Inc.', SEC + 'exchnameen', 'NYSEArca'),
            ('Chicago Stock Exchange, Inc.', SEC + 'exchnameen', 'CHX'),
            ('Non‑accelerated filer', SEC + 'entityfilercategoryen', 'Non-accelerated Filer'),
            ('five‑year', SEC + 'durwordsen', 'P5Y'),
        ]
        for text, transform, expected in examples:
            with self.subTest(text=text, transform=transform):
                kind = 'numeric' if transform.endswith('numwordsen') else 'nonnumeric'
                validate_transform(transform, kind)
                self.assertEqual(nonnumeric_value(text, transform), expected)
        for text, expected in (('nil', '0'), ('one hundred', '100'), ('two hundred fifty', '250')):
            with self.subTest(text=text):
                self.assertEqual(numeric_value(text, SEC + 'numwordsen'), Decimal(expected))

    def test_nonnumeric_unsupported_or_ambiguous_inputs_fail(self):
        for text, transform in [('February 29, 2025', XII + 'date-monthname-day-year-en'),
                                ('June 2026', XII + 'date-monthname-day-year-en'),
                                ('2.5', SEC + 'durweek'), ('Q5 2028', SEC + 'datequarterend'),
                                ('Atlantis', SEC + 'countrynameen'), ('Atlantis', SEC + 'edgarprovcountryen'),
                                ('2026-13', XII22 + 'date-year-month'), ('31 Smarch 2026', XII + 'date-day-monthname-year-en'),
                                ('two months three years', SEC + 'durwordsen'), ('three years extra', SEC + 'durwordsen'),
                                ('true', SEC + 'boolballotbox'), ('☒', '{https://invalid.test}fixed-true'),
                                ('June 30, 2026', XII + 'unknown-date')]:
            with self.subTest(text=text, transform=transform), self.assertRaises(UnsupportedInlineTransform):
                nonnumeric_value(text, transform)
