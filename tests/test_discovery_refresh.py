"""A submissions refresh invalidates completion without losing filing work."""

from copy import deepcopy
import json

import pytest

from sec_pipeline import backfill
from tests.test_backfill import cli_queue, queue


def test_refresh_preserves_filing_results_and_makes_discovery_resumable(queue):
    state = queue.state
    state['companies'] = {'0000123456': {'status': 'discovered', 'filing_count': 3}}
    state['historical_issuers'] = {'job': {'status': 'failed', 'cik': '0000999999'}}
    state['last_run'] = {'complete': True, 'verified_successes': 3}
    before = deepcopy(state['filings'])
    backfill.refresh_discovery(queue.path, state)
    assert state['filings'] == before
    assert state['companies']['0000123456']['status'] == 'pending'
    assert state['historical_issuers']['job']['status'] == 'pending'
    assert state['last_run']['complete'] is False
    assert state['last_run']['invalidated_by'] == 'discovery_refresh'
    assert json.loads(queue.path.read_text()) == state


def test_new_filing_invalidates_a_previous_completion_proof(queue):
    state = queue.state
    state['last_run'] = {'complete': True}
    filing = deepcopy(state['filings'][queue.accessions[0]]['filing'])
    filing['accession_number'] = '0000123456-26-000004'
    backfill.remember_discovery(state, filing)
    assert state['last_run'] == {'complete': False, 'invalidated_by': 'new_filing'}


def test_refresh_flag_is_rejected_for_read_only_status(cli_queue):
    with pytest.raises(SystemExit) as error:
        cli_queue.cli.main(['--status', '--refresh-discovery', *cli_queue.arguments])
    assert error.value.code == 2
    cli_queue.forbidden.assert_not_called()
