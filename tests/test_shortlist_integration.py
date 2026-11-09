"""Integration boundaries for PR #22 on the current fact/parser baseline."""
import csv
import json

import pytest

from sec_pipeline.database import DatabaseError
from sec_pipeline import shortlist_table_validation as scope
from tests.test_shortlist_table_validation import ACCESSION, MISSING, scope_files


def empty_capture(tmp_path):
    capture = tmp_path / 'capture'
    capture.mkdir()
    (capture / 'shared-inventory.json').write_text(json.dumps({
        'filings': [], 'filing_processing_sets': [], 'source_processing_sets': [],
        'identity': {'queried_at': 'synthetic-unit-fixture-not-live'},
    }))
    return capture


def test_supplemental_manifest_cannot_expand_confirmed_date_scope(tmp_path):
    config, audit = scope_files(tmp_path)
    output = tmp_path / 'out'
    extra = [
        {'ticker': 'AAPL', 'cik': '0000320193', 'form': '10-K',
         'accession_number': '0000000001-20-000003', 'filing_date': '2020-12-31'},
        {'ticker': 'AAPL', 'cik': '0000320193', 'form': '10-Q',
         'accession_number': '0000000001-27-000004', 'filing_date': '2027-01-01'},
    ]
    assert scope.run_scoped_validation(config, audit, empty_capture(tmp_path), [], output,
                                      additional_inputs=[('older-and-future-manifest', extra)]) == 2
    with (output / 'validation_summary.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    assert {row['accession'] for row in rows} == {ACCESSION, MISSING}
    excluded = json.loads((output / 'candidate_manifest.json').read_text())['supplemental_excluded_outside_requested_scope']
    assert {row['accession'] for row in excluded} == {row['accession_number'] for row in extra}


def test_reused_output_is_rejected_before_existing_evidence_changes(tmp_path):
    config, audit = scope_files(tmp_path)
    output = tmp_path / 'out'
    output.mkdir()
    receipt = output / 'shared-inventory.json'
    original = b'{"prior_evidence": true}\n'
    receipt.write_bytes(original)
    with pytest.raises(DatabaseError, match='empty output directory'):
        scope.run_scoped_validation(config, audit, empty_capture(tmp_path), [], output)
    assert receipt.read_bytes() == original
    assert list(output.iterdir()) == [receipt]


def test_new_in_window_discovery_is_preserved_separately_from_old_snapshot(tmp_path):
    config, audit = scope_files(tmp_path)
    output = tmp_path / 'out'
    new_accession = '0000000001-26-000003'
    queue = {'companies': {'AAPL': {'status': 'discovered'}}, 'filings': {
        new_accession: {'filing': {'ticker': 'AAPL', 'cik': '0000320193',
            'accession_number': new_accession, 'form': '10-Q', 'filing_date': '2026-11-09'}}}}
    assert scope.run_scoped_validation(config, audit, empty_capture(tmp_path), [], output,
                                      additional_inputs=[('new-discovery-queue', queue)]) == 2
    with (output / 'validation_summary.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    assert {row['accession'] for row in rows} == {ACCESSION, MISSING, new_accession}
    new_row = next(row for row in rows if row['accession'] == new_accession)
    assert new_row['in_section_discovery_snapshot'] == 'False'
    status = json.loads((output / 'run_status.json').read_text())
    assert status['expected_not_in_shared_database'] == 2
    assert status['additional_candidates_not_in_shared_database'] == 1
    assert status['discovery_snapshot_count'] == 2


@pytest.mark.parametrize('filing_date', [None, '2026-02-30', '20261109'])
def test_unknown_or_invalid_supplemental_dates_fail_before_writing(tmp_path, filing_date):
    config, audit = scope_files(tmp_path)
    output = tmp_path / 'out'
    extra = [{'ticker': 'AAPL', 'accession_number': '0000000001-26-000003',
              'form': '10-Q', 'filing_date': filing_date}]
    with pytest.raises(DatabaseError, match='valid ISO filing date'):
        scope.run_scoped_validation(config, audit, empty_capture(tmp_path), [], output,
                                    additional_inputs=[('unknown-date', extra)])
    assert not output.exists()


def test_conflicting_supplemental_cik_is_not_silently_dropped(tmp_path):
    config, audit = scope_files(tmp_path)
    output = tmp_path / 'out'
    extra = [{'ticker': 'AAPL', 'cik': '0000789019',
              'accession_number': '0000000001-26-000003', 'form': '10-Q', 'filing_date': '2026-11-09'}]
    with pytest.raises(DatabaseError, match='confirmed filing identity'):
        scope.run_scoped_validation(config, audit, empty_capture(tmp_path), [], output,
                                    additional_inputs=[('conflicting-cik', extra)])
    assert not output.exists()
