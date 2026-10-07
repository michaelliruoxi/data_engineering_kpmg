"""Opt-in live PostgreSQL contract tests; every domain fixture is rolled back.

Set SEC_DB_TEST=1 and create/migrate a dedicated sec_filings_test database first.
SEC_DB_TEST_DATABASE may select another dedicated name ending in _test.
This module never creates, drops, truncates, or commits domain data.
"""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import psycopg

from sec_pipeline import database as db


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('SEC_DB_TEST') == '1', 'Set SEC_DB_TEST=1 for live database tests.')
class DatabaseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_name = os.environ.get('SEC_DB_TEST_DATABASE', 'sec_filings_test')
        if not re.fullmatch(r'[A-Za-z0-9_]+_test(?:_[A-Za-z0-9_]+)?', cls.database_name):
            raise RuntimeError('Live tests require an explicitly dedicated database name containing _test.')
        configured_database = db.connection_settings(PROJECT_ROOT / '.env')['dbname']
        if cls.database_name == configured_database:
            raise RuntimeError('Test database must differ from the configured application database.')
        with db.connect(PROJECT_ROOT / '.env', dbname=cls.database_name) as conn:
            baseline = db.status(conn)
        if not baseline['ready']:
            raise RuntimeError('Apply migrations to the dedicated test database before running tests.')
        if any(baseline['counts'].values()):
            raise RuntimeError('Dedicated test database must start empty; tests never delete existing data.')
        cls.baseline = baseline

    def setUp(self):
        self.conn = db.connect(PROJECT_ROOT / '.env', dbname=self.database_name)
        self.addCleanup(self.conn.close)
        transaction = self.conn.transaction(force_rollback=True)
        transaction.__enter__()
        self.addCleanup(transaction.__exit__, None, None, None)

    def fixture(self, number=1):
        company = db.register_company(self.conn, db.CompanyInput(
            cik=f'{number:010d}', name=f'Fixture Company {number}', ticker=f'F{number}',
        ))
        filing = db.register_filing(self.conn, db.FilingInput(
            company_id=company, accession_number=f'{number:010d}-26-000001', form='10-Q',
            filing_date=date(2026, 4, 29), reporting_period_end=date(2026, 3, 31),
            source_url=f'https://example.invalid/{number}/index.html',
        ))
        source = db.register_source_document(self.conn, db.SourceDocumentInput(
            filing_id=filing, role='extracted_xbrl_instance',
            source_url=f'https://example.invalid/{number}/source.xml',
            local_path=f'fixture/{number}/source.xml', sha256=f'{number:064x}', size_bytes=123,
            retrieved_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ))
        return company, filing, source

    def fact(self, filing, source, **changes):
        record = db.FactInput(
            filing_id=filing, source_document_id=source, extraction_version='extract-v1',
            occurrence_key='fact-1', concept_namespace='https://example.invalid/taxonomy/2026',
            concept_name='Revenue', numeric_value=Decimal('1'), raw_value='1',
            period_kind='duration', period_start=date(2026, 1, 1), period_end=date(2026, 3, 31),
            unit={'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []},
        )
        return replace(record, **changes)

    def test_00_baseline_status_and_repeated_migrations_are_readable_and_stable(self):
        before = db.status(self.conn)
        self.assertTrue(before['ready'])
        self.assertEqual(before['counts'], self.baseline['counts'])
        self.assertTrue(all(count == 0 for count in before['counts'].values()))
        self.assertEqual(db.migrate(self.conn), [])
        self.assertEqual(db.migrate(self.conn), [])
        self.assertEqual(db.status(self.conn), before)

    def test_migration_checksum_drift_and_unknown_history_are_rejected(self):
        with self.conn.transaction(force_rollback=True):
            self.conn.execute("UPDATE sec.schema_migrations SET sha256 = %s", ('0' * 64,))
            with self.assertRaisesRegex(db.DatabaseError, 'checksum changed'):
                db.migrate(self.conn)
        with self.conn.transaction(force_rollback=True):
            self.conn.execute(
                'INSERT INTO sec.schema_migrations (name, sha256) VALUES (%s, %s)',
                ('999_absent.sql', '0' * 64),
            )
            with self.assertRaisesRegex(db.DatabaseError, 'absent from this package'):
                db.migrate(self.conn)

    def test_sample_seed_replays_metadata_only_without_http_or_file_changes(self):
        manifest_path = PROJECT_ROOT / 'source_manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        records = [manifest['index_document'], *manifest['files']]
        paths = [manifest_path, *(PROJECT_ROOT / record['local_path'] for record in records)]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        with (patch('httpx.Client.send', side_effect=AssertionError('Unexpected HTTP request')),
              patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected HTTP request'))):
            first = db.seed_manifest(self.conn, manifest_path, PROJECT_ROOT)
            second = db.seed_manifest(self.conn, manifest_path, PROJECT_ROOT)
        self.assertEqual(first, second)
        self.assertEqual(len(first['document_ids']), 4)
        counts = db.status(self.conn)['counts']
        self.assertEqual((counts.pop('companies'), counts.pop('filings'), counts.pop('source_documents')), (1, 1, 4))
        self.assertTrue(all(count == 0 for count in counts.values()))
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}, before)
        catalog = self.conn.execute('SELECT cik, source_document_count, fact_count FROM sec.filing_catalog').fetchone()
        self.assertEqual(catalog, ('0000789019', 4, 0))

    def test_seed_conflict_rolls_back_sources_inserted_earlier_in_batch(self):
        manifest_path = PROJECT_ROOT / 'source_manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        company = db.register_company(self.conn, db.CompanyInput(cik=manifest['cik'], name=manifest['company']))
        filing = db.register_filing(self.conn, db.FilingInput(
            company_id=company, accession_number=manifest['accession_number'], form=manifest['form'],
            filing_date=date.fromisoformat(manifest['filing_date']),
            reporting_period_end=date.fromisoformat(manifest['reporting_period_end']),
            source_url=manifest['filing_index_url'],
        ))
        record = manifest['files'][-1]
        existing = db.register_source_document(self.conn, db.SourceDocumentInput(
            filing_id=filing, role=record['role'], source_url=record['source_url'],
            local_path=record['local_path'], sha256='0' * 64, size_bytes=record['size_bytes'],
            retrieved_at=datetime.fromisoformat(record['retrieved_at_utc']),
        ))
        before = db.status(self.conn)['counts']
        with self.assertRaises(db.DataConflict):
            db.seed_manifest(self.conn, manifest_path, PROJECT_ROOT)
        self.assertEqual(db.status(self.conn)['counts'], before)
        self.assertEqual(self.conn.execute('SELECT id FROM sec.source_documents').fetchall(), [(existing,)])

    def test_exact_values_nil_zero_dimensions_units_and_provenance(self):
        _, filing, source = self.fixture()
        exact = Decimal('123456789012345678901234567890.123456789012345678901234567890')
        dimensions = [
            {'kind': 'explicit', 'dimension': '{https://example.invalid/taxonomy/2026}SegmentAxis',
             'member': '{https://example.invalid/taxonomy/2026}CloudMember'},
            {'kind': 'typed', 'dimension': '{https://example.invalid/taxonomy/2026}RegionAxis',
             'value': '<region xmlns="urn:example">East</region>'},
        ]
        unit = {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'],
                'denominator': ['{http://www.xbrl.org/2003/instance}shares']}
        fact_id = db.store_fact(self.conn, self.fact(
            filing, source, numeric_value=exact, raw_value=str(exact), unit=unit,
            dimensions=dimensions, reported_decimals='-6', source_reference={'context_id': 'context-7'},
        ))
        zero = db.store_fact(self.conn, self.fact(
            filing, source, occurrence_key='zero', numeric_value=Decimal('0'), raw_value='0', reported_decimals='INF',
        ))
        nil = db.store_fact(self.conn, self.fact(
            filing, source, occurrence_key='nil', numeric_value=None, raw_value=None, is_nil=True,
        ))
        precision = db.store_fact(self.conn, self.fact(
            filing, source, occurrence_key='precision-zero', reported_precision='0',
        ))
        row = self.conn.execute('''SELECT numeric_value, raw_value, unit, dimensions, reported_decimals,
            source_reference, cik, accession_number, sha256 FROM sec.fact_provenance WHERE fact_id = %s''', (fact_id,)).fetchone()
        self.assertEqual(row, (exact, str(exact), unit, dimensions, '-6', {'context_id': 'context-7'},
                              '0000000001', '0000000001-26-000001', f'{1:064x}'))
        self.assertEqual(self.conn.execute('SELECT numeric_value, is_nil FROM sec.financial_facts WHERE id=%s', (zero,)).fetchone(), (Decimal('0'), False))
        self.assertEqual(self.conn.execute('SELECT numeric_value, is_nil FROM sec.financial_facts WHERE id=%s', (nil,)).fetchone(), (None, True))
        self.assertEqual(self.conn.execute('SELECT reported_precision FROM sec.financial_facts WHERE id=%s', (precision,)).fetchone(), ('0',))

    def test_fact_occurrences_and_versions_preserve_original_content(self):
        _, filing, source = self.fixture()
        record = self.fact(filing, source)
        first = db.store_fact(self.conn, record)
        self.assertEqual(db.store_fact(self.conn, record), first)
        with self.assertRaises(db.DataConflict):
            db.store_fact(self.conn, replace(record, numeric_value=Decimal('2')))
        occurrence = db.store_fact(self.conn, replace(record, occurrence_key='fact-2'))
        version = db.store_fact(self.conn, replace(record, extraction_version='extract-v2', numeric_value=Decimal('2')))
        self.assertEqual(len({first, occurrence, version}), 3)
        self.assertEqual(self.conn.execute('SELECT numeric_value FROM sec.financial_facts WHERE id=%s', (first,)).fetchone()[0], Decimal('1'))

    def test_periods_nil_json_and_nonfinite_values_are_database_constraints(self):
        _, filing, source = self.fixture()
        valid = self.fact(filing, source)
        invalid_records = (
            replace(valid, period_start=date(2026, 4, 1)),
            replace(valid, instant_date=date(2026, 3, 31)),
            replace(valid, period_kind='instant', period_start=None, period_end=None),
            replace(valid, period_kind='forever'),
            replace(valid, is_nil=True),
            replace(valid, numeric_value=None, raw_value=None),
            replace(valid, dimensions={}),
            replace(valid, unit=[]),
            replace(valid, reported_decimals='six'),
            replace(valid, reported_precision='-1'),
        )
        for index, record in enumerate(invalid_records):
            with self.subTest(case=index), self.assertRaises(psycopg.errors.CheckViolation):
                with self.conn.transaction():
                    db.store_fact(self.conn, record)
        instant = db.store_fact(self.conn, replace(valid, period_kind='instant', period_start=None,
            period_end=None, instant_date=date(2026, 3, 31)))
        db.store_fact(self.conn, replace(valid, occurrence_key='forever', period_kind='forever', period_start=None, period_end=None))
        for value in ('NaN', 'Infinity', '-Infinity'):
            with self.subTest(value=value), self.assertRaises(psycopg.errors.CheckViolation):
                with self.conn.transaction():
                    self.conn.execute('UPDATE sec.financial_facts SET numeric_value=%s::numeric WHERE id=%s', (value, instant))

    def test_cross_filing_references_and_amendments_are_rejected(self):
        company, filing, source = self.fixture()
        _, other_filing, other_source = self.fixture(2)
        report = db.store_report(self.conn, db.ReportInput(
            filing_id=filing, source_document_id=source, extraction_version='extract-v1', full_text='Revenue',
        ))
        table_record = db.FinancialTableInput(
            filing_id=filing, source_document_id=source, extraction_version='extract-v1',
            table_key='income', statement_type='income_statement', structure={'rows': []}, readable_text='Revenue',
        )
        table = db.store_financial_table(self.conn, table_record)
        fact = db.store_fact(self.conn, self.fact(filing, source))
        other_fact = db.store_fact(self.conn, self.fact(other_filing, other_source))
        invalid_operations = (
            lambda: db.store_report(self.conn, db.ReportInput(filing_id=other_filing, source_document_id=source, extraction_version='bad', full_text='x')),
            lambda: db.store_chunk(self.conn, db.ChunkInput(filing_id=other_filing, report_id=report, chunking_version='v1', chunk_index=0, text='Revenue', offset_start=0, offset_end=7)),
            lambda: db.store_fact(self.conn, self.fact(other_filing, source, occurrence_key='bad')),
            lambda: db.store_financial_table(self.conn, replace(table_record, filing_id=other_filing, table_key='bad')),
            lambda: db.link_table_fact(self.conn, filing, table, other_fact),
            lambda: db.register_filing(self.conn, db.FilingInput(company_id=company, accession_number='0000000001-26-000002', form='10-Q/A', filing_date=date(2026, 5, 1), amendment_of_id=other_filing)),
        )
        for index, operation in enumerate(invalid_operations):
            with self.subTest(case=index), self.assertRaises(psycopg.errors.ForeignKeyViolation):
                with self.conn.transaction():
                    operation()
        db.link_table_fact(self.conn, filing, table, fact)
        db.link_table_fact(self.conn, filing, table, fact)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM sec.financial_table_facts').fetchone()[0], 1)
        with self.assertRaises(db.DataConflict):
            db.link_table_fact(self.conn, other_filing, table, fact)

    def test_unicode_chunks_citations_report_versions_and_report_edit_guard(self):
        _, filing, source = self.fixture()
        text = 'Revenue 😀 rose e\u0301 by €5.\n净利润增长。'
        report_record = db.ReportInput(
            filing_id=filing, source_document_id=source, extraction_version='extract-v1',
            full_text=text, sections=[{'title': 'Results', 'offset_start': 0, 'offset_end': len(text)}],
        )
        report = db.store_report(self.conn, report_record)
        self.assertEqual(db.store_report(self.conn, report_record), report)
        start, end = text.index('😀'), text.index('\n')
        record = db.ChunkInput(filing_id=filing, report_id=report, chunking_version='chunks-v1',
            chunk_index=0, section='Results', text=text[start:end], offset_start=start, offset_end=end)
        chunk = db.store_chunk(self.conn, record)
        self.assertEqual(db.store_chunk(self.conn, record), chunk)
        citation = self.conn.execute('''SELECT text, offset_start, offset_end, extraction_version,
            source_url, cik FROM sec.chunk_citations WHERE chunk_id=%s''', (chunk,)).fetchone()
        self.assertEqual(citation, (text[start:end], start, end, 'extract-v1', 'https://example.invalid/1/source.xml', '0000000001'))
        invalid_chunks = (
            replace(record, chunk_index=1, offset_end=len(text.encode('utf-8'))),
            replace(record, chunk_index=1, text='Altered quotation'),
            replace(record, chunk_index=1, offset_start=-1),
        )
        for index, invalid in enumerate(invalid_chunks):
            with self.subTest(case=index), self.assertRaises(psycopg.errors.CheckViolation):
                with self.conn.transaction():
                    db.store_chunk(self.conn, invalid)
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.conn.transaction():
                self.conn.execute('UPDATE sec.reports SET full_text=%s WHERE id=%s', ('short', report))
        with self.assertRaises(db.DataConflict):
            db.store_report(self.conn, replace(report_record, full_text='Changed'))
        with self.assertRaises(db.DataConflict):
            db.store_chunk(self.conn, replace(record, metadata={'changed': True}))
        self.assertNotEqual(db.store_report(self.conn, replace(report_record, extraction_version='extract-v2', full_text='Changed')), report)
        self.assertNotEqual(db.store_chunk(self.conn, replace(record, chunking_version='chunks-v2')), chunk)

    def test_table_identity_and_structure_are_versioned(self):
        _, filing, source = self.fixture()
        record = db.FinancialTableInput(
            filing_id=filing, source_document_id=source, extraction_version='extract-v1', table_key='income',
            statement_type='income_statement', structure={'columns': ['Metric', 'Value'], 'rows': [['Revenue', '1']]},
            readable_text='Revenue | 1',
        )
        first = db.store_financial_table(self.conn, record)
        self.assertEqual(db.store_financial_table(self.conn, record), first)
        with self.assertRaises(db.DataConflict):
            db.store_financial_table(self.conn, replace(record, readable_text='Revenue | 2'))
        self.assertNotEqual(db.store_financial_table(self.conn, replace(record, extraction_version='extract-v2')), first)

    def test_write_helpers_leave_commit_and_rollback_to_the_caller(self):
        writer = db.connect(PROJECT_ROOT / '.env', dbname=self.database_name)
        self.addCleanup(writer.close)
        self.addCleanup(writer.rollback)
        record = db.CompanyInput(cik='0000000999', name='Uncommitted Fixture')
        company = db.register_company(writer, record)
        with db.connect(PROJECT_ROOT / '.env', dbname=self.database_name) as observer:
            self.assertEqual(observer.execute('SELECT count(*) FROM sec.companies WHERE id=%s', (company,)).fetchone()[0], 0)
        writer.rollback()
        self.assertEqual(writer.execute('SELECT count(*) FROM sec.companies WHERE id=%s', (company,)).fetchone()[0], 0)
        writer.rollback()
        with self.assertRaises(db.DataConflict):
            with writer.transaction():
                db.register_company(writer, record)
                db.register_company(writer, replace(record, name='Conflicting Fixture'))
        self.assertEqual(writer.execute('SELECT count(*) FROM sec.companies WHERE cik=%s', (record.cik,)).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
