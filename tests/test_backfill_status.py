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
