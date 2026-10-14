"""Pipeline ordering, bounded payloads, replay, and caller-owned rollback."""

from collections import deque
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import ipaddress
import os
from pathlib import Path
import re
import unittest
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from sec_pipeline import database as db
from sec_pipeline.bulk_storage import store_many
from sec_pipeline.table_cells import TableCellInput


FACT_KEYS = ('source_document_id', 'extraction_version', 'occurrence_key')


def fact(index=1):
    return db.FactInput(
        filing_id=UUID(int=1), source_document_id=UUID(int=2), extraction_version='bulk-test',
        occurrence_key=f'fact-{index}', concept_namespace='https://example.invalid/taxonomy',
        concept_name='Revenue', numeric_value=Decimal('12.5'), raw_value='12.5',
        period_kind='instant', instant_date=date(2026, 1, 1),
        unit={'numerator': ['USD'], 'denominator': []}, dimensions=[], source_reference={'element_id': f'fact-{index}'},
    )


class DeferredCursor:
    def __init__(self, connection, row):
        self.connection, self.row = connection, row
        self.fetched = False

    def fetchone(self):
        if self.connection.in_pipeline:
            raise AssertionError('Rows were fetched before the pipeline synchronized.')
        self.fetched = True
        if isinstance(self.row, Exception):
            raise self.row
        return self.row


class RecordingPipeline:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        if self.connection.in_pipeline:
            raise AssertionError('Unexpected nested pipeline.')
        self.connection.in_pipeline = True
        self.connection.groups.append([])
        return self

    def __exit__(self, kind, value, traceback):
        self.connection.in_pipeline = False
        if self.connection.sync_error is not None:
            raise self.connection.sync_error
        return False


class RecordingConnection:
    autocommit = False

    def __init__(self, rows=(), sync_error=None):
        self.rows = deque(rows)
        self.groups, self.cursors = [], []
        self.in_pipeline = False
        self.sync_error = sync_error

    def pipeline(self):
        return RecordingPipeline(self)

    def execute(self, query, parameters):
        if not self.in_pipeline:
            raise AssertionError('Bulk SQL was issued outside its pipeline.')
        self.groups[-1].append((query.as_string(), parameters))
        cursor = DeferredCursor(self, self.rows.popleft())
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        raise AssertionError('Storage took ownership of commit.')

    def rollback(self):
        raise AssertionError('Storage took ownership of rollback.')

    def transaction(self, *args, **kwargs):
        raise AssertionError('Storage took ownership of a transaction/savepoint.')


