"""A combined filing can be archived under a filer its XBRL never identifies.

United Rentals, Inc. files with United Rentals (North America), Inc., and the
SEC stores the report under the subsidiary, yet the XBRL states only the
parent's CIK. FirstEnergy Corp. files with Jersey Central Power & Light
Company, whose DEI gives a name and no CIK. The owner of such an archive has
no DEI identity to check; it is accepted only as the one index filer the XBRL
leaves out, under the name the SEC index gives it.
"""

from copy import deepcopy
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import httpx

from sec_pipeline.acquisition import SecClient, download_filing
from sec_pipeline.filing_sources import SourceVerificationError, registrant_name, verify_documents, verify_filing
from sec_pipeline.registrants import (
    RegistrantBindingError, index_registrants, unidentified_owner_name, verify_discovery_binding, verify_document_binding,
)
from tests.test_combined_registrants import subsidiary_archive
from tests.test_filing_sources import save_fixture
from tests.test_registrants import joint_fixture, refresh_hashes, INDEX, HTML, XML


ROOT = Path(__file__).resolve().parents[1]
REFUSED = (SourceVerificationError, RegistrantBindingError)
CIK_FACT = {XML: b'<dei:EntityCentralIndexKey contextRef="affiliate">0000000043</dei:EntityCentralIndexKey>',
            HTML: b'<ix:nonNumeric name="dei:EntityCentralIndexKey" contextRef="affiliate">43</ix:nonNumeric>'}
NAME_FACT = {XML: b'<dei:EntityRegistrantName contextRef="affiliate">Affiliate Company</dei:EntityRegistrantName>',
             HTML: b'<ix:nonNumeric name="dei:EntityRegistrantName" contextRef="affiliate">Affiliate Company</ix:nonNumeric>'}
PRIMARY_CIK = {XML: b'<dei:EntityCentralIndexKey contextRef="current">0000000042</dei:EntityCentralIndexKey>',
               HTML: b'<ix:nonNumeric name="dei:EntityCentralIndexKey" contextRef="current">42</ix:nonNumeric>'}
PRIMARY_PERIOD = {XML: b'<dei:DocumentPeriodEndDate contextRef="current">2026-03-31</dei:DocumentPeriodEndDate>',
                  HTML: b'<ix:nonNumeric name="dei:DocumentPeriodEndDate" contextRef="current">2026-03-31</ix:nonNumeric>'}
OWNER_CONTEXT = (b'<xbrli:context id="owner"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">43'
                 b'</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant>'
                 b'</xbrli:period></xbrli:context>')
THIRD_FILER = (b'<div class="companyInfo"><span class="companyName">Third Company (Filer) <acronym>CIK</acronym>: '
               b'<a href="/cgi-bin/browse-edgar?CIK=0000000044&amp;action=getcompany">0000000044 (see all company filings)</a>'
               b'</span></div></body>')


def swap(payloads, role, piece, replacement=b""):
    if piece not in payloads[role]:
        raise AssertionError(f"The fixture no longer contains {piece!r}")
    payloads[role] = payloads[role].replace(piece, replacement)


def unidentified_owner(*, keep_name=False):
    """The subsidiary archive with every DEI CIK of the owner removed.

    The index names the owner "AFFILIATE CO". With `keep_name` the documents
    still give its name under its legal-entity member, as FirstEnergy's do;
    without it they say nothing about the owner, as United Rentals' do.
    """
    manifest, discovered, payloads = subsidiary_archive()
    swap(payloads, INDEX, b"Affiliate Company (Filer)", b"AFFILIATE CO (Filer)")
    manifest["company"] = "AFFILIATE CO"
    for role in (XML, HTML):
        swap(payloads, role, CIK_FACT[role])
        if not keep_name:
            swap(payloads, role, NAME_FACT[role])
            payloads[role], removed = re.subn(rb'<xbrli:context id="affiliate">.*?</xbrli:context>', b"", payloads[role], flags=re.S)
            if removed != 1:
                raise AssertionError("The fixture no longer declares the affiliate context once.")
    refresh_hashes(manifest, payloads)
    return manifest, discovered, payloads


