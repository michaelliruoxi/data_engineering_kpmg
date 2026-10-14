"""Combined filings can archive under a dimensional co-registrant's CIK."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from lxml import etree, html

from sec_pipeline.acquisition import SecClient, download_filing
from sec_pipeline.filing_sources import SourceVerificationError, registrant_name, verify_documents, verify_filing
from sec_pipeline.registrants import RegistrantBindingError, verify_document_binding, verify_discovery_binding
from tests.test_registrants import joint_fixture, refresh_hashes, INDEX, HTML, XML


ROOT = Path(__file__).resolve().parents[1]


def subsidiary_archive():
    manifest, discovered, payloads = joint_fixture()
    manifest.update(cik="0000000043", company="Affiliate Company")
    manifest["filing_index_url"] = manifest["filing_index_url"].replace("/data/42/", "/data/43/")
    for record in [manifest["index_document"], *manifest["files"]]:
        record["source_url"] = record["source_url"].replace("/data/42/", "/data/43/")
    payloads[INDEX] = payloads[INDEX].replace(b"/data/42/", b"/data/43/")
    discovered.update(cik="0000000042", company="Example Holdings plc",
                      filing_index_url=manifest["filing_index_url"].replace("/data/43/", "/data/42/"))
    refresh_hashes(manifest, payloads)
    return manifest, discovered, payloads


class CombinedRegistrantTests(unittest.TestCase):
    def test_archive_secondary_and_requested_primary_keep_original_identity_and_bytes(self):
        manifest, discovered, payloads = subsidiary_archive()
        before = deepcopy((manifest, discovered, payloads))
        self.assertEqual(verify_documents(manifest, payloads), manifest)
        self.assertTrue(verify_document_binding(manifest, discovered, payloads))
        self.assertEqual(registrant_name(payloads[XML], manifest["cik"], payloads=payloads, manifest=manifest),
                         "Affiliate Company")
        self.assertEqual((manifest, discovered, payloads), before)
        with self.assertRaises(SourceVerificationError):
            registrant_name(payloads[XML], manifest["cik"])

    def test_single_source_tampering_is_rejected(self):
        for role in (XML, HTML):
            for change in ("member", "name", "axis", "dimension_namespace", "entity", "missing_primary",
                           "missing_form", "missing_period", "wrong_form", "wrong_period", "duplicate_context", "duplicate_name"):
                with self.subTest(role=role, change=change):
                    manifest, discovered, payloads = subsidiary_archive()
                    data = payloads[role]
                    if change == "member":
                        data = data.replace(b"ex:AffiliateMember", b"ex:DifferentMember")
                    elif change == "name":
                        data = data.replace(b">Affiliate Company<", b">Different Company<")
                    elif change == "axis":
                        data = data.replace(b"dei:LegalEntityAxis", b"dei:OtherAxis")
                    elif change == "dimension_namespace":
                        data = data.replace(b"http://xbrl.org/2006/xbrldi", b"https://invalid.test/dimensions")
                    elif change == "entity":
                        data = data.replace(b">42</xbrli:identifier>", b">99</xbrli:identifier>")
                    else:
                        tree = etree.fromstring(data) if role == XML else html.document_fromstring(data)
                        key = "contextRef" if role == XML else "contextref"
                        primary = [node for node in tree.iter() if node.get(key) == "current"]
                        field = {"missing_primary": "EntityCentralIndexKey", "missing_form": "DocumentType",
                                 "missing_period": "DocumentPeriodEndDate", "wrong_form": "DocumentType",
                                 "wrong_period": "DocumentPeriodEndDate", "duplicate_name": "EntityRegistrantName"}.get(change)
                        if change == "duplicate_context":
                            context = tree.xpath('//*[@id="current"]')[0]
                            context.addnext(deepcopy(context))
                        else:
                            target = next(node for node in primary if str(node.tag).endswith("}" + field)
                                          or node.get("name", "").endswith(":" + field))
                            if change.startswith("missing"):
                                target.getparent().remove(target)
                            elif change == "duplicate_name":
                                duplicate = deepcopy(target); duplicate.text = "Other primary company"
                                target.addnext(duplicate)
                            else:
                                target.text = "10-K" if change == "wrong_form" else "2026-03-30"
                        data = etree.tostring(tree)
                    payloads[role] = data
                    refresh_hashes(manifest, payloads)
                    with self.assertRaises((SourceVerificationError, RegistrantBindingError)):
                        verify_document_binding(manifest, discovered, payloads)

    def test_index_must_explicitly_name_the_context_entity_and_every_dei_registrant(self):
        for name in (b"Example Holdings plc (Filer)", b"Affiliate Company (Filer)"):
            manifest, discovered, payloads = subsidiary_archive()
            payloads[INDEX] = payloads[INDEX].replace(name, name.replace(b"(Filer)", b"(Issuer)"))
            refresh_hashes(manifest, payloads)
            with self.subTest(name=name), self.assertRaises((SourceVerificationError, RegistrantBindingError)):
                verify_document_binding(manifest, discovered, payloads)

    def test_multiple_undimensioned_primaries_cannot_supply_inheritance(self):
        manifest, discovered, payloads = subsidiary_archive()
        for role in (XML, HTML):
            tree = etree.fromstring(payloads[role]) if role == XML else html.document_fromstring(payloads[role])
            context = tree.xpath('//*[@id="affiliate"]')[0]
            entity = context[0]
            entity[0].text = "43"
            entity.remove(entity[1])
            payloads[role] = etree.tostring(tree)
        refresh_hashes(manifest, payloads)
        with self.assertRaisesRegex((SourceVerificationError, RegistrantBindingError), "unique undimensioned"):
            verify_document_binding(manifest, discovered, payloads)

    def test_secondary_scoped_form_or_period_cannot_override_primary(self):
        for field, value in (("DocumentType", "10-K"), ("DocumentPeriodEndDate", "2026-03-30")):
            manifest, discovered, payloads = subsidiary_archive()
            fact = f'<dei:{field} contextRef="affiliate">{value}</dei:{field}>'.encode()
            payloads[XML] = payloads[XML].replace(b"</xbrli:xbrl>", fact + b"</xbrli:xbrl>")
            refresh_hashes(manifest, payloads)
            with self.subTest(field=field), self.assertRaises((SourceVerificationError, RegistrantBindingError)):
                verify_document_binding(manifest, discovered, payloads)

    def test_genuine_axis_with_extra_foreign_dimension_or_text_is_rejected(self):
        for role in (XML, HTML):
            for extra in (b'<fake:explicitMember xmlns:fake="https://invalid.test/dimensions" dimension="dei:OtherAxis">ex:OtherMember</fake:explicitMember>',
                          b'unsupported scope', b'<unexpected/>'):
                manifest, discovered, payloads = subsidiary_archive()
                payloads[role] = payloads[role].replace(b'</xbrli:segment>', extra + b'</xbrli:segment>')
                refresh_hashes(manifest, payloads)
                with self.subTest(role=role, extra=extra), self.assertRaisesRegex(
                        (SourceVerificationError, RegistrantBindingError), "unsupported content"):
                    verify_document_binding(manifest, discovered, payloads)

    def test_conflicting_ciks_or_members_cannot_share_a_registrant_context(self):
        for change in ("cik", "member"):
            manifest, discovered, payloads = subsidiary_archive()
            if change == "cik":
                extra = b'<dei:EntityCentralIndexKey contextRef="affiliate">42</dei:EntityCentralIndexKey>'
            else:
                tree = etree.fromstring(payloads[XML])
                context = deepcopy(tree.xpath('//*[@id="affiliate"]')[0])
                context.set("id", "other-affiliate")
                context.xpath('.//*[local-name()="explicitMember"]')[0].text = "ex:DifferentMember"
                extra = etree.tostring(context)
                extra += b'<dei:EntityCentralIndexKey contextRef="other-affiliate">43</dei:EntityCentralIndexKey>'
                extra += b'<dei:EntityRegistrantName contextRef="other-affiliate">Affiliate Company</dei:EntityRegistrantName>'
            payloads[XML] = payloads[XML].replace(b"</xbrli:xbrl>", extra + b"</xbrli:xbrl>")
            refresh_hashes(manifest, payloads)
            with self.subTest(change=change), self.assertRaises((SourceVerificationError, RegistrantBindingError)):
                verify_document_binding(manifest, discovered, payloads)

    def test_download_uses_actual_archive_receipt_then_reuses_unchanged_offline(self):
        manifest, discovered, payloads = subsidiary_archive()
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
            self.assertEqual((actual["cik"], actual["company"]), ("0000000043", "Affiliate Company"))
            self.assertTrue(verify_discovery_binding(actual, discovered, root))
            self.assertEqual(len(calls), 5)
            self.assertEqual(download_filing(client, discovered, root), path)
            self.assertEqual(len(calls), 5)
            self.assertEqual(path.read_bytes(), saved)
            xml_record = next(row for row in actual["files"] if row["role"] == XML)
            self.assertEqual((root / xml_record["local_path"]).read_bytes(), payloads[XML])

    def test_archived_combined_filings_bind_parent_without_rewriting_archive(self):
        cases = {"0000922224-26-000044": ("0000922224", "0000055387"),
                 "0001032208-26-000045": ("0001032208", "0000086521"),
                 "0001047862-26-000142": ("0001047862", "0000023632"),
                 "0001104659-26-093360": ("0001063761", "0001022344"),
                 "0001070750-26-000126": ("0001070750", "0001061937"),
                 "0001711269-26-000100": ("0001711269", "0000054476"),
                 "0001868275-26-000104": ("0001868275", "0001168165")}
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
                discovered = {key: manifest[key] for key in ("accession_number", "form", "filing_date",
                              "reporting_period_end", "primary_document")}
                discovered.update(cik=requested, filing_index_url=manifest["filing_index_url"].replace(
                    "/data/" + str(int(canonical)) + "/", "/data/" + str(int(requested)) + "/"))
                self.assertTrue(verify_discovery_binding(manifest, discovered, ROOT))
                self.assertEqual(path.read_bytes(), before)
        if not checked:
            self.skipTest("Archived combined filing sources are unavailable")


if __name__ == "__main__":
    unittest.main()
