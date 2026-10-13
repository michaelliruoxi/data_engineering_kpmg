"""Opt-in PostgreSQL cell provenance tests; all fixture data rolls back.

Set SEC_DB_TEST=1 and migrate a dedicated SEC_DB_TEST_DATABASE ending in _test.
No existing data is deleted, and these tests never create databases or commit.
"""

import copy
from dataclasses import replace
from datetime import date, datetime, timezone
import os
from pathlib import Path
import re
import unittest

import psycopg
from psycopg.types.json import Jsonb

from sec_pipeline import database as db
from sec_pipeline.catalog import ProcessingSetInput, store_processing_set, select_preferred_processing_set
from sec_pipeline.table_cells import store_table_cells
from sec_pipeline.tables import FactScope, load_tables
from tests.test_table_cells import cell_fixture

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('SEC_DB_TEST') == '1', 'Set SEC_DB_TEST=1 for live database tests.')
class TableCellsPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = os.environ.get('SEC_DB_TEST_DATABASE', 'sec_filings_test')
        if not re.fullmatch(r'[A-Za-z0-9_]+_test(?:_[A-Za-z0-9_]+)?', cls.database):
            raise RuntimeError('Use an explicitly dedicated test database.')
        if cls.database == db.connection_settings(ROOT / '.env')['dbname']:
            raise RuntimeError('Tests must not target the configured application database.')
        with db.connect(ROOT / '.env', dbname=cls.database) as conn:
            if not db.status(conn)['ready']:
                raise RuntimeError('Migrate the dedicated test database first.')
            if conn.execute('SELECT count(*) FROM sec.filings').fetchone()[0]:
                raise RuntimeError('Dedicated test database must contain no filing data.')

    def setUp(self):
        self.conn = db.connect(ROOT / '.env', dbname=self.database)
        self.addCleanup(self.conn.close)
        tx = self.conn.transaction(force_rollback=True)
        tx.__enter__()
        self.addCleanup(tx.__exit__, None, None, None)

    def fixture(self, *, nil=False):
        company = db.register_company(self.conn, db.CompanyInput(cik='0000000001', name='Cell fixture'))
        filing = db.register_filing(self.conn, db.FilingInput(company_id=company,
            accession_number='0000000001-26-000001', form='10-Q', filing_date=date(2026, 4, 1)))
        sources = [db.register_source_document(self.conn, db.SourceDocumentInput(
            filing_id=filing, role=role, source_url=f'https://example.invalid/{name}', local_path=name,
            sha256=char * 64, size_bytes=100, retrieved_at=datetime(2026, 4, 2, tzinfo=timezone.utc)))
            for role, name, char in (('main_inline_xbrl_filing', 'fixture.htm', 'a'),
                                     ('extracted_xbrl_instance', 'fixture.xml', 'b'))]
        table, facts = cell_fixture(filing, sources[0], sources[1], nil=nil)
        mapping = {f.occurrence_key: db.store_fact(self.conn, f) for f in facts}
        scope = FactScope(filing, sources[1], facts[0].extraction_version, mapping)
        return table, facts, scope

    def load(self, *, nil=False):
        table, facts, scope = self.fixture(nil=nil)
        ids = load_tables(self.conn, [table], scope)
        self.conn.execute('SET CONSTRAINTS ALL IMMEDIATE')
        self.conn.execute('SET CONSTRAINTS ALL DEFERRED')
        return table, facts, scope, ids[table.table_key]

    def preferred(self, table, scope):
        processing = store_processing_set(self.conn, ProcessingSetInput(
            filing_id=table.filing_id, set_key='fixture-pass',
            fact_source_document_id=scope.xml_document_id, fact_extraction_version=scope.extraction_version,
            table_source_document_id=table.source_document_id, table_extraction_version=table.extraction_version,
            coverage={'facts': 'complete', 'tables': 'partial', 'reports': 'absent', 'chunks': 'absent'},
            validation_status='passed', validation={'checks': {'synthetic_fixture': True}}, configuration={}))
        select_preferred_processing_set(self.conn, table.filing_id, processing)
        return processing

    def test_loader_replay_preserves_cells_multiple_facts_and_one_row_per_cell(self):
        table, _, scope, table_id = self.load()
        before = self.conn.execute('SELECT cell_key,id FROM sec.table_cells ORDER BY cell_key').fetchall()
        self.assertEqual(len(before), 4)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM sec.cell_fact_links').fetchone()[0], 3)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM sec.financial_table_facts').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM sec.statement_cells').fetchone()[0], 0)
        processing = self.preferred(table, scope)
        rows = self.conn.execute('SELECT cell_key,fact_count,linked_facts,processing_set_id FROM sec.statement_cells ORDER BY row_index,column_index').fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual([r[1] for r in rows], [0, 0, 2, 1])
        self.assertEqual([f['numeric_value'] for f in rows[2][2]], ['100', '20'])
        self.assertTrue(all(r[3] == processing for r in rows))
        self.assertEqual(load_tables(self.conn, [table], scope), {'income': table_id})
        self.assertEqual(self.conn.execute('SELECT cell_key,id FROM sec.table_cells ORDER BY cell_key').fetchall(), before)

    def test_nil_is_preserved_and_not_presented_as_zero(self):
        table, _, scope, _ = self.load(nil=True)
        self.preferred(table, scope)
        facts = self.conn.execute("SELECT linked_facts FROM sec.statement_cells WHERE cell_key='r1c1'").fetchone()[0]
        self.assertEqual(facts[0]['numeric_value'], '100')
        self.assertIsNone(facts[1]['numeric_value'])
        self.assertIsNone(facts[1]['raw_value'])
        self.assertTrue(facts[1]['is_nil'])

    def test_direct_sql_cannot_rewrite_cell_source_or_linked_fact(self):
        _, _, scope, _ = self.load()
        for sql, args in (
            ("UPDATE sec.table_cells SET display_text='999' WHERE cell_key='r1c1'", ()),
            ("UPDATE sec.financial_facts SET numeric_value=999 WHERE id=%s", (scope.xml_id_to_fact_id['F1'],)),
            ("UPDATE sec.cell_fact_links SET evidence=jsonb_set(evidence,'{raw_value}','\"999\"')", ()),
            ("UPDATE sec.financial_tables SET relational_cells_version=NULL", ()),
            ("UPDATE sec.financial_tables SET readable_text='different source presentation'", ()),
        ):
            with self.subTest(sql=sql), self.assertRaises(psycopg.errors.CheckViolation):
                with self.conn.transaction():
                    self.conn.execute(sql, args)

    def test_incomplete_cells_or_membership_reject_at_transaction_boundary(self):
        self.load()
        for sql in ("DELETE FROM sec.table_cells WHERE cell_key='r0c0'",
                    "DELETE FROM sec.financial_table_facts"):
            with self.subTest(sql=sql), self.assertRaises(psycopg.errors.CheckViolation):
                with self.conn.transaction():
                    self.conn.execute(sql)
                    self.conn.execute('SET CONSTRAINTS ALL IMMEDIATE')

    def test_wrong_fact_source_or_version_never_satisfies_cell_evidence(self):
        table, facts, scope, table_id = self.load()
        for changed in (replace(facts[0], extraction_version='facts-other'),
                        replace(facts[0], source_document_id=table.source_document_id)):
            with self.conn.transaction(force_rollback=True):
                other = db.store_fact(self.conn, changed)
                link = self.conn.execute("SELECT cell_id,reference_order,link_method,evidence FROM sec.cell_fact_links WHERE fact_id=%s LIMIT 1",
                                         (scope.xml_id_to_fact_id['F1'],)).fetchone()
                with self.assertRaises(psycopg.errors.CheckViolation):
                    with self.conn.transaction():
                        self.conn.execute('''INSERT INTO sec.cell_fact_links(filing_id,table_id,cell_id,fact_id,reference_order,link_method,evidence)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                            (table.filing_id, table_id, link[0], other, link[1], link[2], Jsonb(link[3])))

    def test_processing_set_seal_blocks_cells_links_and_cascade_delete(self):
        table, _, scope, _ = self.load()
        self.preferred(table, scope)
        for sql in ('DELETE FROM sec.table_cells', 'DELETE FROM sec.cell_fact_links',
                    'DELETE FROM sec.financial_tables', 'DELETE FROM sec.financial_table_facts',
                    'TRUNCATE sec.table_cells CASCADE', 'TRUNCATE sec.cell_fact_links',
                    'TRUNCATE sec.financial_table_facts'):
            with self.subTest(sql=sql), self.assertRaises(psycopg.errors.CheckViolation):
                with self.conn.transaction():
                    self.conn.execute(sql)

    def test_unsealed_managed_cells_cannot_be_truncated_outside_row_checks(self):
        self.load()
        for sql in ('TRUNCATE sec.table_cells CASCADE', 'TRUNCATE sec.cell_fact_links',
                    'TRUNCATE sec.financial_table_facts'):
            with self.subTest(sql=sql), self.assertRaises(psycopg.errors.CheckViolation):
                with self.conn.transaction():
                    self.conn.execute(sql)

    def test_legacy_cleanup_deletes_cells_and_links_without_orphans(self):
        self.load()
        self.conn.execute('DELETE FROM sec.financial_table_facts')
        self.conn.execute('DELETE FROM sec.financial_tables')
        self.conn.execute('DELETE FROM sec.financial_facts')
        self.conn.execute('SET CONSTRAINTS ALL IMMEDIATE')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM sec.table_cells').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM sec.cell_fact_links').fetchone()[0], 0)

    def test_unresolved_cell_has_no_verified_links_even_with_candidates_in_evidence(self):
        table, _, scope = self.fixture()
        structure = copy.deepcopy(table.structure)
        cell = structure['rows'][1][1]
        cell['classification'], cell['unresolved_reason'] = 'unresolved', 'ambiguous candidates'
        candidates, cell['fact_references'] = cell['fact_references'], []
        cell['candidates'] = candidates
        table = replace(table, structure=structure)
        table_id = db.store_financial_table(self.conn, table)
        ids = store_table_cells(self.conn, table_id, table, scope.xml_id_to_fact_id)
        self.conn.execute('SET CONSTRAINTS ALL IMMEDIATE')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM sec.cell_fact_links WHERE cell_id=%s', (ids['r1c1'],)).fetchone()[0], 0)
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.conn.transaction():
                self.conn.execute('''INSERT INTO sec.cell_fact_links(filing_id,table_id,cell_id,fact_id,reference_order,link_method,evidence)
                    VALUES (%s,%s,%s,%s,0,'inline_id',%s)''',
                    (table.filing_id, table_id, ids['r1c1'], scope.xml_id_to_fact_id['F1'], Jsonb(candidates[0])))


if __name__ == '__main__':
    unittest.main()