class UnidentifiedArchiveOwnerTests(unittest.TestCase):
    def test_an_owner_the_xbrl_leaves_out_is_archived_under_its_index_name(self):
        for keep_name in (False, True):
            with self.subTest(keep_name=keep_name):
                manifest, discovered, payloads = unidentified_owner(keep_name=keep_name)
                before = deepcopy((manifest, discovered, payloads))
                self.assertEqual(unidentified_owner_name(payloads, manifest), "AFFILIATE CO")
                self.assertEqual(verify_documents(manifest, payloads), manifest)
                self.assertTrue(verify_document_binding(manifest, discovered, payloads))
                self.assertEqual(registrant_name(payloads[XML], manifest["cik"], payloads=payloads, manifest=manifest),
                                 "AFFILIATE CO")
                self.assertEqual(registrant_name(payloads[XML], discovered["cik"], payloads=payloads, manifest=manifest),
                                 "Example Holdings plc")
                self.assertEqual((manifest, discovered, payloads), before)
                # Without the index and the inline document the XML alone proves nothing about the owner.
                with self.assertRaises(SourceVerificationError):
                    registrant_name(payloads[XML], manifest["cik"])

    def test_the_owner_is_listed_with_the_parent_as_an_index_registrant(self):
        manifest, _, payloads = unidentified_owner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_fixture(root, manifest, payloads)
            self.assertEqual(index_registrants(manifest, root), [
                {"cik": "0000000042", "name": "Example Holdings plc", "is_canonical": False},
                {"cik": "0000000043", "name": "AFFILIATE CO", "is_canonical": True},
            ])

    def test_the_manifest_must_carry_the_index_name(self):
        # The documents print "Affiliate Company" without a CIK; that name is not tied to the owner.
        for name in ("Affiliate Company", "Example Holdings plc", ""):
            manifest, discovered, payloads = unidentified_owner(keep_name=True)
            manifest["company"] = name
            with self.subTest(name=name), self.assertRaises(REFUSED):
                verify_document_binding(manifest, discovered, payloads)

    def test_a_requested_registrant_still_needs_its_own_dei_cik(self):
        manifest, discovered, payloads = joint_fixture()
        for role in (XML, HTML):
            swap(payloads, role, CIK_FACT[role])
        refresh_hashes(manifest, payloads)
        self.assertEqual(verify_documents(manifest, payloads), manifest)
        with self.assertRaisesRegex(RegistrantBindingError, "requested CIK is absent"):
            verify_document_binding(manifest, discovered, payloads)

    def test_anything_else_about_the_owner_or_the_parent_is_still_refused(self):
        changes = ("cik_in_xml_only", "cik_in_inline_only", "owner_is_a_context_entity", "second_filer_left_out",
                   "owner_is_not_a_filer", "parent_cik_missing", "parent_period_differs", "parent_period_missing")
        for change in changes:
            manifest, discovered, payloads = unidentified_owner(keep_name=True)
            if change == "cik_in_xml_only":
                swap(payloads, XML, b"</xbrli:xbrl>", CIK_FACT[XML] + b"</xbrli:xbrl>")
            elif change == "cik_in_inline_only":
                swap(payloads, HTML, b"</body>", CIK_FACT[HTML] + b"</body>")
            elif change == "owner_is_a_context_entity":
                swap(payloads, XML, b"</xbrli:xbrl>", OWNER_CONTEXT + b"</xbrli:xbrl>")
            elif change == "second_filer_left_out":
                swap(payloads, INDEX, b"</body>", THIRD_FILER)
            elif change == "owner_is_not_a_filer":
                swap(payloads, INDEX, b"AFFILIATE CO (Filer)", b"AFFILIATE CO (Issuer)")
            elif change == "parent_cik_missing":
                for role in (XML, HTML):
                    swap(payloads, role, PRIMARY_CIK[role])
            elif change == "parent_period_differs":
                for role in (XML, HTML):
                    swap(payloads, role, PRIMARY_PERIOD[role], PRIMARY_PERIOD[role].replace(b"2026-03-31", b"2026-03-30"))
            else:
                for role in (XML, HTML):
                    swap(payloads, role, PRIMARY_PERIOD[role])
            refresh_hashes(manifest, payloads)
            with self.subTest(change=change):
                with self.assertRaises(REFUSED):
                    verify_documents(manifest, payloads)
                with self.assertRaises(REFUSED):
                    verify_document_binding(manifest, discovered, payloads)
                with self.assertRaises(REFUSED):
                    registrant_name(payloads[XML], manifest["cik"], payloads=payloads, manifest=manifest)

    def test_an_identified_owner_keeps_its_established_verification(self):
        manifest, discovered, payloads = subsidiary_archive()
        with self.assertRaises(RegistrantBindingError):
            unidentified_owner_name(payloads, manifest)
        self.assertEqual(registrant_name(payloads[XML], manifest["cik"], payloads=payloads, manifest=manifest),
                         "Affiliate Company")
        # The index name cannot stand in for the name the XML ties to the owner's CIK.
        swap(payloads, INDEX, b"Affiliate Company (Filer)", b"AFFILIATE CO (Filer)")
        manifest["company"] = "AFFILIATE CO"
        refresh_hashes(manifest, payloads)
        with self.assertRaises(REFUSED):
            verify_document_binding(manifest, discovered, payloads)

    def test_download_archives_the_owner_under_its_index_name_and_reuses_it_offline(self):
        manifest, discovered, payloads = unidentified_owner(keep_name=True)
        calls = []
        urls = {record["source_url"]: payloads[record["role"]]
                for record in [manifest["index_document"], *manifest["files"]]}
        urls[discovered["filing_index_url"]] = payloads[INDEX]

        def respond(request):
            calls.append(str(request.url))
            return httpx.Response(200, content=urls[str(request.url)])

        with tempfile.TemporaryDirectory() as directory, patch("sec_pipeline.acquisition.time.sleep"), \
                SecClient("Test Team test@sec.test", transport=httpx.MockTransport(respond)) as client:
            root = Path(directory)
            path = download_filing(client, discovered, root)
            saved = path.read_bytes()
            actual = verify_filing(path, root)
            self.assertEqual((actual["cik"], actual["company"]), ("0000000043", "AFFILIATE CO"))
            self.assertTrue(verify_discovery_binding(actual, discovered, root))
            self.assertEqual(len(calls), 5)
            self.assertEqual(download_filing(client, discovered, root), path)
            self.assertEqual(len(calls), 5)
            self.assertEqual(path.read_bytes(), saved)

    def test_archived_filings_of_this_kind_bind_the_parent_without_rewriting_the_archive(self):
        cases = {"0001067701-24-000028": ("0001067701", "0001047166"),
                 "0001031296-25-000017": ("0001031296", "0000053456")}
        checked = 0
        for accession, (requested, canonical) in cases.items():
            with self.subTest(accession=accession):
                path = ROOT / "data/raw/sec" / accession / "source_manifest.json"
                if not path.is_file():
                    continue
                checked += 1
                before = path.read_bytes()
                manifest = verify_filing(path, ROOT)
                self.assertEqual(manifest["cik"], canonical)
                primary = next(record for record in manifest["files"] if record["role"] == HTML)
                discovered = {key: manifest[key] for key in ("accession_number", "form", "filing_date", "reporting_period_end")}
                discovered.update(cik=requested, primary_document=primary["source_url"].rsplit("/", 1)[-1],
                                  filing_index_url=manifest["filing_index_url"].replace(
                                      "/data/" + str(int(canonical)) + "/", "/data/" + str(int(requested)) + "/"))
                self.assertTrue(verify_discovery_binding(manifest, discovered, ROOT))
                self.assertEqual(path.read_bytes(), before)
        if not checked:
            self.skipTest("Archived filings with an unidentified owner are unavailable")


if __name__ == "__main__":
    unittest.main()