class BulkStorageTests(unittest.TestCase):
    def test_inserts_sync_before_fetch_and_preserve_record_order(self):
        ids = [UUID(int=10), UUID(int=11)]
        conn = RecordingConnection([(ident,) for ident in ids])
        self.assertEqual(store_many(conn, 'financial_facts', [fact(1), fact(2)], FACT_KEYS), ids)
        self.assertEqual([len(group) for group in conn.groups], [2])
        query, values = conn.groups[0][0]
        self.assertIn('DO NOTHING RETURNING "id"', query)
        self.assertNotIn('DO UPDATE', query)
        bound = dict(zip(asdict(fact()), values))
        self.assertEqual(bound['numeric_value'], Decimal('12.5'))
        self.assertIsNone(bound['label'])
        for name in ('unit', 'dimensions', 'source_reference'):
            self.assertIsInstance(bound[name], Jsonb)
            self.assertEqual(bound[name].obj, asdict(fact())[name])

    def test_mixed_replay_uses_indexable_keys_and_null_safe_payload_predicates(self):
        ids = [UUID(int=10), UUID(int=11), UUID(int=12)]
        conn = RecordingConnection([None, (ids[1],), None, (ids[0],), (ids[2],)])
        self.assertEqual(store_many(conn, 'financial_facts', [fact(1), fact(2), fact(3)], FACT_KEYS), ids)
        self.assertEqual([len(group) for group in conn.groups], [3, 2])
        for query, parameters in conn.groups[1]:
            for field in asdict(fact()):
                operator = '=' if field in FACT_KEYS else 'IS NOT DISTINCT FROM'
                self.assertIn(f'"{field}" {operator} %s', query)
            self.assertTrue(query.endswith(' FOR SHARE'))
            self.assertEqual(len(parameters), len(asdict(fact())))

    def test_identical_input_duplicates_reuse_ids_through_database_comparison(self):
        ident = UUID(int=20)
        conn = RecordingConnection([(ident,), None, (ident,)])
        self.assertEqual(store_many(conn, 'financial_facts', [fact(), fact()], FACT_KEYS), [ident, ident])
        self.assertEqual([len(group) for group in conn.groups], [2, 1])

    def test_changed_duplicate_raises_without_committing_prior_insert(self):
        conn = RecordingConnection([(UUID(int=20),), None, None])
        with self.assertRaises(db.DataConflict):
            store_many(conn, 'financial_facts', [fact(), replace(fact(), label='changed')], FACT_KEYS)
        self.assertEqual([len(group) for group in conn.groups], [2, 1])

    def test_changed_duplicate_across_batches_raises_and_stops_later_writes(self):
        conn = RecordingConnection([(UUID(int=20),), None, None])
        with self.assertRaises(db.DataConflict):
            store_many(conn, 'financial_facts', [fact(), replace(fact(), raw_value='13'), fact(3)],
                       FACT_KEYS, batch_size=1)
        self.assertEqual([len(group) for group in conn.groups], [1, 1, 1])

    def test_bounded_batches_accept_generators_without_copying_all_payloads(self):
        ids = [UUID(int=index + 100) for index in range(205)]
        conn = RecordingConnection([(ident,) for ident in ids])
        self.assertEqual(store_many(conn, 'financial_facts', (fact(i) for i in range(205)), FACT_KEYS), ids)
        self.assertEqual([len(group) for group in conn.groups], [100, 100, 5])

    def test_pipeline_and_fetch_errors_propagate_without_retry_or_recovery(self):
        for at_sync in (True, False):
            error = psycopg.OperationalError('deliberate transport failure')
            conn = RecordingConnection([(UUID(int=10),)] if at_sync else [error], sync_error=error if at_sync else None)
            with self.assertRaises(psycopg.OperationalError) as caught:
                store_many(conn, 'financial_facts', [fact(), fact(2)], FACT_KEYS, batch_size=1)
            self.assertIs(caught.exception, error)
            self.assertEqual(len(conn.groups), 1)
            if at_sync:
                self.assertFalse(conn.cursors[0].fetched)

    def test_all_inputs_are_validated_before_sql_including_finite_decimals(self):
        for invalid in (1.5, Decimal('NaN'), Decimal('Infinity')):
            conn = RecordingConnection()
            with self.assertRaises(db.DatabaseError):
                store_many(conn, 'financial_facts', [fact(), replace(fact(2), numeric_value=invalid)], FACT_KEYS, batch_size=1)
            self.assertEqual(conn.groups, [])
        for records in ([fact(), {'wrong': 'record'}], None, 'wrong'):
            conn = RecordingConnection()
            with self.assertRaises(db.DatabaseError):
                store_many(conn, 'financial_facts', records, FACT_KEYS)
            self.assertEqual(conn.groups, [])

    def test_invalid_configuration_and_autocommit_fail_before_sql(self):
        for kwargs in ({'batch_size': 0}, {'batch_size': 101}, {'batch_size': True}, {'returning': 'fact_id'}):
            conn = RecordingConnection()
            with self.assertRaises(db.DatabaseError):
                store_many(conn, 'financial_facts', [fact()], FACT_KEYS, **kwargs)
            self.assertEqual(conn.groups, [])
        for table, keys in [('financial_facts', ('occurrence_key',)), ('reports', FACT_KEYS), ('x;DROP TABLE sec.filings', FACT_KEYS)]:
            with self.assertRaises(db.DatabaseError):
                store_many(RecordingConnection(), table, [fact()], keys)
        conn = RecordingConnection()
        conn.autocommit = True
        with self.assertRaises(db.DatabaseError):
            store_many(conn, 'financial_facts', [], FACT_KEYS)

    def test_empty_input_performs_no_pipeline_or_sql(self):
        conn = RecordingConnection()
        self.assertEqual(store_many(conn, 'financial_facts', [], FACT_KEYS), [])
        self.assertEqual(conn.groups, [])

    def test_cell_json_columns_and_link_returning_column(self):
        cell = TableCellInput(UUID(int=1), UUID(int=2), 'cell-1', 0, 1, 1, 1, 'data', '12.5',
                              {'xpath': '/html/body/table/tr/td'}, 'resolved', None, 'Revenue', ['2026'],
                              {'instant': '2026-01-01'}, False)
        conn = RecordingConnection([(UUID(int=3),)])
        self.assertEqual(store_many(conn, 'table_cells', [cell], ('table_id', 'cell_key')), [UUID(int=3)])
        values = dict(zip(asdict(cell), conn.groups[0][0][1]))
        for name in ('source_locator', 'column_headings', 'column_period'):
            self.assertIsInstance(values[name], Jsonb)
        link = dict(filing_id=UUID(int=1), table_id=UUID(int=2), cell_id=UUID(int=3), fact_id=UUID(int=4),
                    reference_order=0, link_method='inline_id', evidence={'xml_id': 'fact-1'})
        conn = RecordingConnection([None, (UUID(int=4),)])
        self.assertEqual(store_many(conn, 'cell_fact_links', [link], ('cell_id', 'fact_id'), returning='fact_id'), [UUID(int=4)])
        self.assertIn('RETURNING "fact_id"', conn.groups[0][0][0])
        self.assertIn('SELECT "fact_id"', conn.groups[1][0][0])
        self.assertIsInstance(conn.groups[0][0][1][-1], Jsonb)
        with self.assertRaises(db.DatabaseError):
            store_many(RecordingConnection(), 'cell_fact_links', [link | {'unexpected': 1}],
                       ('cell_id', 'fact_id'), returning='fact_id')

    def test_table_memberships_replay_and_detect_filing_scope_conflicts(self):
        membership = {'filing_id': UUID(int=1), 'table_id': UUID(int=2), 'fact_id': UUID(int=3)}
        conn = RecordingConnection([None, (UUID(int=3),)])
        self.assertEqual(store_many(conn, 'financial_table_facts', [membership],
                                    ('table_id', 'fact_id'), returning='fact_id'), [UUID(int=3)])
        self.assertIn('"filing_id" IS NOT DISTINCT FROM %s', conn.groups[1][0][0])
        with self.assertRaises(db.DataConflict):
            store_many(RecordingConnection([None, None]), 'financial_table_facts',
                       [membership | {'filing_id': UUID(int=4)}], ('table_id', 'fact_id'), returning='fact_id')

    def test_chunks_preserve_supplied_section_id_and_json_metadata(self):
        chunk = db.ChunkInput(filing_id=UUID(int=1), report_id=UUID(int=2), chunking_version='bulk-test',
                             chunk_index=0, text='Revenue', offset_start=0, offset_end=7,
                             metadata={'source': 'section-1'}, section_id=UUID(int=3))
        conn = RecordingConnection([(UUID(int=4),)])
        self.assertEqual(store_many(conn, 'chunks', [chunk], ('report_id', 'chunking_version', 'chunk_index')), [UUID(int=4)])
        values = dict(zip(asdict(chunk), conn.groups[0][0][1]))
        self.assertEqual(values['section_id'], chunk.section_id)
        self.assertIsInstance(values['metadata'], Jsonb)
        self.assertEqual(values['metadata'].obj, chunk.metadata)


