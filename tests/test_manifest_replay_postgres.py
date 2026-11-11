"""Re-acquired manifest provenance must not invalidate an unchanged sealed set."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil

import psycopg
import pytest

from sec_pipeline import backfill, document_sets, filing_ingestion_v4
from tests.test_catalog_postgres import disposable_catalog_database
from tests.test_corrected_profile import original_sample_set


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.skipif(
    os.environ.get('SEC_DB_TEST') != '1' or os.environ.get('SEC_DB_CONCURRENCY_TEST') != '1',
    reason='Committed manifest replay requires an explicitly local disposable database.',
)
@pytest.mark.parametrize('rebuild_core', [False, True], ids=['document-set-manifest', 'core-manifest'])
def test_rebuilt_manifests_preserve_processing_identity_and_ingested_queue(
        original_sample_set, tmp_path, monkeypatch, rebuild_core):
    original_manifest, original_root = original_sample_set
    original = json.loads(original_manifest.read_text())
    core = json.loads((original_root / original['core_manifest']['local_path']).read_text())
    root = tmp_path / 'reacquired'
    for record in [core['index_document'], *core['files']]:
        target = root / record['local_path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original_root / record['local_path'], target)
    accession = core['accession_number']
    core_path = root / 'data/raw/sec' / accession / 'source_manifest.json'
    core_path.write_text(json.dumps(core), encoding='utf-8')
    manifest_path = document_sets.download_document_set(None, core_path, root)
    primary = next(record for record in core['files'] if record['role'] == 'main_inline_xbrl_filing')
    filing = {key: core[key] for key in document_sets.IDENTITY_FIELDS}
    filing['primary_document'] = Path(primary['local_path']).name
    state = {'schema_version': 1, 'configuration': {'since': '2026-01-01', 'until': '2026-12-31'},
             'companies': {}, 'filings': {accession: {'filing': filing, 'status': 'pending', 'attempts': 0}}}
    queue_path = root / 'queue.json'
    source_hashes = {record['local_path']: _digest(root / record['local_path'])
                     for record in [core['index_document'], *core['files']]}

    with disposable_catalog_database() as options, psycopg.connect(**options) as conn:
        backfill.process_queue(queue_path, state, None, root, mode='ingest', connection=conn,
                               tables_version='tables-v4', emit=lambda event: None)
        entry = state['filings'][accession]
        assert entry['status'] == 'ingested', entry
        first = deepcopy(entry['result'])
        attempts = entry['attempts']
        with conn.transaction():
            before = conn.execute('SELECT id,configuration,counts FROM sec.source_processing_sets').fetchall()
        document_digest, core_digest = _digest(manifest_path), _digest(core_path)

        if rebuild_core:
            # Re-acquisition changes only receipt metadata, never source bytes.
            core['updated_at_utc'] = '2030-01-02T03:04:05Z'
            core_path.write_text(json.dumps(core), encoding='utf-8')
            assert _digest(core_path) != core_digest
        else:
            assert _digest(core_path) == core_digest

        class ReacquisitionClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

        monkeypatch.setattr(document_sets, 'datetime', ReacquisitionClock)
        manifest_path.unlink()
        assert document_sets.download_document_set(None, core_path, root) == manifest_path
        assert _digest(manifest_path) != document_digest
        regenerated = document_sets.verify_document_set(manifest_path, root)
        assert {record['local_path']: _digest(root / record['local_path'])
                for record in [regenerated['index_document'], *regenerated['files']]} == source_hashes

        backfill.process_queue(queue_path, state, None, root, mode='ingest', connection=conn,
                               tables_version='tables-v4', emit=lambda event: None)
        assert entry['status'] == 'ingested', entry
        assert entry['attempts'] == attempts
        assert entry['result']['mode'] == 'stored_receipt'
        assert entry['result']['processing_set_id'] == first['processing_set_id']
        assert entry['result']['configuration'] == first['configuration']
        assert state['last_run']['complete'] is True
        assert json.loads(queue_path.read_text())['filings'][accession]['status'] == 'ingested'

        # Explicit replay also reaches the seal without replacing first-receipt provenance.
        replay = filing_ingestion_v4.run_ingestion(conn, manifest_path=manifest_path, data_root=root,
                                                   processing_set_key='filing-import-v4')
        assert replay['processing_set_id'] == first['processing_set_id']
        assert replay['configuration'] == first['configuration']
        with conn.transaction():
            assert conn.execute('SELECT id,configuration,counts FROM sec.source_processing_sets').fetchall() == before
