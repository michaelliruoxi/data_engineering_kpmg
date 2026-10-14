"""Offline discovery, transport boundaries, and immutable filing archives."""

from datetime import date
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from sec_pipeline.acquisition import (
    AccessBlocked, AcquisitionError, FilingIdentity, SecClient,
    _immutable_write, _registrant, discover_documents, discover_filings, download_filing, validate_sec_url,
)


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "raw" / "sec" / "0001193125-26-191507"
IDENTITY = "Research User test@example.org"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK0000789019.json"
FILING = FilingIdentity("0000789019", "Microsoft Corporation", "0001193125-26-191507",
                       "10-Q", "2026-04-29", "2026-03-31", "msft-20260331.htm")


def columns(*rows):
    names = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")
    return {name: [row[index] for row in rows] for index, name in enumerate(names)}


class SecTransportTests(unittest.TestCase):
    def test_identity_and_rate_configuration_are_validated_without_http(self):
        for identity in ("", "test@example.org", "Research test@example.com", "Name\r\nInjected: yes test@example.org"):
            with self.subTest(identity=identity), self.assertRaises(AcquisitionError):
                SecClient(identity)
        for interval in (0, 0.99, float("nan"), float("inf")):
            with self.subTest(interval=interval), self.assertRaises(AcquisitionError):
                SecClient(IDENTITY, interval_seconds=interval)
        with patch.dict("os.environ", {"EDGAR_IDENTITY": IDENTITY}):
            with SecClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok"))) as client:
                client.get(SUBMISSIONS)
                self.assertNotIn(IDENTITY, json.dumps(client.request_log))

    def test_unsafe_urls_rejected_before_transport(self):
        unsafe = (
            "http://www.sec.gov/robots.txt", "https://www.sec.gov.evil.test/robots.txt",
            "https://evil.test/robots.txt", "https://data.sec.gov@evil.test/robots.txt",
            "https://user@www.sec.gov/robots.txt", "https://www.sec.gov:443/robots.txt",
            SUBMISSIONS + "?redirect=https://evil.test", SUBMISSIONS + "#fragment",
            "https://data.sec.gov/submissions/%2e%2e/robots.txt",
            "https://data.sec.gov/submissions/../robots.txt", "https://www.sec.gov/cgi-bin/test",
            "https://data.sec.gov/submissions/CIK0000789019.json\n",
        )
        calls = []
        with SecClient(IDENTITY, transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
            for url in unsafe:
                with self.subTest(url=url), self.assertRaises(AcquisitionError):
                    client.get(url)
        self.assertEqual(calls, [])
        self.assertEqual(validate_sec_url(SUBMISSIONS), SUBMISSIONS)

    def test_access_block_permanently_stops_client(self):
        for status, data in ((401, b"unauthorized"), (403, b"forbidden"), (429, b"rate limit"),
                             (200, b"Your Request Originates from an Undeclared Automated Tool")):
            calls = []

            def respond(request):
                calls.append(request)
                return httpx.Response(status, content=data)

            with self.subTest(status=status), SecClient(IDENTITY, transport=httpx.MockTransport(respond)) as client:
                with self.assertRaises(AccessBlocked):
                    client.get(SUBMISSIONS)
                with self.assertRaises(AccessBlocked):
                    client.get(SUBMISSIONS)
                self.assertTrue(client.stopped)
                self.assertEqual(len(calls), 1)

    def test_redirect_not_followed(self):
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://example.org"})

        with SecClient(IDENTITY, transport=httpx.MockTransport(respond)) as client:
            with self.assertRaisesRegex(AcquisitionError, "302"):
                client.get(SUBMISSIONS)
        self.assertEqual(len(calls), 1)

    def test_one_second_spacing_applies_to_successive_requests(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok"))
        with SecClient(IDENTITY, transport=transport) as client:
            with patch("sec_pipeline.acquisition.time.monotonic", side_effect=[10.0, 10.5, 11.0]), \
                    patch("sec_pipeline.acquisition.time.sleep") as sleep:
                client.get(SUBMISSIONS)
                client.get(SUBMISSIONS)
            sleep.assert_called_once_with(0.5)

    def test_transient_retry_is_bounded_and_retains_declared_user_agent(self):
        calls = []

        def respond(request):
            calls.append(request)
            self.assertEqual(request.headers["User-Agent"], IDENTITY)
            return httpx.Response(503 if len(calls) < 3 else 200, content=b"source")

        with SecClient(IDENTITY, transport=httpx.MockTransport(respond)) as client, \
                patch("sec_pipeline.acquisition.time.sleep"):
            data, metadata = client.get(SUBMISSIONS)
        self.assertEqual(len(calls), 3)
        self.assertEqual(metadata["sha256"], hashlib.sha256(data).hexdigest())
        calls.clear()
        with SecClient(IDENTITY, max_retries=1, transport=httpx.MockTransport(respond)) as client, \
                patch("sec_pipeline.acquisition.time.sleep"):
            with self.assertRaisesRegex(AcquisitionError, "503"):
                client.get(SUBMISSIONS)
        self.assertEqual(len(calls), 2)

    def test_declared_and_streamed_size_limits(self):
        for headers, stream in (({"Content-Length": "9"}, None), ({}, httpx.ByteStream(b"too large"))):
            def respond(request):
                return httpx.Response(200, headers=headers, stream=stream or httpx.ByteStream(b"x"))

            with self.subTest(headers=headers), SecClient(IDENTITY, max_bytes=4,
                    transport=httpx.MockTransport(respond)) as client:
                with self.assertRaisesRegex(AcquisitionError, "size limit"):
                    client.get(SUBMISSIONS)


class DiscoveryTests(unittest.TestCase):
    def test_five_year_window_history_deduplication_and_amendment_exclusion(self):
        current_row = (FILING.accession_number, "10-Q", "2026-04-29", "2026-03-31", FILING.primary_document)
        recent = columns(current_row,
                         ("0000789019-26-000099", "10-Q/A", "2026-05-01", "2026-03-31", "amend.htm"),
                         ("0000789019-26-000098", "8-K", "2026-04-01", "", "current.htm"),
                         ("0000789019-26-000100", "10-Q", "2026-11-01", "2026-09-30", "future.htm"))
        history_name = "CIK0000789019-submissions-001.json"
        documents = {
            SUBMISSIONS: {"cik": 789019, "name": "Current Company Name", "filings": {
                "recent": recent, "files": [
                    {"name": "CIK0000789019-submissions-002.json", "filingFrom": "2017-01-01", "filingTo": "2021-10-12"},
                    {"name": history_name, "filingFrom": "2021-10-13", "filingTo": "2026-05-01"},
                    {"name": history_name, "filingFrom": "2021-10-13", "filingTo": "2026-05-01"},
                ]}},
            "https://data.sec.gov/submissions/" + history_name: columns(current_row,
                ("0000789019-21-000001", "10-K", "2021-10-13", "2021-06-30", "older.htm"),
                ("0000789019-21-000002", "10-K", "2021-10-12", "2021-06-30", "excluded.htm")),
        }
        calls = []

        def respond(request):
            calls.append(str(request.url))
            return httpx.Response(200, json=documents[str(request.url)])

        with SecClient(IDENTITY, transport=httpx.MockTransport(respond)) as client, \
                patch("sec_pipeline.acquisition.time.sleep"):
            selected = discover_filings(client, "789019", date(2021, 10, 13), "2026-10-13")
        self.assertEqual(len(calls), 2)
        self.assertEqual([item["filing_date"] for item in selected], ["2021-10-13", "2026-04-29"])
        self.assertEqual(selected[1]["filing_index_url"], FILING.filing_index_url)
        self.assertEqual(selected[0]["company"], "Current Company Name")

    def test_invalid_cik_columns_and_conflicting_accessions_fail(self):
        recent = columns((FILING.accession_number, "10-Q", "2026-04-29", "2026-03-31", FILING.primary_document))
        examples = [
            {"cik": 1, "name": "Test", "filings": {"recent": recent}},
            {"cik": 789019, "name": "Test", "filings": {"recent": recent | {"form": []}}},
            {"cik": 789019, "name": "Test", "filings": {"recent": columns(
                (FILING.accession_number, "10-Q", "2026-04-29", "2026-03-31", FILING.primary_document),
                (FILING.accession_number, "10-Q", "2026-04-30", "2026-03-31", FILING.primary_document))}},
        ]
        for document in examples:
            with self.subTest(document=document), SecClient(IDENTITY, transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=document))) as client:
                with self.assertRaises(AcquisitionError):
                    discover_filings(client, "789019", "2021-10-13", "2026-10-13")

    def test_historical_filename_cannot_point_to_other_issuer(self):
        document = {"cik": 789019, "name": "Test", "filings": {"recent": columns(), "files": [
            {"name": "CIK0000320193-submissions-001.json", "filingFrom": "2022-01-01", "filingTo": "2023-01-01"}]}}
        with SecClient(IDENTITY, transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=document))) as client:
            with self.assertRaisesRegex(AcquisitionError, "does not match"):
                discover_filings(client, "789019", "2021-10-13", "2026-10-13")
            self.assertEqual(len(client.request_log), 1)


