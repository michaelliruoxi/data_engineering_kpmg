from copy import deepcopy
import pytest
from sec_pipeline.backfill_status import reconcile, receipt_status


def receipt(accession='a', identifier='v1'):
    return dict(accession_number=accession, processing_family='legacy', processing_set_id=identifier,
                validation_status='passed', coverage=dict.fromkeys(('facts','tables','reports','chunks'),'complete'),
                counts=dict.fromkeys(('facts','tables','reports','chunks'), 1), versions={'tables':'tables-v3'})


def test_distinct_filings_not_versions_and_queue_not_receipts():
    a, b = receipt(), receipt(identifier='v2')
    result = reconcile({'filings':{'a':{'status':'pending'},'b':{'status':'ingested'}}},
                       [a,b], [a], observed_at='2026-10-29T00:00:00Z')
    assert result['queue_filings'] == 2
    assert result['database_filings_with_receipts'] == 1
    assert result['counts'] == {'fully_imported':1,'pending':1}
    assert result['filings'][0]['selected_receipt'] == a
    assert result['filings'][1]['issues'] == ['queue_claims_import_but_no_database_receipt']
    assert result['observed_at_utc'] == '2026-10-29T00:00:00Z'


def test_offline_does_not_report_missing_receipts_as_pending_or_success():
    result = reconcile({'filings':{'a':{'status':'ingested'}}})
    assert result['database_filings_with_receipts'] is None
    assert result['counts'] == {'unverified':1}


def test_text_review_failed_pending_are_separate():
    r = receipt(); r['coverage']['tables']='absent'; r['counts']['tables']=0
    assert receipt_status(r) == 'text_only'
    r['validation_status']='review_required'
    assert receipt_status(r) == 'review_required'
    r = receipt(); r['coverage']['reports']='partial'
    assert receipt_status(r) == 'review_required'
    report=reconcile({'filings':{'a':{'status':'failed'},'b':{'status':'pending'}}}, [])
    assert report['counts'] == {'failed':1,'pending':1}


def test_no_arbitrary_newest_selection_and_cross_filing_override_rejected():
    r=receipt(); queue={'filings':{'a':{'status':'ingested'}}}
    assert reconcile(queue,[r])['counts'] == {'review_required':1}
    with pytest.raises(ValueError):
        reconcile(queue,[r],overrides={'a':('legacy','other')})
    with pytest.raises(ValueError):
        reconcile(queue,[r],overrides={'missing':('legacy','v1')})
    assert reconcile(queue,[r],overrides={'a':('legacy','v1')})['counts'] == {'fully_imported':1}


def test_inputs_preserved_and_receipts_missing_queue_reported():
    r=receipt(); original=deepcopy(r)
    result=reconcile({'filings':{}},[r],[r])
    assert result['filings'][0]['issues'] == ['database_receipt_not_in_queue']
    assert r == original


@pytest.mark.parametrize('validation_status',['passed','failed','unvalidated'])
def test_unselected_receipts_missing_queue_still_report_discrepancy(validation_status):
    r=receipt(); r['validation_status']=validation_status
    result=reconcile({'filings':{}},[r],[])
    row=result['filings'][0]
    assert row['status']=='review_required'
    assert row['selected_receipt'] is None
    assert row['issues']==['stored_candidates_without_unique_selection',
                          'database_receipt_not_in_queue']


def test_present_queue_entry_without_status_is_not_reported_as_missing():
    r=receipt()
    result=reconcile({'filings':{'a':{}}},[r],[r])
    assert result['filings'][0]['issues']==[]


def test_invalid_counts_never_accepted():
    for invalid in [True, 0, -1, '1', None]:
        r=receipt(); r['counts']['facts']=invalid
        assert receipt_status(r) == 'review_required'


def test_database_reader_rejects_write_transaction_before_reading_receipts():
    from sec_pipeline.backfill_status import read_receipts
    class Connection:
        def execute(self, query):
            assert query == 'SHOW transaction_read_only'
            return self
        def fetchone(self):
            return {'transaction_read_only':'off'}
    with pytest.raises(ValueError, match='Read-only'):
        read_receipts(Connection())


@pytest.mark.parametrize('field',['facts','reports','chunks'])
@pytest.mark.parametrize('coverage',['partial','unknown','absent'])
def test_partial_text_receipt_remains_review_required(field,coverage):
    r=receipt(); r['coverage']['tables']='absent'; r['counts']['tables']=0
    r['coverage'][field]=coverage
    assert receipt_status(r)=='review_required'


@pytest.mark.parametrize('queue_status', ['ingested', 'ingested_review_required', 'stale_receipt'])
def test_stale_queue_receipt_cannot_be_reported_as_fully_imported(queue_status):
    r = receipt()
    queue = {'filings': {'a': {'status': queue_status, 'receipt_status': 'stale_receipt',
                              'error_type': 'ParserRevisionConflict'}}}
    original = deepcopy(queue)
    result = reconcile(queue, [r], [r])
    assert result['counts'] == {'review_required': 1}
    row = result['filings'][0]
    assert row['status'] == 'review_required'
    assert row['queue_status'] == queue_status
    assert row['queue_receipt_status'] == 'stale_receipt'
    assert row['selected_receipt'] == r
    assert row['issues'] == ['stale_receipt_requires_versioned_upgrade']
    assert queue == original


