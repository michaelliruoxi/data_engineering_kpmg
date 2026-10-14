"""Source identity, namespace, provenance, and local archive verification."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from sec_pipeline.filing_sources import (
    SourceVerificationError, archive_base, normalized_date, registrant_name, verify_documents, verify_filing,
)


ROOT = Path(__file__).resolve().parents[1]
HTML_ROLE = "main_inline_xbrl_filing"
XML_ROLE = "extracted_xbrl_instance"
SCHEMA_ROLE = "xbrl_extension_schema"
INDEX_ROLE = "filing_index"


def fixture(form="10-K"):
    period, filed = ("2025-12-31", "2026-02-20") if form == "10-K" else ("2026-03-31", "2026-04-30")
    manifest = {
        "schema_version": 1, "status": "verified", "company": "Example Holdings plc",
        "cik": "0000000042", "accession_number": "0000000042-26-000001", "form": form,
        "filing_date": filed, "reporting_period_end": period,
    }
    base = archive_base(manifest)
    names = {INDEX_ROLE: manifest["accession_number"] + "-index.html", HTML_ROLE: "report.htm",
             XML_ROLE: "report_htm.xml", SCHEMA_ROLE: "report.xsd"}
    manifest["filing_index_url"] = base + names[INDEX_ROLE]
    payloads = {
        INDEX_ROLE: f'''<html><body>Form {form} Accession {manifest['accession_number']} CIK 42
            Filing Date {filed} Period of Report {period}
            <a href="/ix?doc=/Archives/edgar/data/42/000000004226000001/report.htm">Report</a>
            <a href="report_htm.xml">Instance</a><a href="report.xsd">Schema</a>
            </body></html>'''.encode(),
        HTML_ROLE: f'''<html xmlns:dei="http://xbrl.sec.gov/dei/2025"
            xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
            xmlns:xbrli="http://www.xbrl.org/2003/instance"><body>
            <ix:header><ix:resources><xbrli:context id="current"><xbrli:entity>
            <xbrli:identifier scheme="http://www.sec.gov/CIK">42</xbrli:identifier></xbrli:entity>
            <xbrli:period><xbrli:instant>{period}</xbrli:instant></xbrli:period></xbrli:context></ix:resources></ix:header>
            <ix:nonNumeric name="dei:EntityCentralIndexKey" contextRef="current">42</ix:nonNumeric>
            <ix:nonNumeric name="dei:EntityRegistrantName" contextRef="current">EXAMPLE HOLDINGS PLC</ix:nonNumeric>
            <ix:nonNumeric name="dei:DocumentType" contextRef="current">{form}</ix:nonNumeric>
            <ix:nonNumeric name="dei:DocumentPeriodEndDate" contextRef="current">{period}</ix:nonNumeric>
            </body></html>'''.encode(),
        XML_ROLE: f'''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:dei="http://xbrl.sec.gov/dei/2025" xmlns:ex="https://example.test/taxonomy/2025"
            xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink">
            <link:schemaRef xlink:type="simple" xlink:href="report.xsd"/>
            <xbrli:context id="current"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">42</xbrli:identifier></xbrli:entity>
            <xbrli:period><xbrli:instant>{period}</xbrli:instant></xbrli:period></xbrli:context>
            <dei:EntityCentralIndexKey contextRef="current">0000000042</dei:EntityCentralIndexKey>
            <dei:EntityRegistrantName contextRef="current">Example Holdings plc</dei:EntityRegistrantName>
            <dei:DocumentType contextRef="current">{form}</dei:DocumentType>
            <dei:DocumentPeriodEndDate contextRef="current">{period}</dei:DocumentPeriodEndDate>
            </xbrli:xbrl>'''.encode(),
        SCHEMA_ROLE: b'''<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="https://example.test/taxonomy/2025">
            <xs:element name="ExampleConcept" type="xs:decimal"/></xs:schema>''',
    }
    records = {
        role: {"role": role, "local_path": "data/raw/sec/" + manifest["accession_number"] + "/" + name,
               "source_url": base + name, "sha256": hashlib.sha256(payloads[role]).hexdigest(),
               "size_bytes": len(payloads[role]), "status": "verified", "verification": {"result": "passed"},
               "retrieved_at_utc": "2026-10-13T00:00:00Z"}
        for role, name in names.items()
    }
    manifest["index_document"] = records[INDEX_ROLE]
    manifest["files"] = [records[role] for role in (HTML_ROLE, XML_ROLE, SCHEMA_ROLE)]
    return manifest, payloads


def save_fixture(root, manifest, payloads):
    for record in [manifest["index_document"], *manifest["files"]]:
        source = root / record["local_path"]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(payloads[record["role"]])
    path = root / "source_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


class FilingDocumentTests(unittest.TestCase):
    def test_annual_and_quarterly_identity_are_accepted(self):
        for form in ("10-K", "10-Q"):
            with self.subTest(form=form):
                manifest, payloads = fixture(form)
                self.assertEqual(verify_documents(manifest, payloads), manifest)

    def test_existing_real_microsoft_archive_remains_valid(self):
        manifest = verify_filing(ROOT / "source_manifest.json", ROOT)
        self.assertEqual(manifest["cik"], "0000789019")
        self.assertEqual(manifest["form"], "10-Q")
        self.assertEqual(len(manifest["files"]), 3)

    def test_inline_transformed_date_and_namespace_prefix_alias(self):
        manifest, payloads = fixture()
        payloads[HTML_ROLE] = payloads[HTML_ROLE].replace(b"2025-12-31", b"December 31, 2025")
        payloads[XML_ROLE] = payloads[XML_ROLE].replace(b"xmlns:dei=", b"xmlns:d=").replace(b"dei:", b"d:")
        self.assertEqual(verify_documents(manifest, payloads), manifest)
        for value in ("December 31, 2025", "Dec 31 2025", "12/31/2025", "31 December 2025"):
            self.assertEqual(normalized_date(value), "2025-12-31")

    def test_changed_document_identity_is_rejected(self):
        for role, before, after in (
            (HTML_ROLE, b">42<", b">43<"), (XML_ROLE, b">0000000042<", b">0000000043<"),
            (HTML_ROLE, b">10-K<", b">10-Q<"), (XML_ROLE, b">10-K<", b">10-Q<"),
            (HTML_ROLE, b">2025-12-31<", b">2025-12-30<"),
            (XML_ROLE, b">2025-12-31<", b">2025-12-30<"),
            (XML_ROLE, b">Example Holdings plc<", b">Different Holdings plc<"),
        ):
            with self.subTest(role=role, before=before):
                manifest, payloads = fixture()
                self.assertIn(before, payloads[role])
                payloads[role] = payloads[role].replace(before, after)
                with self.assertRaises(SourceVerificationError):
                    verify_documents(manifest, payloads)

    def test_missing_duplicate_and_unexpected_source_roles_are_rejected(self):
        for change in ("missing", "duplicate", "unexpected"):
            manifest, payloads = fixture()
            if change == "missing":
                payloads.pop(XML_ROLE)
            elif change == "duplicate":
                manifest["files"][0]["role"] = XML_ROLE
            else:
                payloads["extra_document"] = b"extra"
            with self.subTest(change=change), self.assertRaises(SourceVerificationError):
                verify_documents(manifest, payloads)

    def test_source_url_identity_and_index_link_are_checked(self):
        manifest, payloads = fixture()
        for bad_url in ("https://evil.test/report.htm", archive_base(manifest) + "../report.htm",
                        archive_base(manifest) + "report.htm?download=true"):
            changed = deepcopy(manifest)
            changed["files"][0]["source_url"] = bad_url
            with self.subTest(url=bad_url), self.assertRaises(SourceVerificationError):
                verify_documents(changed, payloads)
        payloads[INDEX_ROLE] = payloads[INDEX_ROLE].replace(b'href="report_htm.xml"', b'href="other.xml"')
        with self.assertRaisesRegex(SourceVerificationError, "does not link"):
            verify_documents(manifest, payloads)

    def test_index_dates_accession_and_cik_are_checked(self):
        for before, after in (
            (b"2026-02-20", b"2026-02-21"), (b"2025-12-31", b"2025-12-30"),
            (b"0000000042-26-000001", b"0000000042-26-000002"), (b"CIK 42", b"CIK 43"),
        ):
            with self.subTest(before=before):
                manifest, payloads = fixture()
                payloads[INDEX_ROLE] = payloads[INDEX_ROLE].replace(before, after)
                with self.assertRaises(SourceVerificationError):
                    verify_documents(manifest, payloads)

    def test_amendment_index_cannot_verify_an_original_filing(self):
        manifest, payloads = fixture("10-Q")
        payloads[INDEX_ROLE] = payloads[INDEX_ROLE].replace(b"Form 10-Q ", b"Form 10-Q/A ")
        with self.assertRaises(SourceVerificationError):
            verify_documents(manifest, payloads)

    def test_external_viewer_query_cannot_confirm_an_archive_link(self):
        manifest, payloads = fixture()
        payloads[INDEX_ROLE] = payloads[INDEX_ROLE].replace(b'href="/ix?doc=', b'href="https://evil.test/view?doc=')
        with self.assertRaises(SourceVerificationError):
            verify_documents(manifest, payloads)

    def test_fake_dei_namespace_is_not_a_source_identity(self):
        for role in (HTML_ROLE, XML_ROLE):
            with self.subTest(role=role):
                manifest, payloads = fixture()
                payloads[role] = payloads[role].replace(b"http://xbrl.sec.gov/dei/2025", b"http://xbrlXsecXgov/dei/2025")
                with self.assertRaises(SourceVerificationError):
                    verify_documents(manifest, payloads)

    def test_non_inline_xbrl_elements_cannot_spoof_dei_identity(self):
        for namespace in (b"https://example.test/not-inline-xbrl", b"http://wwwXxbrlXorg/2013/inlineXBRL"):
            with self.subTest(namespace=namespace):
                manifest, payloads = fixture()
                payloads[HTML_ROLE] = payloads[HTML_ROLE].replace(
                    b"http://www.xbrl.org/2013/inlineXBRL", namespace)
                with self.assertRaises(SourceVerificationError):
                    verify_documents(manifest, payloads)
        manifest, payloads = fixture()
        payloads[HTML_ROLE] = payloads[HTML_ROLE].replace(b"ix:nonNumeric", b"nonNumeric")
        with self.assertRaises(SourceVerificationError):
            verify_documents(manifest, payloads)

    def test_coregistrant_identity_uses_matching_dei_cik_context(self):
        manifest, payloads = fixture()
        context = b'''<xbrli:context id="affiliate"><xbrli:entity>
            <xbrli:identifier scheme="http://www.sec.gov/CIK">42</xbrli:identifier>
            <xbrli:segment><xbrldi:explicitMember xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
              xmlns:ex="https://example.test/taxonomy/2025" dimension="dei:LegalEntityAxis">ex:AffiliateMember</xbrldi:explicitMember></xbrli:segment>
            </xbrli:entity><xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period></xbrli:context>'''
        payloads[XML_ROLE] = payloads[XML_ROLE].replace(b"</xbrli:xbrl>", context + b'''
            <dei:EntityCentralIndexKey contextRef="affiliate">0000000043</dei:EntityCentralIndexKey>
            <dei:EntityRegistrantName contextRef="affiliate">Affiliate Company</dei:EntityRegistrantName>
            </xbrli:xbrl>''')
        payloads[HTML_ROLE] = payloads[HTML_ROLE].replace(b"</ix:resources>", context + b"</ix:resources>")
        payloads[HTML_ROLE] = payloads[HTML_ROLE].replace(b"</body>", b'''
            <ix:nonNumeric name="dei:EntityCentralIndexKey" contextRef="affiliate">43</ix:nonNumeric>
            <ix:nonNumeric name="dei:EntityRegistrantName" contextRef="affiliate">Affiliate Company</ix:nonNumeric>
            </body>''')
        self.assertEqual(registrant_name(payloads[XML_ROLE], "0000000042"), "Example Holdings plc")
        self.assertEqual(verify_documents(manifest, payloads), manifest)
        self.assertIn(b"Affiliate Company", payloads[XML_ROLE])

    def test_missing_duplicate_and_wrong_entity_contexts_are_rejected(self):
        for role in (HTML_ROLE, XML_ROLE):
            for change in ("missing", "duplicate", "entity_mismatch"):
                with self.subTest(role=role, change=change):
                    manifest, payloads = fixture()
                    if change == "missing":
                        payloads[role] = payloads[role].replace(b'id="current"', b'id="different"')
                    elif change == "duplicate":
                        duplicate = b'<xbrli:context id="current"/>'
                        end = b"</body>" if role == HTML_ROLE else b"</xbrli:xbrl>"
                        payloads[role] = payloads[role].replace(end, duplicate + end)
                    else:
                        payloads[role] = payloads[role].replace(b'>42</xbrli:identifier>', b'>43</xbrli:identifier>')
                    with self.assertRaises(SourceVerificationError):
                        verify_documents(manifest, payloads)

    def test_matching_cik_does_not_allow_an_ambiguous_registrant(self):
        manifest, payloads = fixture()
        payloads[XML_ROLE] = payloads[XML_ROLE].replace(b"</xbrli:xbrl>", b'''
            <dei:EntityRegistrantName contextRef="current">Another Company</dei:EntityRegistrantName>
            </xbrli:xbrl>''')
        with self.assertRaises(SourceVerificationError):
            registrant_name(payloads[XML_ROLE], manifest["cik"])
        with self.assertRaises(SourceVerificationError):
            verify_documents(manifest, payloads)

    def test_foreign_schema_reference_namespace_is_rejected(self):
        manifest, payloads = fixture()
        payloads[XML_ROLE] = payloads[XML_ROLE].replace(
            b'<link:schemaRef xlink:type="simple" xlink:href="report.xsd"/>',
            b'<other:schemaRef xmlns:other="https://example.test/not-xbrl" other:href="report.xsd"/>')
        with self.assertRaises(SourceVerificationError):
            verify_documents(manifest, payloads)

    def test_schema_namespace_reference_type_and_missing_context_fail(self):
        for role, before, after in (
            (SCHEMA_ROLE, b"https://example.test/taxonomy/2025", b"https://other.test/taxonomy/2025"),
            (XML_ROLE, b'xlink:href="report.xsd"', b'xlink:href="other.xsd"'),
            (SCHEMA_ROLE, b"xs:schema", b"xs:element"),
            (XML_ROLE, b"xbrli:context", b"xbrli:wrongContext"),
        ):
            manifest, payloads = fixture()
            payloads[role] = payloads[role].replace(before, after)
            with self.subTest(role=role, before=before), self.assertRaises(SourceVerificationError):
                verify_documents(manifest, payloads)

    def test_dtd_empty_and_access_block_sources_fail(self):
        for role, replacement in ((XML_ROLE, b'<!DOCTYPE xbrl [<!ENTITY injected "bad">]>'),
                                  (HTML_ROLE, b""), (INDEX_ROLE, b"Request rate threshold exceeded")):
            manifest, payloads = fixture()
            payloads[role] = replacement + payloads[role] if role == XML_ROLE else replacement
            with self.subTest(role=role), self.assertRaises(SourceVerificationError):
                verify_documents(manifest, payloads)


class LocalFilingTests(unittest.TestCase):
    def test_local_archive_rechecks_hash_and_size(self):
        manifest, payloads = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = save_fixture(root, manifest, payloads)
            self.assertEqual(verify_filing(path, root), manifest)
            source = root / manifest["files"][0]["local_path"]
            source.write_bytes(source.read_bytes().replace(b"EXAMPLE", b"CHANGED"))
            with self.assertRaisesRegex(SourceVerificationError, "hash|SHA-256"):
                verify_filing(path, root)

    def test_absolute_traversing_and_duplicate_local_paths_fail(self):
        for change in ("absolute", "traversal", "duplicate"):
            manifest, payloads = fixture()
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = save_fixture(root, manifest, payloads)
                if change == "absolute":
                    manifest["files"][0]["local_path"] = str(root / manifest["files"][0]["local_path"])
                elif change == "traversal":
                    manifest["files"][0]["local_path"] = "../outside.htm"
                else:
                    manifest["files"][0]["local_path"] = manifest["index_document"]["local_path"]
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(SourceVerificationError, "paths"):
                    verify_filing(path, root)

    def test_unverified_manifest_record_and_missing_receipt_fail(self):
        for change in ("manifest", "source", "verification", "timestamp"):
            manifest, payloads = fixture()
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = save_fixture(root, manifest, payloads)
                if change == "manifest":
                    manifest["status"] = "pending"
                elif change == "source":
                    manifest["files"][0]["status"] = "pending"
                elif change == "verification":
                    manifest["files"][0]["verification"] = {"result": "failed"}
                else:
                    manifest["files"][0]["retrieved_at_utc"] = ""
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(SourceVerificationError):
                    verify_filing(path, root)


if __name__ == "__main__":
    unittest.main()