class DocumentDiscoveryTests(unittest.TestCase):
    def test_registrant_allows_http_and_https_dei_but_not_similar_hosts(self):
        template = '''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:dei="{namespace}">
            <xbrli:context id="current"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">42</xbrli:identifier></xbrli:entity>
            <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period></xbrli:context>
            <dei:EntityCentralIndexKey contextRef="current">42</dei:EntityCentralIndexKey>
            <dei:EntityRegistrantName contextRef="current">Historical Company</dei:EntityRegistrantName></xbrli:xbrl>'''
        for namespace in ("http://xbrl.sec.gov/dei/2025", "https://xbrl.sec.gov/dei/2025"):
            self.assertEqual(_registrant(template.format(namespace=namespace).encode(), "0000000042"), "Historical Company")
        with self.assertRaises(AcquisitionError):
            _registrant(template.format(namespace="https://xbrlXsecXgov/dei/2025").encode(), "0000000042")

    def test_real_index_selects_inline_html_extracted_instance_and_schema(self):
        sources = discover_documents((SAMPLE / (FILING.accession_number + "-index.html")).read_bytes(), FILING)
        self.assertEqual(sources, {
            "main_inline_xbrl_filing": FILING.archive_url + "msft-20260331.htm",
            "extracted_xbrl_instance": FILING.archive_url + "msft-20260331_htm.xml",
            "xbrl_extension_schema": FILING.archive_url + "msft-20260331.xsd",
        })

    def test_ex101_instance_and_simple_filename_links(self):
        data = b'''<html><table>
            <tr><td>1</td><td>Quarterly report</td><td><a href="msft-20260331.htm">main</a></td><td>10-Q</td></tr>
            <tr><td>2</td><td>Instance</td><td><a href="different.xml">instance</a></td><td>EX-101.INS</td></tr>
            <tr><td>3</td><td>Schema</td><td><a href="different.xsd">schema</a></td><td>EX-101.SCH</td></tr>
            </table></html>'''
        sources = discover_documents(data, FILING)
        self.assertTrue(sources["extracted_xbrl_instance"].endswith("different.xml"))
        for wrong in (b"https://evil.test/different.xml", b"../different.xml", b"%2e%2e/different.xml"):
            with self.subTest(wrong=wrong), self.assertRaises(AcquisitionError):
                discover_documents(data.replace(b'href="different.xml"', b'href="' + wrong + b'"'), FILING)

    def test_missing_instance_is_explicit_failure(self):
        data = (SAMPLE / (FILING.accession_number + "-index.html")).read_bytes()
        with self.assertRaisesRegex(AcquisitionError, "unique HTML"):
            discover_documents(data.replace(b"INSTANCE DOCUMENT", b"OTHER DOCUMENT"), FILING)