def test_stale_queue_receipt_stays_unverified_without_database_access():
    queue = {'filings': {'a': {'status': 'ingested', 'receipt_status': 'stale_receipt'}}}
    result = reconcile(queue)
    assert result['counts'] == {'unverified': 1}
    assert result['filings'][0]['issues'] == ['stale_receipt_requires_versioned_upgrade']


def queued(cik, form, filing_date, status='pending'):
    return {'status': status, 'filing': {'cik': cik, 'company': 'Company ' + cik, 'form': form,
                                         'filing_date': filing_date}}


def summary_queue():
    return {'configuration': {'since': '2021-10-13', 'until': '2026-10-13', 'forms': ['10-K', '10-Q']},
            'filings': {'a': queued('1', '10-K', '2022-02-01', 'ingested'),
                        'b': queued('1', '10-Q', '2022-05-01', 'ingested'),
                        'c': queued('2', '10-Q', '2022-05-02', 'failed'),
                        'd': queued('2', '10-Q', '2023-05-02'),
                        'e': queued('3', '10-K', '2023-02-01')}}


def test_coverage_summary_counts_stored_and_missing_by_year_form_and_issuer():
    from sec_pipeline.backfill_status import coverage_summary
    queue = summary_queue()
    text = receipt('b'); text['coverage']['tables'] = 'absent'; text['counts']['tables'] = 0
    text['coverage'].update(dict.fromkeys(('facts', 'reports', 'chunks'), 'complete'))
    outside = receipt('z')
    receipts = [receipt('a'), text, outside]
    report = reconcile(queue, receipts, receipts, observed_at='2026-11-17T00:00:00Z')
    summary = coverage_summary(queue, report)
    assert summary['observed_at_utc'] == '2026-11-17T00:00:00Z'
    assert summary['scope'] == {'since': '2021-10-13', 'until': '2026-10-13', 'forms': ['10-K', '10-Q']}
    assert summary['filings'] == {'queued': 5, 'stored': 2, 'missing': 3, 'failed': 1,
                                  'fully_imported': 1, 'pending': 2, 'text_only': 1}
    assert summary['by_filing_year_and_form'] == [
        {'filing_year': 2022, 'form': '10-K', 'queued': 1, 'stored': 1, 'missing': 0, 'fully_imported': 1},
        {'filing_year': 2022, 'form': '10-Q', 'queued': 2, 'stored': 1, 'missing': 1, 'failed': 1, 'text_only': 1},
        {'filing_year': 2023, 'form': '10-K', 'queued': 1, 'stored': 0, 'missing': 1, 'pending': 1},
        {'filing_year': 2023, 'form': '10-Q', 'queued': 1, 'stored': 0, 'missing': 1, 'pending': 1}]
    assert summary['issuers'] == {'with_queued_filings': 3, 'all_stored': 1, 'partly_stored': 0, 'none_stored': 2}
    assert [(row['cik'], row['missing']) for row in summary['issuer_rows']] == [('2', 2), ('3', 1), ('1', 0)]
    assert summary['issuer_rows'][0]['company'] == 'Company 2'
    # The receipt outside the queue is reported on its own, never as coverage.
    assert summary['database_filings_outside_queue'] == 1


def test_coverage_summary_counts_a_review_required_receipt_as_stored():
    from sec_pipeline.backfill_status import coverage_summary
    queue = {'filings': {'a': queued('1', '10-K', '2022-02-01', 'ingested')}}
    flagged = receipt('a'); flagged['validation_status'] = 'review_required'
    summary = coverage_summary(queue, reconcile(queue, [flagged], [flagged]))
    assert summary['filings'] == {'queued': 1, 'stored': 1, 'missing': 0, 'review_required': 1}
    assert summary['issuers']['all_stored'] == 1
    assert summary['scope'] == {'since': None, 'until': None, 'forms': None}


def test_offline_coverage_summary_claims_neither_stored_nor_missing():
    from sec_pipeline.backfill_status import coverage_summary
    queue = summary_queue()
    summary = coverage_summary(queue, reconcile(queue))
    assert summary['database_verified'] is False
    assert summary['filings'] == {'queued': 5, 'stored': 0, 'missing': 0, 'unverified': 5}
    assert summary['issuers'] == {'with_queued_filings': 3, 'all_stored': None, 'partly_stored': None,
                                  'none_stored': None}
    assert summary['database_filings_outside_queue'] is None


def test_coverage_summary_rejects_a_report_made_for_another_queue():
    from sec_pipeline.backfill_status import coverage_summary
    queue = summary_queue()
    report = reconcile({'filings': {'a': queue['filings']['a']}}, [])
    with pytest.raises(ValueError, match='every queued accession'):
        coverage_summary(queue, report)
