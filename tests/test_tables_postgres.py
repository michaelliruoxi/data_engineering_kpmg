"""Real sample component acceptance on an explicitly selected local test DB.

Ordinary discovery skips this suite. Run scripts/run_tables_postgres_tests.py.
No mocks, substitute financial records, migrations, or network requests here.
"""
from dataclasses import replace
import hashlib
from pathlib import Path
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from sec_pipeline import database as db
from sec_pipeline.facts_bridge import write_facts, fact_ids_for_tables
from sec_pipeline.processing import PipelineContext, resolve_source_ids
from sec_pipeline.tables import FactScope, TableExtractionError, load_tables
from sec_pipeline.tables_bridge import parse_tables, write_tables

ROOT = Path(__file__).resolve().parents[1]
RELATIONS = ('companies', 'filings', 'source_documents', 'financial_facts',
             'financial_tables', 'financial_table_facts', 'reports', 'chunks')


class TablesPostgresTests(unittest.TestCase):
    connection_options = None

    @classmethod
    def connect(cls):
        return psycopg.connect(**cls.connection_options)

    @classmethod
    def snapshot(cls):
        with cls.connect() as conn:
            return {name: conn.execute(sql.SQL('SELECT * FROM sec.{} ORDER BY {}').format(
                sql.Identifier(name), sql.SQL('table_id, fact_id') if name == 'financial_table_facts'
                else sql.Identifier('id'))).fetchall() for name in RELATIONS}

    @classmethod
    def setUpClass(cls):
        if cls.connection_options is None:
            raise unittest.SkipTest('Run scripts/run_tables_postgres_tests.py against a dedicated local test DB.')
        cls.manifest = db._verified_manifest(ROOT / 'source_manifest.json', ROOT)
        paths = [ROOT / 'source_manifest.json', *(ROOT / r['local_path']
                 for r in [cls.manifest['index_document'], *cls.manifest['files']])]
        cls.hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        cls.guard = cls.connect()
        cls.addClassCleanup(cls.guard.close)
        if not cls.guard.execute('SELECT pg_try_advisory_lock(73654321456)').fetchone()[0]:
            raise RuntimeError('Another Financial Tables suite owns the test database.')
        if not db.status(cls.guard)['ready']:
            raise RuntimeError('Migrate only the dedicated test database before this suite.')
        cls.guard.rollback()
        cls.baseline = cls.snapshot()
        if any(cls.baseline.values()):
            raise RuntimeError('Dedicated test database is not empty; existing data will not be cleared.')
        cls.addClassCleanup(cls.verify_final_state)

    @classmethod
    def verify_final_state(cls):
        if cls.snapshot() != cls.baseline:
            raise AssertionError('Database did not return to its exact baseline.')
        if {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in cls.hashes} != cls.hashes:
            raise AssertionError('Source bytes changed.')

    def context(self, conn):
        seeded = db.seed_manifest(conn, ROOT / 'source_manifest.json', ROOT)
        return PipelineContext(self.manifest, ROOT, resolve_source_ids(self.manifest, seeded),
                               {'facts': 'facts-v1', 'tables': 'tables-v1'}, {})

    def load(self, conn):
        context = self.context(conn)
        context.outputs['facts'] = write_facts(conn, context)
        context.outputs['tables'] = write_tables(conn, context)
        return context

    def verify(self, conn, context):
        sources = context.source_ids
        facts = conn.execute('SELECT id, occurrence_key, filing_id, source_document_id, extraction_version '
                             'FROM sec.financial_facts').fetchall()
        self.assertEqual(len(facts), 1622)
        self.assertTrue(all(row[2:] == (sources.filing_id, sources.xml_document_id, 'facts-v1')
                            for row in facts))
        mapping = fact_ids_for_tables(context)
        expected_counts = {'income_statement': 76, 'balance_sheet': 76, 'cash_flow': 131}
        tables = conn.execute('SELECT id, filing_id, source_document_id, extraction_version, '
                              'table_key, structure FROM sec.financial_tables').fetchall()
        self.assertEqual(len(tables), 3)
        for ident, filing, source, version, key, structure in tables:
            self.assertEqual((filing, source, version),
                             (sources.filing_id, sources.html_document_id, 'tables-v1'))
            refs = {r['xml_id'] for row in structure['rows'] for cell in row
                    for r in cell['fact_references']}
            links = conn.execute('SELECT fact_id, filing_id FROM sec.financial_table_facts '
                                 'WHERE table_id = %s', (ident,)).fetchall()
            self.assertEqual({r[0] for r in links}, {mapping[k] for k in refs})
            self.assertTrue(all(r[1] == sources.filing_id for r in links))
            self.assertEqual(len(links), expected_counts[key])
            self.assertEqual(structure['inventory']['resolved'],
                             {'income_statement': 76, 'balance_sheet': 66, 'cash_flow': 132}[key])
            self.assertEqual(structure['inventory'].get('unresolved', 0), 0)
        self.assertEqual(conn.execute('SELECT count(*) FROM sec.financial_table_facts').fetchone()[0], 283)
        query_text = '\n'.join(line for line in (ROOT / 'docs/financial-tables-queries.sql').read_text().splitlines()
                               if not line.lstrip().startswith('--'))
        queries = query_text.split(';')
        results = [conn.execute(query).fetchall() for query in queries if 'SELECT' in query.upper()]
        self.assertEqual(len(results), 5)
        self.assertEqual(len(results[0]), 3)
        self.assertEqual(dict(results[1]), expected_counts)
        self.assertEqual(len(results[2]), 284)  # Ten heading refs and one repeated cash fact.
        self.assertEqual(results[3], [])
        self.assertEqual(results[4], [])

    def test_real_load_committed_replay_and_scoped_cleanup(self):
        context = None
        try:
            with self.connect() as conn:
                with conn.transaction():
                    context = self.load(conn)
                    self.verify(conn, context)
                    self.assertEqual(self.snapshot(), self.baseline)
            committed = self.snapshot()
            self.assertEqual(len(committed['financial_facts']), 1622)
            with self.connect() as conn:
                with conn.transaction():
                    replay = self.load(conn)
                    self.verify(conn, replay)
                    self.assertEqual(replay.source_ids, context.source_ids)
                    self.assertEqual(replay.outputs['facts'].stored_ids, context.outputs['facts'].stored_ids)
                    self.assertEqual(replay.outputs['tables'].stored_ids, context.outputs['tables'].stored_ids)
            self.assertEqual(self.snapshot(), committed)
            with self.assertRaises(db.DataConflict):
                with self.connect() as conn:
                    with conn.transaction():
                        replay = self.load(conn)
                        table = replay.outputs['tables'].parsed.items[0]
                        db.store_financial_table(conn, replace(table, readable_text='changed committed table'))
            self.assertEqual(self.snapshot(), committed)
        finally:
            if context is not None:
                with self.connect() as conn:
                    filing = context.source_ids.filing_id
                    company = conn.execute('SELECT company_id FROM sec.filings WHERE id=%s', (filing,)).fetchone()
                    for name in ('financial_table_facts', 'financial_tables', 'financial_facts', 'source_documents'):
                        conn.execute(sql.SQL('DELETE FROM sec.{} WHERE filing_id=%s').format(sql.Identifier(name)), (filing,))
                    conn.execute('DELETE FROM sec.filings WHERE id=%s', (filing,))
                    if company:
                        conn.execute('DELETE FROM sec.companies WHERE id=%s', company)
            self.assertEqual(self.snapshot(), self.baseline)

    def failure_case(self, action, error):
        with self.assertRaises(error):
            with self.connect() as conn:
                with conn.transaction():
                    context = self.load(conn)
                    self.verify(conn, context)
                    action(conn, context)
        self.assertEqual(self.snapshot(), self.baseline)

    def test_table_same_version_content_conflict_rolls_back(self):
        def action(conn, context):
            table = context.outputs['tables'].parsed.items[0]
            db.store_financial_table(conn, replace(table, readable_text=table.readable_text + '\nchanged'))
        self.failure_case(action, db.DataConflict)

    def test_fact_same_version_content_conflict_rolls_back(self):
        def action(conn, context):
            fact = context.outputs['facts'].parsed.items[0]
            db.store_fact(conn, replace(fact, label=(fact.label or '') + ' changed'))
        self.failure_case(action, db.DataConflict)

    def test_missing_and_unknown_uuid_rejected(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                def action(conn, context):
                    mapping = fact_ids_for_tables(context)
                    tables = context.outputs['tables'].parsed.items
                    key = next(r['xml_id'] for row in tables[0].structure['rows'] for c in row for r in c['fact_references'])
                    if missing:
                        mapping.pop(key)
                    else:
                        mapping[key] = uuid4()
                    load_tables(conn, tables, FactScope(context.source_ids.filing_id,
                                context.source_ids.xml_document_id, 'facts-v1', mapping))
                self.failure_case(action, TableExtractionError)

    def test_other_filing_source_version_uuid_rejected(self):
        for scope in ('filing', 'source', 'version'):
            with self.subTest(scope=scope):
                def action(conn, context):
                    mapping = fact_ids_for_tables(context)
                    tables = context.outputs['tables'].parsed.items
                    key = next(r['xml_id'] for row in tables[0].structure['rows'] for c in row for r in c['fact_references'])
                    fact = next(f for f in context.outputs['facts'].parsed.items if f.occurrence_key == key)
                    if scope == 'version':
                        altered = replace(fact, extraction_version='facts-other-test')
                    elif scope == 'source':
                        altered = replace(fact, source_document_id=context.source_ids.html_document_id)
                    else:
                        company = conn.execute('SELECT company_id FROM sec.filings WHERE id=%s', (fact.filing_id,)).fetchone()[0]
                        from datetime import date
                        filing = db.register_filing(conn, db.FilingInput(company_id=company,
                                                   accession_number='0001193125-26-999999', form='10-Q',
                                                   filing_date=date.fromisoformat(self.manifest['filing_date']),
                                                   source_url='https://example.invalid/local-scope-test'))
                        source = next(r for r in self.manifest['files'] if r['role']=='extracted_xbrl_instance')
                        from datetime import datetime
                        source_id = db.register_source_document(conn, db.SourceDocumentInput(filing_id=filing,
                                                   role=source['role'], source_url=source['source_url'],
                                                   local_path=source['local_path'], sha256=source['sha256'],
                                                   size_bytes=source['size_bytes'],
                                                   retrieved_at=datetime.fromisoformat(source['retrieved_at_utc'])))
                        altered = replace(fact, filing_id=filing, source_document_id=source_id)
                    mapping[key] = db.store_fact(conn, altered)
                    load_tables(conn, tables, FactScope(context.source_ids.filing_id,
                                context.source_ids.xml_document_id, 'facts-v1', mapping))
                self.failure_case(action, TableExtractionError)

    def test_late_sql_failure_rolls_back_every_stage(self):
        def action(conn, context):
            conn.execute('SELECT 1 / 0')
        self.failure_case(action, psycopg.errors.DivisionByZero)

    def test_error_after_partial_table_stage_rolls_back_outer_transaction(self):
        with self.assertRaises(db.DataConflict):
            with self.connect() as conn:
                with conn.transaction():
                    context = self.context(conn)
                    context.outputs['facts'] = write_facts(conn, context)
                    parsed = parse_tables(context)
                    scope = FactScope(context.source_ids.filing_id, context.source_ids.xml_document_id,
                                      'facts-v1', fact_ids_for_tables(context))
                    ids = load_tables(conn, parsed.items[:1], scope)
                    self.assertEqual(len(ids), 1)
                    db.store_financial_table(conn, replace(parsed.items[0], readable_text='deliberate conflict'))
        self.assertEqual(self.snapshot(), self.baseline)
