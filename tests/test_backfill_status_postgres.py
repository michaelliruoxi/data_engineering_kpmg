"""Read-only SQL report contract in the explicitly local disposable database."""
import os
import psycopg
from psycopg.rows import dict_row
import pytest
from sec_pipeline.backfill_status import read_receipts, reconcile
from tests.test_catalog_postgres import local_test_options


@pytest.mark.skipif(os.environ.get('SEC_DB_TEST')!='1',reason='Dedicated local PostgreSQL checks are opt-in.')
def test_status_queries_execute_in_readonly_snapshot_without_mutation():
    with psycopg.connect(**local_test_options(),row_factory=dict_row,autocommit=True) as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            receipts,selections,observed=read_receipts(conn)
            result=reconcile({'filings':{}},receipts,selections,observed_at=observed)
            assert result['database_verified'] and result['observed_at_utc']==observed
            assert conn.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']=='on'
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                with conn.transaction():
                    conn.execute('UPDATE sec.companies SET name=name WHERE false')
