"""Parser fixes must not reuse old receipts or rewrite old same-version data."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from sec_pipeline import backfill, filing_adapter, processing
from sec_pipeline.parser_policy import policy_metadata, require_current_receipt
from sec_pipeline.processing import IngestionPlan, ParserRevisionConflict, PipelineStages
from tests.test_backfill import queue, configuration, Connection, selection_row
from tests.test_backfill_progress import successful_result
from tests.test_backfill_profiles import profiled_queue
from tests.test_backfill_text_fallback import fallback_queue, run as run_fallback_queue
from tests.test_filing_ingestion_v4 import scenario, ingest


@pytest.mark.parametrize('table_version', ['tables-v1', 'tables-v2', 'tables-v3', 'tables-v4'])
def test_every_active_profile_has_report_revision_and_correct_table_policy(table_version):
    versions = {**filing_adapter.VERSIONS, 'tables': table_version}
    revisions = policy_metadata(versions)['parser_revisions']
    assert revisions['reports'] == 'visible-table-rows-v2'
    assert revisions.get('tables') == (None if table_version == 'tables-v1' else 'logical-headers-v1')


@pytest.mark.parametrize('document_set', [False, True])
@pytest.mark.parametrize('stored', [{}, {'parser_revisions': {'reports': 'old'}}])
def test_stale_receipt_fails_before_any_write(document_set, stored):
    conn = SimpleNamespace(execute=Mock(return_value=SimpleNamespace(fetchone=lambda: (stored,))))
    with pytest.raises(ParserRevisionConflict, match='explicit new storage versions'):
        require_current_receipt(conn, UUID(int=1), 'existing', filing_adapter.VERSIONS, document_set=document_set)
    assert len(conn.execute.call_args_list) == 1
    assert conn.execute.call_args.args[0].startswith('SELECT configuration ')
    assert backfill._fatal_runtime_error(ParserRevisionConflict('old receipt')) is True


def test_new_and_matching_receipts_can_proceed():
    for stored in (None, (policy_metadata(filing_adapter.VERSIONS),)):
        conn = SimpleNamespace(execute=Mock(return_value=SimpleNamespace(fetchone=lambda: stored)))
        require_current_receipt(conn, UUID(int=1), 'existing', filing_adapter.VERSIONS)


def test_stale_dry_run_queue_is_reparsed_and_current_revision_can_be_reused(queue):
    for accession in queue.accessions:
        result = successful_result(queue, accession)
        result.pop('parser_revisions')
        queue.state['filings'][accession].update(status='validated', result=result)
    queue.dry_run.side_effect = lambda **kwargs: successful_result(queue, kwargs['manifest_path'].parent.name)
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, limit=1, emit=lambda _: None) == 1
    accession = queue.accessions[0]
    assert queue.state['filings'][accession]['result']['parser_revisions'] == policy_metadata(filing_adapter.VERSIONS)['parser_revisions']
    assert backfill.process_queue(queue.path, queue.state, None, queue.root, limit=1, emit=lambda _: None) == 1
    assert [call.kwargs['manifest_path'].parent.name for call in queue.dry_run.call_args_list] == queue.accessions[:2]


def test_stale_stored_receipt_is_not_a_successful_skip(queue):
    accession = queue.accessions[0]
    expected = configuration(queue.manifests[accession])
    receipt = selection_row(queue.manifests[accession])
    receipt[0].pop('parser_revisions')
    assert backfill._stored_set(Connection(receipt), accession, 'filing-import-v1', expected) is False


def test_revision_conflict_stops_backfill_without_text_fallback(fallback_queue):
    fallback_queue.ingest.side_effect = ParserRevisionConflict('Stored processing set uses an older parser revision.')
    with pytest.raises(ParserRevisionConflict, match='older parser revision'):
        run_fallback_queue(fallback_queue)
    assert fallback_queue.ingest.call_count == 1
    assert fallback_queue.text_plans == []
    assert fallback_queue.state['last_run']['stopped_reason'] == 'parser_revision_mismatch'


def test_legacy_ingestion_checks_revision_before_calling_fact_writer(tmp_path, monkeypatch):
    manifest = json.loads((Path(__file__).resolve().parents[1] / 'source_manifest.json').read_text())
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    facts = Mock(side_effect=AssertionError('No stage may write before a revision check'))
    stages = PipelineStages(facts=facts, tables=Mock(), report=Mock(), chunks=Mock(), validate=Mock())
    plan = IngestionPlan(stages=stages, required_checks=('source',))
    conn = SimpleNamespace(execute=Mock(return_value=SimpleNamespace(fetchone=lambda: ({},))))
    def transaction(_conn, **kwargs):
        context = SimpleNamespace(source_ids=SimpleNamespace(filing_id=UUID(int=1)))
        kwargs['stages'].facts(_conn, context)
    monkeypatch.setattr(processing, 'run_ingestion_transaction', transaction)
    with pytest.raises(ParserRevisionConflict):
        processing.run_ingestion(conn, manifest_path=path, data_root=tmp_path, versions=filing_adapter.VERSIONS,
            plan=plan, verify_inputs=lambda *_: manifest, processing_set_key='existing',
            coverage={'facts': 'complete', 'tables': 'partial', 'reports': 'unknown', 'chunks': 'complete'})
    facts.assert_not_called()


def test_v4_replay_rejects_old_revision_without_changing_existing_rows(scenario, monkeypatch):
    from sec_pipeline import filing_ingestion_v4 as runner
    ingest(scenario)
    scenario.conn.state['seal'][4].pop('parser_revisions')
    previous = deepcopy(scenario.conn.state)
    parse = Mock(side_effect=AssertionError('Old receipt must stop before parsing or writing'))
    monkeypatch.setattr(runner, 'parse_filing_set', parse)
    with pytest.raises(ParserRevisionConflict, match='older parser revision'):
        ingest(scenario)
    parse.assert_not_called()
    assert scenario.conn.state == previous
    assert scenario.conn.commits == scenario.conn.rollbacks == 1
