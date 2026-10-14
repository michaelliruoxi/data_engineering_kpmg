"""Canonical accession ownership and explicit co-registrant bindings."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from sec_pipeline.registrants import (
    RegistrantBindingError, canonical_identity, index_registrants, verify_discovery_binding, verify_document_binding,
)
from tests.test_filing_sources import fixture, save_fixture


ROOT = Path(__file__).resolve().parents[1]
INDEX = "filing_index"
HTML = "main_inline_xbrl_filing"
XML = "extracted_xbrl_instance"
XOM = ROOT / "data/raw/sec/0000034088-26-000093/source_manifest.json"


def joint_fixture():
    manifest, payloads = fixture("10-Q")
    base = "https://www.sec.gov/Archives/edgar/data/42/000000004226000001/"
    payloads[INDEX] = b'''<html><body>Form 10-Q Accession 0000000042-26-000001
        Filing Date 2026-04-30 Period of Report 2026-03-31
        <table class="tableFile">
        <tr><td>1</td><td>Quarterly report</td><td><a href="/ix?doc=/Archives/edgar/data/42/000000004226000001/report.htm">report.htm</a></td><td>10-Q</td></tr>
        <tr><td>2</td><td>Extracted XBRL instance</td><td><a href="report_htm.xml">instance</a></td><td>XML</td></tr>
        <tr><td>3</td><td>Schema</td><td><a href="report.xsd">schema</a></td><td>EX-101.SCH</td></tr>
        </table>
        <div class="companyInfo"><span class="companyName">Example Holdings plc (Filer)
        <acronym>CIK</acronym>: <a href="/cgi-bin/browse-edgar?CIK=0000000042&amp;action=getcompany">0000000042 (see all company filings)</a></span></div>
        <div class="companyInfo"><span class="companyName">Affiliate Company (Filer)
        <acronym>CIK</acronym>: <a href="/cgi-bin/browse-edgar?CIK=0000000043&amp;action=getcompany">0000000043 (see all company filings)</a></span></div>
        </body></html>'''
    context = b'''<xbrli:context id="affiliate"><xbrli:entity>
        <xbrli:identifier scheme="http://www.sec.gov/CIK">42</xbrli:identifier>
        <xbrli:segment><xbrldi:explicitMember xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
          xmlns:ex="https://example.test/taxonomy/2025" dimension="dei:LegalEntityAxis">ex:AffiliateMember</xbrldi:explicitMember></xbrli:segment>
        </xbrli:entity><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>'''
    payloads[XML] = payloads[XML].replace(b"</xbrli:xbrl>", context + b'''
        <dei:EntityCentralIndexKey contextRef="affiliate">0000000043</dei:EntityCentralIndexKey>
        <dei:EntityRegistrantName contextRef="affiliate">Affiliate Company</dei:EntityRegistrantName>
        </xbrli:xbrl>''')
    payloads[HTML] = payloads[HTML].replace(b"</ix:resources>", context + b"</ix:resources>")
    payloads[HTML] = payloads[HTML].replace(b"</body>", b'''
        <ix:nonNumeric name="dei:EntityCentralIndexKey" contextRef="affiliate">43</ix:nonNumeric>
        <ix:nonNumeric name="dei:EntityRegistrantName" contextRef="affiliate">Affiliate Company</ix:nonNumeric>
        </body>''')
    discovered = {key: manifest[key] for key in ("accession_number", "form", "filing_date", "reporting_period_end")}
    discovered.update(cik="0000000043", company="Affiliate Company", primary_document="report.htm",
                      filing_index_url=base.replace("/data/42/", "/data/43/") + manifest["accession_number"] + "-index.html")
    refresh_hashes(manifest, payloads)
    return manifest, discovered, payloads


def refresh_hashes(manifest, payloads):
    for record in [manifest["index_document"], *manifest["files"]]:
        data = payloads[record["role"]]
        record.update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))


def check_fixture(manifest, discovered, payloads):
    refresh_hashes(manifest, payloads)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        save_fixture(root, manifest, payloads)
        return verify_discovery_binding(manifest, discovered, root)


class CanonicalIdentityTests(unittest.TestCase):
    def test_unofficial_pdf_with_same_form_does_not_replace_primary_html(self):
        _, discovered, payloads = joint_fixture()
        expected = canonical_identity(payloads[INDEX], discovered)
        pdf = b'<tr><td>12</td><td>Unofficial PDF</td><td><a href="report.pdf">report.pdf</a></td><td>10-Q</td></tr>'
        data = payloads[INDEX].replace(b'</table>', pdf + b'</table>')
        self.assertEqual(canonical_identity(data, discovered), expected)

    def test_extra_link_on_exact_primary_row_remains_ambiguous(self):
        _, discovered, payloads = joint_fixture()
        data = payloads[INDEX].replace(b'>report.htm</a>', b'>report.htm</a><a href="report.pdf">PDF</a>')
        with self.assertRaisesRegex(RegistrantBindingError, 'ambiguous links'):
            canonical_identity(data, discovered)

    def test_secondary_discovery_keeps_original_accession_owner(self):
        manifest, discovered, payloads = joint_fixture()
        original = deepcopy(discovered)
        canonical, associations = canonical_identity(payloads[INDEX], discovered)
        self.assertEqual(canonical["cik"], "0000000042")
        self.assertEqual(canonical["company"], "Example Holdings plc")
        self.assertEqual(canonical["filing_index_url"], manifest["filing_index_url"])
        self.assertEqual(canonical["accession_number"], discovered["accession_number"])
        self.assertEqual(associations, [
            {"cik": "0000000042", "name": "Example Holdings plc", "is_canonical": True, "is_requested": False},
            {"cik": "0000000043", "name": "Affiliate Company", "is_canonical": False, "is_requested": True},
        ])
        self.assertEqual(discovered, original)

    def test_arbitrary_link_or_issuer_label_does_not_establish_requested_filer(self):
        for modification in ("label", "outside_block", "foreign_link", "wrong_link_cik"):
            manifest, discovered, payloads = joint_fixture()
            data = payloads[INDEX]
            if modification == "label":
                data = data.replace(b"Affiliate Company (Filer)", b"Affiliate Company (Issuer)")
            elif modification == "outside_block":
                data = data.replace(b'<div class="companyInfo"><span class="companyName">Affiliate',
                                    b'<div class="unrelated"><span class="companyName">Affiliate')
            elif modification == "foreign_link":
                data = data.replace(b'/cgi-bin/browse-edgar?CIK=0000000043', b'https://evil.test/cgi-bin/browse-edgar?CIK=0000000043')
            else:
                data = data.replace(b'CIK=0000000043', b'CIK=0000000099')
            with self.subTest(modification=modification), self.assertRaises(RegistrantBindingError):
                canonical_identity(data, discovered)

    def test_canonical_archive_owner_must_be_an_explicit_filer(self):
        _, discovered, payloads = joint_fixture()
        with self.assertRaisesRegex(RegistrantBindingError, "Canonical source CIK"):
            canonical_identity(payloads[INDEX].replace(b"/data/42/", b"/data/99/"), discovered)

    def test_main_document_host_accession_filename_and_ambiguity_are_rejected(self):
        for modification in ("host", "viewer_host", "accession", "filename", "duplicate", "form"):
            _, discovered, payloads = joint_fixture()
            data = payloads[INDEX]
            if modification == "host":
                data = data.replace(b"/ix?doc=/Archives", b"https://evil.test/ix?doc=/Archives")
            elif modification == "viewer_host":
                data = data.replace(b"/ix?doc=/Archives", b"/ix?doc=https://evil.test/Archives")
            elif modification == "accession":
                data = data.replace(b"/000000004226000001/report.htm", b"/000000004226000002/report.htm")
            elif modification == "filename":
                data = data.replace(b"/report.htm", b"/wrong.htm")
            elif modification == "duplicate":
                row = data[data.index(b"<tr>"):data.index(b"</tr>") + len(b"</tr>")]
                data = data.replace(b"</table>", row + b"</table>")
            else:
                data = data.replace(b"<td>10-Q</td>", b"<td>10-Q/A</td>")
            with self.subTest(modification=modification), self.assertRaises(RegistrantBindingError):
                canonical_identity(data, discovered)

    def test_discovery_dates_and_primary_name_must_match_index(self):
        _, discovered, payloads = joint_fixture()
        for key, value in (("filing_date", "2026-05-01"), ("reporting_period_end", "2026-03-30"),
                           ("primary_document", "different.htm")):
            with self.subTest(key=key), self.assertRaises(RegistrantBindingError):
                canonical_identity(payloads[INDEX], discovered | {key: value})


class DiscoveryBindingTests(unittest.TestCase):
    def test_payload_binding_is_pure_and_rejects_changed_receipt_hash(self):
        manifest, discovered, payloads = joint_fixture()
        self.assertTrue(verify_document_binding(manifest, discovered, payloads))
        changed = dict(payloads)
        changed[HTML] += b"changed"
        with self.assertRaisesRegex(RegistrantBindingError, "bytes changed"):
            verify_document_binding(manifest, discovered, changed)

    def test_index_registrants_are_deterministic_without_requested_flags(self):
        manifest, _, payloads = joint_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_fixture(root, manifest, payloads)
            expected = [
                {"cik": "0000000042", "name": "Example Holdings plc", "is_canonical": True},
                {"cik": "0000000043", "name": "Affiliate Company", "is_canonical": False},
            ]
            self.assertEqual(index_registrants(manifest, root), expected)
            self.assertEqual(index_registrants(deepcopy(manifest), root), expected)

    def test_secondary_form_and_period_inherit_verified_primary_without_mutation(self):
        manifest, discovered, payloads = joint_fixture()
        original_manifest, original_discovered = deepcopy(manifest), deepcopy(discovered)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = save_fixture(root, manifest, payloads)
            before = {source: source.read_bytes() for source in root.rglob("*") if source.is_file()}
            self.assertTrue(verify_discovery_binding(manifest, discovered, root))
            self.assertEqual(before, {source: source.read_bytes() for source in before})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original_manifest)
        self.assertEqual(manifest, original_manifest)
        self.assertEqual(discovered, original_discovered)

    def test_primary_discovery_keeps_ordinary_verification(self):
        manifest, discovered, payloads = joint_fixture()
        discovered.update(cik=manifest["cik"], company=manifest["company"], filing_index_url=manifest["filing_index_url"])
        self.assertTrue(check_fixture(manifest, discovered, payloads))

    def test_same_index_entry_requires_real_dei_cik_not_name_alone(self):
        for role in (XML, HTML):
            manifest, discovered, payloads = joint_fixture()
            before, after = (b">0000000043<", b">0000000099<") if role == XML else (b">43<", b">99<")
            payloads[role] = payloads[role].replace(before, after)
            with self.subTest(role=role), self.assertRaises(RegistrantBindingError):
                check_fixture(manifest, discovered, payloads)

    def test_secondary_axis_namespace_entity_and_member_are_not_optional(self):
        for change in ("axis", "namespace", "identifier", "member"):
            manifest, discovered, payloads = joint_fixture()
            if change == "axis":
                payloads[XML] = payloads[XML].replace(b"dei:LegalEntityAxis", b"dei:OtherAxis")
            elif change == "namespace":
                payloads[XML] = payloads[XML].replace(b"http://xbrl.org/2006/xbrldi", b"https://evil.test/dimensions")
            elif change == "identifier":
                start = payloads[XML].index(b'<xbrli:context id="affiliate">')
                payloads[XML] = payloads[XML][:start] + payloads[XML][start:].replace(b">42</xbrli:identifier>", b">99</xbrli:identifier>")
            else:
                payloads[XML] = payloads[XML].replace(b"ex:AffiliateMember", b"ex:DifferentMember")
            with self.subTest(change=change), self.assertRaises(RegistrantBindingError):
                check_fixture(manifest, discovered, payloads)

    def test_secondary_context_must_be_declared_once(self):
        for change in ("missing", "duplicate"):
            manifest, discovered, payloads = joint_fixture()
            if change == "missing":
                payloads[XML] = payloads[XML].replace(b'id="affiliate"', b'id="other"')
            else:
                payloads[XML] = payloads[XML].replace(b"</xbrli:xbrl>", b'<xbrli:context id="affiliate"/></xbrli:xbrl>')
            with self.subTest(change=change), self.assertRaises(RegistrantBindingError):
                check_fixture(manifest, discovered, payloads)

    def test_secondary_name_and_document_facts_cannot_contradict_sources(self):
        for change in ("name", "form", "period"):
            manifest, discovered, payloads = joint_fixture()
            if change == "name":
                payloads[HTML] = payloads[HTML].replace(b">Affiliate Company<", b">Different Company<")
            elif change == "form":
                payloads[XML] = payloads[XML].replace(b"</xbrli:xbrl>", b'<dei:DocumentType contextRef="affiliate">10-K</dei:DocumentType></xbrli:xbrl>')
            else:
                payloads[XML] = payloads[XML].replace(b"</xbrli:xbrl>", b'<dei:DocumentPeriodEndDate contextRef="affiliate">2026-03-30</dei:DocumentPeriodEndDate></xbrli:xbrl>')
            with self.subTest(change=change), self.assertRaises(RegistrantBindingError):
                check_fixture(manifest, discovered, payloads)

    def test_continued_inline_registrant_name_uses_verified_continuation_text(self):
        manifest, discovered, payloads = joint_fixture()
        payloads[HTML] = payloads[HTML].replace(
            b'name="dei:EntityRegistrantName" contextRef="affiliate">Affiliate Company</ix:nonNumeric>',
            b'name="dei:EntityRegistrantName" contextRef="affiliate" continuedAt="name-rest">Affiliate <ix:exclude>omit</ix:exclude></ix:nonNumeric>')
        payloads[HTML] = payloads[HTML].replace(b"</body>", b'<ix:continuation id="name-rest">Company</ix:continuation></body>')
        self.assertTrue(check_fixture(manifest, discovered, payloads))

    def test_legal_entity_axis_cannot_be_hidden_inside_an_unrelated_wrapper(self):
        manifest, discovered, payloads = joint_fixture()
        payloads[XML] = payloads[XML].replace(b"<xbrli:segment>", b"<unrelated><xbrli:segment>").replace(
            b"</xbrli:segment>", b"</xbrli:segment></unrelated>")
        with self.assertRaisesRegex(RegistrantBindingError, "outside context"):
            check_fixture(manifest, discovered, payloads)

    def test_corrupt_sources_and_paths_fail_before_binding(self):
        manifest, discovered, payloads = joint_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_fixture(root, manifest, payloads)
            source = root / manifest["files"][0]["local_path"]
            source.write_bytes(source.read_bytes() + b"changed")
            with self.assertRaisesRegex(RegistrantBindingError, "bytes changed"):
                verify_discovery_binding(manifest, discovered, root)
            manifest["files"][0]["local_path"] = "../outside.htm"
            with self.assertRaisesRegex(RegistrantBindingError, "paths"):
                verify_discovery_binding(manifest, discovered, root)

    @unittest.skipUnless(XOM.is_file(), "The optional live XOM archive has not been acquired.")
    def test_real_xom_joint_filing_binds_holding_company_without_rewriting_owner(self):
        manifest = json.loads(XOM.read_text(encoding="utf-8"))
        discovered = {key: manifest[key] for key in ("accession_number", "form", "filing_date", "reporting_period_end", "primary_document")}
        discovered.update(cik="0002115436", company="ExxonMobil Holdings Corp",
            filing_index_url="https://www.sec.gov/Archives/edgar/data/2115436/000003408826000093/0000034088-26-000093-index.html")
        index = (ROOT / manifest["index_document"]["local_path"]).read_bytes()
        canonical, associations = canonical_identity(index, discovered)
        self.assertEqual(canonical["cik"], "0000034088")
        self.assertEqual({record["cik"] for record in associations}, {"0000034088", "0002115436"})
        self.assertTrue(verify_discovery_binding(manifest, discovered, ROOT))
        self.assertEqual(manifest["cik"], "0000034088")


if __name__ == "__main__":
    unittest.main()