class DownloadTests(unittest.TestCase):
    def _joint_client(self, manifest, discovered, payloads, calls, *, canonical_index=None):
        canonical_url = manifest["filing_index_url"]
        urls = {record["source_url"]: payloads[record["role"]]
                for record in [manifest["index_document"], *manifest["files"]]}
        urls[discovered["filing_index_url"]] = payloads["filing_index"]
        if canonical_index is not None:
            urls[canonical_url] = canonical_index

        def respond(request):
            url = str(request.url)
            calls.append(url)
            return httpx.Response(200, content=urls[url])

        return SecClient(IDENTITY, transport=httpx.MockTransport(respond))

    def test_joint_filing_fetches_actual_canonical_index_and_reuses_archive_offline(self):
        from tests.test_registrants import joint_fixture

        manifest, discovered, payloads = joint_fixture()
        calls = []
        with tempfile.TemporaryDirectory() as temporary, self._joint_client(manifest, discovered, payloads, calls) as client, \
                patch("sec_pipeline.acquisition.time.sleep"):
            root = Path(temporary)
            path = download_filing(client, discovered, root)
            saved = path.read_bytes()
            archived = json.loads(saved)
            self.assertEqual(calls[:2], [discovered["filing_index_url"], manifest["filing_index_url"]])
            self.assertEqual(len(calls), 5)
            self.assertEqual(archived["cik"], "0000000042")
            self.assertEqual(archived["company"], "Example Holdings plc")
            self.assertEqual(archived["index_document"]["source_url"], manifest["filing_index_url"])
            requests = archived["download_attempts"][0]["requests"]
            self.assertEqual(requests[1]["source_url"], archived["index_document"]["source_url"])
            self.assertEqual(requests[1]["retrieved_at_utc"], archived["index_document"]["retrieved_at_utc"])
            self.assertEqual(download_filing(client, discovered, root), path)
            self.assertEqual(len(calls), 5)
            self.assertEqual(path.read_bytes(), saved)

    def test_canonical_and_alias_index_disagreement_stops_before_source_download(self):
        from tests.test_registrants import joint_fixture

        manifest, discovered, payloads = joint_fixture()
        calls = []
        changed = payloads["filing_index"].replace(b"Affiliate Company (Filer)", b"Different Company (Filer)")
        with tempfile.TemporaryDirectory() as temporary, self._joint_client(
                manifest, discovered, payloads, calls, canonical_index=changed) as client, \
                patch("sec_pipeline.acquisition.time.sleep"):
            root = Path(temporary)
            with self.assertRaisesRegex(AcquisitionError, "indices disagree"):
                download_filing(client, discovered, root)
            self.assertEqual(len(calls), 2)
            self.assertEqual(list(root.rglob("source_manifest.json")), [])

    def test_block_during_canonical_index_lookup_keeps_access_block_signal(self):
        from tests.test_registrants import joint_fixture

        manifest, discovered, payloads = joint_fixture()
        calls = []

        def respond(request):
            calls.append(str(request.url))
            if str(request.url) == discovered["filing_index_url"]:
                return httpx.Response(200, content=payloads["filing_index"])
            return httpx.Response(403, content=b"Forbidden")

        with tempfile.TemporaryDirectory() as temporary, SecClient(IDENTITY, transport=httpx.MockTransport(respond)) as client, \
                patch("sec_pipeline.acquisition.time.sleep"):
            with self.assertRaises(AccessBlocked):
                download_filing(client, discovered, Path(temporary))
            self.assertTrue(client.stopped)
            self.assertEqual(calls, [discovered["filing_index_url"], manifest["filing_index_url"]])
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_failed_requested_dei_binding_never_publishes_verified_archive(self):
        from tests.test_registrants import joint_fixture

        manifest, discovered, payloads = joint_fixture()
        payloads["extracted_xbrl_instance"] = payloads["extracted_xbrl_instance"].replace(
            b"dei:LegalEntityAxis", b"dei:OtherAxis")
        calls = []
        with tempfile.TemporaryDirectory() as temporary, self._joint_client(manifest, discovered, payloads, calls) as client, \
                patch("sec_pipeline.acquisition.time.sleep"):
            root = Path(temporary)
            with self.assertRaisesRegex(AcquisitionError, "LegalEntityAxis"):
                download_filing(client, discovered, root)
            self.assertEqual(len(calls), 5)
            self.assertEqual(list(root.iterdir()), [])

    def test_same_cik_legacy_index_does_not_require_filer_blocks(self):
        from lxml import html
        from tests.test_registrants import joint_fixture

        manifest, discovered, payloads = joint_fixture()
        discovered.update(cik=manifest["cik"], company=manifest["company"], filing_index_url=manifest["filing_index_url"])
        index = html.fromstring(payloads["filing_index"])
        for block in index.xpath("//div[@class='companyInfo']"):
            block.getparent().remove(block)
        index.find("body").text = "CIK 42 " + (index.find("body").text or "")
        payloads["filing_index"] = html.tostring(index)
        calls = []
        with tempfile.TemporaryDirectory() as temporary, self._joint_client(manifest, discovered, payloads, calls) as client, \
                patch("sec_pipeline.acquisition.time.sleep"):
            path = download_filing(client, discovered, Path(temporary))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["cik"], manifest["cik"])
            self.assertEqual(len(calls), 4)

    def test_concurrent_publication_preserves_winner_and_cleans_temporary_files(self):
        publish_name = "rename" if os.name == "nt" else "link"
        publish = getattr(os, publish_name)
        for winner in (b"matching payload", b"other writer payload"):
            with self.subTest(winner=winner), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "source.xml"

                def race(source, target):
                    Path(target).write_bytes(winner)
                    publish(source, target)

                with patch("sec_pipeline.acquisition.os." + publish_name, side_effect=race):
                    if winner == b"matching payload":
                        _immutable_write(path, b"matching payload")
                    else:
                        with self.assertRaisesRegex(AcquisitionError, "different bytes"):
                            _immutable_write(path, b"matching payload")
                self.assertEqual(path.read_bytes(), winner)
                self.assertEqual(list(path.parent.iterdir()), [path])

    def _client(self, calls, *, corrupt_xml=False):
        def respond(request):
            calls.append(str(request.url))
            filename = str(request.url).rsplit("/", 1)[-1]
            data = (SAMPLE / filename).read_bytes()
            if corrupt_xml and filename.endswith(".xml"):
                data = data.replace(b">789019<", b">320193<")
                data = data.replace(b">0000789019<", b">0000320193<")
            return httpx.Response(200, content=data)

        return SecClient(IDENTITY, transport=httpx.MockTransport(respond))

    def test_download_verified_archive_and_offline_replay_with_historical_name(self):
        calls = []
        discovery = FILING.to_dict() | {"company": "Changed Current Name"}
        with tempfile.TemporaryDirectory() as temporary, self._client(calls) as client, \
                patch("sec_pipeline.acquisition.time.sleep"):
            root = Path(temporary)
            manifest_path = download_filing(client, discovery, root)
            saved = manifest_path.read_bytes()
            manifest = json.loads(saved)
            self.assertEqual(len(calls), 4)
            self.assertEqual(manifest["company"], "MICROSOFT CORPORATION")
            self.assertEqual(manifest["discovery_company"], "Changed Current Name")
            for record in [manifest["index_document"], *manifest["files"]]:
                source = root / record["local_path"]
                self.assertEqual(source.read_bytes(), (SAMPLE / source.name).read_bytes())
                self.assertEqual(record["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertNotIn(IDENTITY, saved.decode("utf-8"))
            self.assertEqual(download_filing(client, discovery, root), manifest_path)
            self.assertEqual(len(calls), 4)
            self.assertEqual(saved, manifest_path.read_bytes())
            self.assertEqual(list(manifest_path.parent.glob("*.part")), [])

    def test_existing_different_bytes_are_not_overwritten_or_partially_published(self):
        calls = []
        with tempfile.TemporaryDirectory() as temporary, self._client(calls) as client, \
                patch("sec_pipeline.acquisition.time.sleep"):
            root = Path(temporary)
            directory = root / "data" / "raw" / "sec" / FILING.accession_number
            directory.mkdir(parents=True)
            source = directory / FILING.primary_document
            source.write_bytes(b"existing different source")
            with self.assertRaisesRegex(AcquisitionError, "different bytes"):
                download_filing(client, FILING, root)
            self.assertEqual(source.read_bytes(), b"existing different source")
            self.assertEqual(list(directory.iterdir()), [source])

    def test_invalid_sources_never_create_a_verified_manifest(self):
        calls = []
        with tempfile.TemporaryDirectory() as temporary, self._client(calls, corrupt_xml=True) as client, \
                patch("sec_pipeline.acquisition.time.sleep"):
            with self.assertRaises(AcquisitionError):
                download_filing(client, FILING, Path(temporary))
            self.assertEqual(list(Path(temporary).rglob("source_manifest.json")), [])

    def test_replay_revalidates_saved_sources_before_any_request(self):
        calls = []
        with tempfile.TemporaryDirectory() as temporary, self._client(calls) as client, \
                patch("sec_pipeline.acquisition.time.sleep"):
            root = Path(temporary)
            path = download_filing(client, FILING, root)
            source = path.parent / FILING.primary_document
            source.write_bytes(b"corrupted after download")
            with self.assertRaisesRegex(AcquisitionError, "failed verification"):
                download_filing(client, FILING, root)
            self.assertEqual(len(calls), 4)


if __name__ == "__main__":
    unittest.main()
