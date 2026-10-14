"""Reviewed issuer-history graphs, exact date scopes and immutable SEC receipts."""

from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from sec_pipeline.acquisition import AccessBlocked
from sec_pipeline.issuer_history import (
    IssuerHistoryError, archive_history_evidence, history_jobs, load_history,
)


ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / 'config/issuer_history.json'


def record(current=1, predecessor=2, effective='2024-01-01'):
    return {'current_cik': f'{current:010d}', 'predecessor_cik': f'{predecessor:010d}',
            'effective_date': effective, 'current_name': f'Successor {current}',
            'predecessor_name': f'Original issuer {predecessor}',
            'evidence_url': f'https://www.sec.gov/Archives/edgar/data/{current}/000119312526000001/registration.htm',
            'relationship': 'successor_issuer'}


def universe(*ciks):
    return {'as_of': '2026-10-13', 'companies': [{'cik': f'{cik:010d}'} for cik in ciks]}


class EvidenceClient:
    data = b'<html><body><h1>FORM 8-K12B</h1><p>Successor registration disclosure.</p></body></html>'

    def __init__(self, *, blocked_at=None):
        self.calls = []
        self.blocked_at = blocked_at

    def get(self, url):
        self.calls.append(url)
        if len(self.calls) == self.blocked_at:
            raise AccessBlocked('Fixture access block')
        return self.data, {'http_status': 200, 'retrieved_at_utc': '2026-10-13T16:15:00Z',
                           'content_type': 'text/html', 'size_bytes': len(self.data),
                           'sha256': hashlib.sha256(self.data).hexdigest(),
                           'private_transport_field': 'must-not-be-copied'}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / 'history.json'

    def load(self, records=None, members=None):
        self.path.write_text(json.dumps({'schema_version': 1, 'records': records if records is not None else [record()]}), encoding='utf-8')
        return load_history(self.path, members or universe(1))

    def test_seed_has_exactly_four_reviewed_successions_and_original_names(self):
        members = universe(2115436, 2012383, 1996862, 2011641)
        history = load_history(SEED, members)
        self.assertEqual({(r['current_cik'], r['predecessor_cik'], r['effective_date']) for r in history['records']}, {
            ('0002115436', '0000034088', '2026-07-01'), ('0002012383', '0001364742', '2024-10-01'),
            ('0001996862', '0001144519', '2023-11-01'), ('0002011641', '0001832433', '2024-08-01')})
        self.assertEqual(history['source_sha256'], hashlib.sha256(SEED.read_bytes()).hexdigest())
        names = {r['current_cik']: r['predecessor_name'] for r in history['records']}
        self.assertEqual(names['0002115436'], 'Exxon Mobil Corporation')
        self.assertEqual(names['0001996862'], 'Bunge Limited')
        self.assertNotIn('GIP', ' '.join(names.values()))
        self.assertEqual(json.loads(json.dumps(history)), history)

    def test_predecessor_jobs_keep_original_ciks_and_inclusive_effective_cutoffs(self):
        history = load_history(SEED, universe(2115436, 2012383, 1996862, 2011641))
        jobs = history_jobs(history, '2021-10-13', '2026-10-13')
        expected = {record['predecessor_cik']: record for record in history['records']}
        self.assertEqual(len(jobs), 4)
        self.assertEqual(len({job['job_key'] for job in jobs}), 4)
        for job in jobs:
            self.assertEqual(job['since'], '2021-10-13')
            self.assertEqual(job['until'], expected[job['cik']]['effective_date'])
            self.assertEqual(job['current_ciks'], [expected[job['cik']]['current_cik']])
            self.assertEqual(job['forms'], ['10-K', '10-Q'])
            self.assertEqual(job['history_source_sha256'], history['source_sha256'])
        self.assertEqual(len(history_jobs(history, '2026-07-01', '2026-10-13')), 1)
        self.assertEqual(history_jobs(history, '2026-07-01', '2026-10-13')[0]['until'], '2026-07-01')
        self.assertEqual(history_jobs(history, '2026-07-02', '2026-10-13'), [])
        self.assertTrue(all(job['until'] == '2022-01-01' for job in history_jobs(history, '2021-10-13', '2022-01-01')))

    def test_verified_chain_keeps_original_start_and_earlier_cutoffs(self):
        history = self.load([record(1, 2, '2025-01-01'), record(2, 3, '2023-01-01')])
        jobs = history_jobs(history, date(2021, 10, 13), date(2026, 10, 13))
        self.assertEqual([(job['cik'], job['since'], job['until'], job['current_ciks']) for job in jobs], [
            ('0000000002', '2021-10-13', '2025-01-01', ['0000000001']),
            ('0000000003', '2021-10-13', '2023-01-01', ['0000000001'])])
        self.assertEqual(len(jobs[1]['routes'][0]['records']), 2)

    def test_shared_predecessor_does_not_leak_associations_past_a_shorter_cutoff(self):
        history = self.load([record(1, 2, '2025-01-01'), record(4, 2, '2023-01-01')], universe(1, 4))
        jobs = history_jobs(history, '2021-01-01', '2026-01-01')
        self.assertEqual(len(jobs), 2)
        self.assertEqual({(job['until'], tuple(job['current_ciks'])) for job in jobs}, {
            ('2025-01-01', ('0000000001',)), ('2023-01-01', ('0000000004',))})
        history = self.load([record(1, 2), record(4, 2)], universe(1, 4))
        [job] = history_jobs(history, '2021-01-01', '2026-01-01')
        self.assertEqual(job['current_ciks'], ['0000000001', '0000000004'])
        self.assertEqual(len(job['routes']), 2)

    def test_duplicate_self_cycle_or_reversed_chronology_is_rejected(self):
        examples = [[record(), record()], [record(1, 1)],
                    [record(1, 2), record(2, 1)],
                    [record(1, 2, '2023-01-01'), record(2, 3, '2025-01-01')]]
        for records in examples:
            with self.subTest(records=records), self.assertRaises(IssuerHistoryError):
                self.load(records)

    def test_orphan_relationships_and_automatic_acquisition_or_spinoff_types_are_rejected(self):
        with self.assertRaises(IssuerHistoryError):
            self.load([record(4, 5)])
        for relationship in ('acquisition', 'spin_off_parent', 'subsidiary', 'merger'):
            with self.subTest(relationship=relationship), self.assertRaises(IssuerHistoryError):
                self.load([record() | {'relationship': relationship}])
        history = self.load([record()], universe(1, 10, 11))
        self.assertEqual({job['cik'] for job in history_jobs(history, '2021-01-01', '2026-01-01')}, {'0000000002'})

    def test_bad_calendar_noncanonical_cik_and_missing_or_extra_fields_fail(self):
        for change in ({'effective_date': '2025-02-29'}, {'effective_date': '2024-1-01'},
                       {'effective_date': '2027-01-01'}, {'current_cik': '1'}, {'predecessor_cik': '0000000000'},
                       {'predecessor_cik': 2}, {'predecessor_name': ''}, {'current_name': 'Name\nother'},
                       {'include_acquired_businesses': True}):
            with self.subTest(change=change), self.assertRaises(IssuerHistoryError):
                self.load([record() | change])
        incomplete = record()
        incomplete.pop('evidence_url')
        with self.assertRaises(IssuerHistoryError):
            self.load([incomplete])

    def test_only_mapped_issuer_direct_sec_archive_evidence_is_accepted(self):
        original = record()['evidence_url']
        bad = [original.replace('https:', 'http:'), original.replace('www.sec.gov', 'example.com'),
               original.replace('www.sec.gov', 'www.sec.gov.example.com'), original.replace('/data/1/', '/data/3/'),
               original.replace('/000119312526000001/', '/123/'), original + '?download=1', original + '#section',
               original.replace('www.sec.gov', 'user:password@www.sec.gov'), original.replace('registration.htm', '../registration.htm'),
               original.replace('registration.htm', '%2e%2e.htm'), 'https://data.sec.gov/submissions/CIK0000000001.json']
        for url in bad:
            with self.subTest(url=url), self.assertRaises(IssuerHistoryError):
                self.load([record() | {'evidence_url': url}])
        self.load([record() | {'evidence_url': original.replace('/data/1/', '/data/2/')}])

    def test_hashes_bind_raw_configuration_and_normalized_meaning(self):
        history = self.load()
        self.path.write_text(json.dumps({'records': [record()], 'schema_version': 1}, indent=2), encoding='utf-8')
        changed_format = load_history(self.path, universe(1))
        self.assertNotEqual(history['source_sha256'], changed_format['source_sha256'])
        self.assertEqual(history['configuration_sha256'], changed_format['configuration_sha256'])
        history['records'][0]['predecessor_name'] = 'Changed after approval'
        with self.assertRaises(IssuerHistoryError):
            history_jobs(history, '2021-01-01', '2026-01-01')

    def test_duplicate_json_keys_and_reversed_job_window_fail(self):
        self.path.write_text('{"schema_version":1,"schema_version":1,"records":[]}', encoding='utf-8')
        with self.assertRaises(IssuerHistoryError):
            load_history(self.path, universe(1))
        history = self.load()
        with self.assertRaises(IssuerHistoryError):
            history_jobs(history, '2026-01-02', '2026-01-01')
        self.assertEqual(history_jobs(self.load([]), '2021-01-01', '2026-01-01'), [])

    def test_archive_receipts_preserve_original_configuration_and_reuse_without_http(self):
        history = load_history(SEED, universe(2115436, 2012383, 1996862, 2011641))
        before = deepcopy(history)
        client = EvidenceClient()
        archived = archive_history_evidence(client, history, self.directory)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(history, before)
        for enriched in archived:
            path = self.directory / enriched['evidence_local_path']
            self.assertEqual(path.read_bytes(), client.data)
            self.assertEqual(enriched['evidence_sha256'], hashlib.sha256(client.data).hexdigest())
            self.assertIn(enriched['current_cik'], path.parts)
            receipt_text = path.with_name('receipt.json').read_text(encoding='utf-8')
            self.assertNotIn('must-not-be-copied', receipt_text)
            self.assertEqual(json.loads(receipt_text)['evidence_url'], enriched['evidence_url'])
        self.assertEqual(archive_history_evidence(None, history, self.directory), archived)

    def test_tampered_evidence_or_receipt_is_not_replaced_or_redownloaded(self):
        for changed in ('bytes', 'url', 'timestamp'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory(dir=self.directory) as directory:
                root = Path(directory)
                history = self.load()
                [archived] = archive_history_evidence(EvidenceClient(), history, root)
                evidence = root / archived['evidence_local_path']
                receipt_path = evidence.with_name('receipt.json')
                if changed == 'bytes':
                    evidence.write_bytes(evidence.read_bytes() + b'<!-- changed -->')
                else:
                    receipt = json.loads(receipt_path.read_text())
                    receipt['evidence_url' if changed == 'url' else 'evidence_retrieved_at'] = (
                        record(3, 4)['evidence_url'] if changed == 'url' else '2026-10-13T16:15:00')
                    receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
                old_evidence, old_receipt = evidence.read_bytes(), receipt_path.read_bytes()
                client = EvidenceClient()
                with self.assertRaises(IssuerHistoryError):
                    archive_history_evidence(client, history, root)
                self.assertEqual(client.calls, [])
                self.assertEqual(evidence.read_bytes(), old_evidence)
                self.assertEqual(receipt_path.read_bytes(), old_receipt)

    def test_unreceipted_different_bytes_are_never_overwritten(self):
        history = self.load()
        target = self.directory / 'data/raw/issuer_history/0000000001/2024-01-01/registration.htm'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'<html>Form 8-K original bytes</html>')
        original = target.read_bytes()
        with self.assertRaises(ValueError):
            archive_history_evidence(EvidenceClient(), history, self.directory)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(target.with_name('receipt.json').exists())

    def test_access_block_leaves_completed_receipts_and_stops_other_requests(self):
        history = load_history(SEED, universe(2115436, 2012383, 1996862, 2011641))
        blocked = EvidenceClient(blocked_at=2)
        with self.assertRaises(AccessBlocked):
            archive_history_evidence(blocked, history, self.directory)
        self.assertEqual(len(blocked.calls), 2)
        self.assertEqual(len(list(self.directory.rglob('receipt.json'))), 1)
        resumed = EvidenceClient()
        self.assertEqual(len(archive_history_evidence(resumed, history, self.directory)), 4)
        self.assertEqual(len(resumed.calls), 3)

    def test_bad_registration_payload_or_transport_metadata_creates_no_receipt(self):
        for payload in (b' ', b'<html>Request rate threshold exceeded</html>', b'<html>Not a registration form</html>'):
            history, client = self.load(), EvidenceClient()
            client.data = payload
            with self.subTest(payload=payload), self.assertRaises(IssuerHistoryError):
                archive_history_evidence(client, history, self.directory)
            self.assertEqual(list(self.directory.rglob('receipt.json')), [])


if __name__ == '__main__':
    unittest.main()
