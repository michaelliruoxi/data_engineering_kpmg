"""Complete primary inline membership is additive to immutable legacy archives."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from sec_pipeline.acquisition import AccessBlocked, SecClient
from sec_pipeline.document_sets import (
    DocumentSetError, MANIFEST_NAME, SUPPLEMENTAL_ROLE, discover_inline_documents,
    download_document_set, verify_document_set,
)
from tests.test_filing_sources import fixture, save_fixture, INDEX_ROLE, HTML_ROLE


IDENTITY = 'Data Test test@sec.test'


def set_fixture(*, excluded=False):
    core, payloads = fixture('10-Q')
    base = core['filing_index_url'].rsplit('/', 1)[0] + '/'
    row = lambda seq, kind, filename: (f'<tr><td>{seq}</td><td>{kind}</td><td><a href="/ix?doc={base}{filename}">{filename}</a>'
                                      f'<span>iXBRL</span></td><td>{kind}</td></tr>')
    payloads[INDEX_ROLE] = (f'<html><body>CIK 42 Accession {core["accession_number"]} Form 10-Q '
        'Filing Date 2026-04-30 Period of Report 2026-03-31<table>' + row(1, '10-Q', 'report.htm')
        + row(7, 'EX-99.1', 'report_d2.htm')
        + (row(8, 'EX-FILING FEES', 'fees.htm') + row(9, 'EX-98', 'spac.htm') if excluded else '')
        + '</table><a href="report_htm.xml">Instance</a><a href="report.xsd">Schema</a></body></html>').encode()
    references = ('<ix:references><link:schemaRef xmlns:link="http://www.xbrl.org/2003/linkbase" '
                  'xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="report.xsd"/></ix:references>').encode()
    payloads[HTML_ROLE] = payloads[HTML_ROLE].replace(b'<html ', b'<html xmlns="http://www.w3.org/1999/xhtml" ').replace(
        b'<ix:header>', b'<ix:header>' + references)
    extra = b'''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:ex="https://example.test/taxonomy/2025"><head><title>Financial statements</title></head><body>
        <ix:nonNumeric id="additional" name="ex:DisclosureTextBlock" contextRef="current">Original supplemental text.</ix:nonNumeric>
        </body></html>'''
    for record in [core['index_document'], *core['files']]:
        data = payloads[record['role']]
        record.update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    return core, payloads, base + 'report_d2.htm', extra


def client_for(url, data, calls, status=200):
    def respond(request):
        calls.append(str(request.url))
        if str(request.url) != url:
            raise AssertionError('Unexpected network request: ' + str(request.url))
        return httpx.Response(status, content=data, headers={'Content-Type': 'text/html'})
    return SecClient(IDENTITY, transport=httpx.MockTransport(respond))


class DocumentSetTests(unittest.TestCase):
    def test_index_inventory_keeps_primary_order_and_explicit_separate_targets(self):
        core, payloads, url, _ = set_fixture(excluded=True)
        members, excluded = discover_inline_documents(payloads[INDEX_ROLE], core)
        self.assertEqual([record['sequence'] for record in members], [1, 7])
        self.assertEqual(members[1]['source_url'], url)
        self.assertEqual([record['document_type'] for record in excluded], ['EX-FILING FEES', 'EX-98'])
        self.assertTrue(all(record['index_locator']['source_sha256'] == core['index_document']['sha256'] for record in members + excluded))

    def test_download_is_additive_and_replay_has_no_network_or_receipt_changes(self):
        core, payloads, url, extra = set_fixture(excluded=True)
        calls = []
        with tempfile.TemporaryDirectory() as directory, client_for(url, extra, calls) as client:
            root = Path(directory)
            core_path = save_fixture(root, core, payloads)
            originals = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
            path = download_document_set(client, core_path, root)
            self.assertEqual(path.name, MANIFEST_NAME)
            result = verify_document_set(path, root)
            self.assertEqual(result['schema_version'], 2)
            self.assertEqual(len(result['files']), 4)
            self.assertEqual(result['document_set']['inline_target'], '')
            self.assertEqual(result['document_set']['member_source_urls'], [next(row['source_url'] for row in core['files'] if row['role'] == HTML_ROLE), url])
            saved = path.read_bytes()
            self.assertTrue(all(path.read_bytes() == data for path, data in originals.items()))
            self.assertEqual(download_document_set(client, core_path, root), path)
            self.assertEqual(path.read_bytes(), saved)
            self.assertEqual(calls, [url])
            self.assertNotIn(IDENTITY, saved.decode())
            self.assertEqual([row['role'] for row in result['files']].count(SUPPLEMENTAL_ROLE), 1)

    def test_conflicting_index_rows_primary_type_hosts_and_sequences_fail(self):
        for change in ('duplicate', 'type', 'host', 'sequence', 'primary_marker'):
            core, payloads, _, _ = set_fixture()
            data = payloads[INDEX_ROLE]
            if change == 'duplicate':
                start, end = data.index(b'<tr>'), data.index(b'</tr>') + len(b'</tr>')
                data = data.replace(b'</table>', data[start:end] + b'</table>')
            elif change == 'type':
                data = data.replace(b'<td>10-Q</td></tr>', b'<td>10-K</td></tr>')
            elif change == 'host':
                data = data.replace(b'https://www.sec.gov/Archives', b'https://invalid.test/Archives')
            elif change == 'sequence':
                data = data.replace(b'<td>7</td>', b'<td>1</td>')
            else:
                data = data.replace(b'/ix?doc=', b'').replace(b'<span>iXBRL</span>', b'')
            with self.subTest(change=change), self.assertRaises(ValueError):
                discover_inline_documents(data, core)

    def test_an_unmarked_filename_containing_ixbrl_is_not_an_inline_marker(self):
        core, payloads, _, _ = set_fixture()
        unmarked = b'<tr><td>8</td><td>EX-99.2</td><td><a href="ordinary-ixbrl.htm">ordinary-ixbrl.htm</a></td><td>EX-99.2</td></tr>'
        data = payloads[INDEX_ROLE].replace(b'</table>', unmarked + b'</table>')
        members, _ = discover_inline_documents(data, core)
        self.assertEqual(len(members), 2)

    def test_invalid_or_alternate_target_never_publishes_a_manifest(self):
        for change in ('target', 'empty_target', 'xml_base', 'tuple', 'fraction', 'namespace', 'schema', 'dtd', 'unknown_inline'):
            core, payloads, url, extra = set_fixture()
            if change in {'target', 'empty_target'}:
                extra = extra.replace(b'<ix:nonNumeric ', b'<ix:nonNumeric target="' + (b'other' if change == 'target' else b'') + b'" ')
            elif change == 'xml_base':
                extra = extra.replace(b'<html ', b'<html xml:base="https://invalid.test/" ')
            elif change in {'tuple', 'fraction'}:
                extra = extra.replace(b'ix:nonNumeric', ('ix:' + change).encode())
            elif change == 'namespace':
                extra = extra.replace(b'http://www.xbrl.org/2013/inlineXBRL', b'https://invalid.test/inline')
            elif change == 'schema':
                extra = extra.replace(b'<body>', b'<body><ix:references><link:schemaRef xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="different.xsd"/></ix:references>')
            elif change == 'unknown_inline':
                extra = extra.replace(b'ix:nonNumeric', b'ix:notAnInlineElement')
            else:
                extra = b'<!DOCTYPE html>' + extra
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, client_for(url, extra, []) as client:
                root = Path(directory); core_path = save_fixture(root, core, payloads)
                with self.assertRaises(DocumentSetError):
                    download_document_set(client, core_path, root)
                self.assertFalse(core_path.with_name(MANIFEST_NAME).exists())
                self.assertFalse((root / Path(core['files'][0]['local_path']).parent / 'report_d2.htm').exists())

    def test_saved_membership_target_receipts_paths_and_core_hash_are_revalidated(self):
        for change in ('omitted', 'sequence', 'locator', 'target', 'receipt', 'source_bytes', 'core_bytes', 'path', 'check_type', 'primary_type'):
            core, payloads, url, extra = set_fixture()
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, client_for(url, extra, []) as client:
                root = Path(directory); core_path = save_fixture(root, core, payloads)
                path = download_document_set(client, core_path, root)
                manifest = json.loads(path.read_text())
                record = next(row for row in manifest['files'] if row['role'] == SUPPLEMENTAL_ROLE)
                if change == 'omitted':
                    manifest['files'].remove(record); manifest['document_set']['member_source_urls'].remove(url)
                elif change == 'sequence':
                    record['sequence'] = 8
                elif change == 'locator':
                    record['index_locator']['source_xpath'] = '/html/body/other'
                elif change == 'target':
                    record['target_membership']['inline_target'] = 'other'
                elif change == 'receipt':
                    record['sha256'] = '0' * 64
                elif change == 'source_bytes':
                    (root / record['local_path']).write_bytes(extra + b'changed')
                elif change == 'core_bytes':
                    core_path.write_bytes(core_path.read_bytes() + b' ')
                elif change == 'check_type':
                    manifest['document_set']['source_checks']['single_primary_target'] = 1
                elif change == 'primary_type':
                    next(row for row in manifest['files'] if row['role'] == HTML_ROLE)['is_primary'] = 1
                else:
                    record['local_path'] = '../outside.htm'
                path.write_text(json.dumps(manifest))
                with self.assertRaises(DocumentSetError):
                    verify_document_set(path, root)

    def test_changed_unmanifested_source_or_access_block_is_not_overwritten(self):
        core, payloads, url, extra = set_fixture()
        for status in (200, 403):
            calls = []
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory, client_for(url, extra, calls, status) as client:
                root = Path(directory); core_path = save_fixture(root, core, payloads)
                source = (root / core['files'][0]['local_path']).parent / 'report_d2.htm'
                source.write_bytes(b'existing source')
                with self.assertRaises((DocumentSetError, AccessBlocked)):
                    download_document_set(client, core_path, root)
                self.assertEqual(source.read_bytes(), b'existing source')
                self.assertFalse(core_path.with_name(MANIFEST_NAME).exists())

    def test_actual_archived_sets_verify_offline_with_original_core_receipts(self):
        project = Path(__file__).resolve().parents[1]
        checked = 0
        for accession, counts in (('0000021076-26-000034', [69, 2019]), ('0000831001-26-000045', [9410, 27])):
            path = project / 'data/raw/sec' / accession / MANIFEST_NAME
            if not path.is_file():
                continue
            checked += 1
            with self.subTest(accession=accession):
                before = path.read_bytes()
                manifest = verify_document_set(path, project)
                members = [record for record in manifest['files'] if record['role'] in {HTML_ROLE, SUPPLEMENTAL_ROLE}]
                self.assertEqual([record['target_membership']['fact_elements'] for record in members], counts)
                with patch('sec_pipeline.acquisition.httpx.Client.stream', side_effect=AssertionError('Offline replay cannot request sources')), SecClient(IDENTITY) as client:
                    self.assertEqual(download_document_set(client, project / manifest['core_manifest']['local_path'], project), path)
                self.assertEqual(path.read_bytes(), before)
        if not checked:
            self.skipTest('Archived document sets are unavailable')


if __name__ == '__main__':
    unittest.main()
