"""Continued DEI values must retain complete dates without accepting broken chains."""

import unittest

from sec_pipeline.filing_sources import SourceVerificationError, verify_documents
from test_filing_sources import fixture, HTML_ROLE


class SourceContinuationTests(unittest.TestCase):
    def continued_fixture(self):
        manifest, payloads = fixture()
        source = payloads[HTML_ROLE].decode()
        source = source.replace('name="dei:DocumentPeriodEndDate" contextRef="current">2025-12-31',
            'name="dei:DocumentPeriodEndDate" contextRef="current" continuedAt="year">December 31')
        source = source.replace('</body>', '<ix:continuation id="year">, <ix:exclude>footnote</ix:exclude>2025</ix:continuation></body>')
        payloads[HTML_ROLE] = source.encode()
        return manifest, payloads

    def test_continued_date_excludes_non_fact_text(self):
        manifest, payloads = self.continued_fixture()
        self.assertEqual(verify_documents(manifest, payloads), manifest)

    def test_missing_duplicate_foreign_and_cyclic_targets_are_rejected(self):
        for change in (
            lambda b: b.replace(b'id="year"', b'id="different"'),
            lambda b: b.replace(b'</body>', b'<ix:continuation id="year">2025</ix:continuation></body>'),
            lambda b: b.replace(b'ix:continuation', b'foreign:continuation'),
            lambda b: b.replace(b'id="year"', b'id="year" continuedAt="year"'),
        ):
            with self.subTest(change=change):
                manifest, payloads = self.continued_fixture()
                payloads[HTML_ROLE] = change(payloads[HTML_ROLE])
                with self.assertRaises(SourceVerificationError):
                    verify_documents(manifest, payloads)


if __name__ == '__main__':
    unittest.main()