@unittest.skipUnless(os.environ.get('SEC_BULK_TEST') == '1', 'Set SEC_BULK_TEST=1 for dedicated local PostgreSQL tests.')
class BulkStoragePostgresTests(unittest.TestCase):
    """Optional real pipeline checks; local dedicated database, forced rollback."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        database = os.environ.get('SEC_BULK_TEST_DATABASE', 'sec_filings_bulk_test')
        if not re.fullmatch(r'[A-Za-z0-9_]+_test', database):
            raise RuntimeError('Bulk tests require a dedicated database ending in _test.')
        settings = db.connection_settings(Path(os.environ.get('SEC_BULK_TEST_ENV_FILE', root / '.env.local')))
        if settings['host'] not in {'localhost', '127.0.0.1', '::1'} or database == settings['dbname']:
            raise RuntimeError('Bulk tests require a separate database on the local loopback host.')
        cls.settings = settings | {'dbname': database, 'hostaddr': '127.0.0.1' if settings['host'] == 'localhost' else settings['host']}
        with psycopg.connect(**cls.settings) as conn:
            if not ipaddress.ip_address(conn.info.hostaddr).is_loopback or conn.info.dbname != database:
                raise RuntimeError('Connected bulk-test destination is not the requested local test database.')
            if not db.status(conn)['ready'] or conn.execute('SELECT count(*) FROM sec.filings').fetchone()[0]:
                raise RuntimeError('Migrate an empty dedicated test database before running bulk tests.')

    def setUp(self):
        self.conn = psycopg.connect(**self.settings)
        self.addCleanup(self.conn.close)
        transaction = self.conn.transaction(force_rollback=True)
        transaction.__enter__()
        self.addCleanup(transaction.__exit__, None, None, None)
        company = db.register_company(self.conn, db.CompanyInput(cik='0000000001', name='Bulk storage fixture'))
        filing = db.register_filing(self.conn, db.FilingInput(company_id=company, accession_number='0000000001-26-000001',
                                                           form='10-Q', filing_date=date(2026, 1, 2)))
        source = db.register_source_document(self.conn, db.SourceDocumentInput(filing_id=filing,
            role='extracted_xbrl_instance', source_url='https://example.invalid/bulk.xml', local_path='bulk.xml',
            sha256='a' * 64, size_bytes=1, retrieved_at=datetime(2026, 1, 3, tzinfo=timezone.utc)))
        self.records = [replace(fact(index), filing_id=filing, source_document_id=source) for index in range(3)]

    def test_real_pipeline_replay_and_late_conflict_rollback(self):
        ids = store_many(self.conn, 'financial_facts', self.records, FACT_KEYS, batch_size=2)
        before = self.conn.execute('SELECT id, raw_value, created_at FROM sec.financial_facts ORDER BY occurrence_key').fetchall()
        self.assertEqual(store_many(self.conn, 'financial_facts', self.records, FACT_KEYS, batch_size=2), ids)
        self.assertEqual(self.conn.execute('SELECT id, raw_value, created_at FROM sec.financial_facts ORDER BY occurrence_key').fetchall(), before)
        with self.assertRaises(db.DataConflict):
            with self.conn.transaction():
                store_many(self.conn, 'financial_facts', [replace(self.records[0], occurrence_key='new-before-conflict'),
                                                        replace(self.records[1], label='changed')], FACT_KEYS)
        self.assertEqual(self.conn.execute('SELECT id, raw_value, created_at FROM sec.financial_facts ORDER BY occurrence_key').fetchall(), before)

    def test_real_duplicates_use_postgres_equality_and_caller_savepoint_rollback(self):
        ident, duplicate = store_many(self.conn, 'financial_facts', [self.records[0], self.records[0]], FACT_KEYS)
        self.assertEqual(ident, duplicate)
        first = replace(self.records[1], source_reference={'flag': True})
        changed = replace(first, source_reference={'flag': 1})
        with self.assertRaises(db.DataConflict):
            with self.conn.transaction():
                store_many(self.conn, 'financial_facts', [first, changed], FACT_KEYS, batch_size=1)
        self.assertEqual(self.conn.execute('SELECT id FROM sec.financial_facts').fetchall(), [(ident,)])


if __name__ == '__main__':
    unittest.main()
