"""Offline checks for the download boundaries and source-integrity checks."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts.download_sec_sample import (
    AccessBlocked, BASE_URL, DOCUMENTS, INDEX_NAME, SampleError, SecReader,
    check_identity, sec_client, sha256, validate_document, verify_record,
)


class DownloadBoundaries(unittest.TestCase):
    def test_identity_requires_both_explicit_approval_and_contact(self):
        for identity, approved in (("Research User test@example.org", False),
                                   ("", True), ("test@example.org", True),
                                   ("Name\r\nInjected: header test@example.org", True)):
            with self.subTest(identity=identity, approved=approved):
                with self.assertRaises(SampleError):
                    check_identity(identity, approved)
        self.assertEqual(check_identity("Research User test@example.org", True),
                         "Research User test@example.org")

    def test_block_stops_session_without_retry_or_next_file(self):
        for status, content in ((403, b"Forbidden"), (429, b"Too Many Requests"),
                                (200, b"Your Request Originates from an Undeclared Automated Tool")):
            with self.subTest(status=status):
                calls = []

                def respond(request):
                    calls.append(request)
                    return httpx.Response(status, content=content)

                with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                    reader = SecReader(client, [])
                    with self.assertRaises(AccessBlocked):
                        reader.get(BASE_URL + INDEX_NAME)
                    with self.assertRaises(SampleError):
                        reader.get(BASE_URL + DOCUMENTS["main_inline_xbrl_filing"])
                self.assertEqual(len(calls), 1)

    def test_redirect_is_not_followed(self):
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://example.org/"})

        with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False) as client:
            with self.assertRaises(SampleError):
                SecReader(client, []).get(BASE_URL + INDEX_NAME)
        self.assertEqual(len(calls), 1)

    def test_requests_are_spaced_by_at_least_two_seconds(self):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"body"))) as client:
            reader = SecReader(client, [])
            with patch("scripts.download_sec_sample.time.monotonic", side_effect=[10, 10.5, 12]), \
                 patch("scripts.download_sec_sample.time.sleep") as sleep:
                reader.get(BASE_URL + INDEX_NAME)
                reader.get(BASE_URL + DOCUMENTS["main_inline_xbrl_filing"])
                sleep.assert_called_once_with(1.5)

    def test_library_configuration_does_not_request_sec(self):
        # Windows asyncio uses a loopback socket pair internally; block HTTP requests,
        # while allowing that local bookkeeping in the library's rate limiter.
        with patch("httpx.Client.send", side_effect=AssertionError("Unexpected HTTP request")):
            with sec_client("Research User test@example.org") as client:
                self.assertEqual(client.headers["User-Agent"], "Research User test@example.org")
                self.assertFalse(client.follow_redirects)

    def test_xml_error_page_is_rejected(self):
        with self.assertRaises(SampleError):
            validate_document(b"<html><body>Service unavailable" + b" " * 600 + b"</body></html>",
                              "extracted_xbrl_instance")

    def test_inline_display_date_matches_the_canonical_reporting_period(self):
        sample = b'''<html><body>Microsoft Corporation
            <ix:nonNumeric name="dei:EntityRegistrantName">MICROSOFT CORPORATION</ix:nonNumeric>
            <ix:nonNumeric name="dei:EntityCentralIndexKey">789019</ix:nonNumeric>
            <ix:nonNumeric name="dei:DocumentType">10-Q</ix:nonNumeric>
            <ix:nonNumeric name="dei:DocumentPeriodEndDate"
              format="ixt:date-monthname-day-year-en">March 31, 2026</ix:nonNumeric>
            </body></html>''' + b" " * 500
        self.assertEqual(validate_document(sample, "main_inline_xbrl_filing")["result"], "passed")
        with self.assertRaises(SampleError):
            validate_document(sample.replace(b"March 31", b"March 30"), "main_inline_xbrl_filing")

    def test_corruption_is_rejected_without_modifying_existing_file(self):
        original, corrupted = b"original bytes", b"modified bytes"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "source.xml"
            path.write_bytes(corrupted)
            record = {"local_path": path.name, "size_bytes": len(original), "sha256": sha256(original)}
            with self.assertRaisesRegex(SampleError, "SHA-256 mismatch"):
                verify_record(root, record)
            self.assertEqual(path.read_bytes(), corrupted)


if __name__ == "__main__":
    unittest.main()
