"""Metadata identity regression tests using the dedicated opt-in PostgreSQL DB."""
from dataclasses import asdict, replace
from datetime import date, timedelta
import os
import unittest
from types import SimpleNamespace
from uuid import UUID

from sec_pipeline import database as db
import test_database_integration as fixtures


class MetadataReplayTests(unittest.TestCase):
    """Case-only spelling changes reuse historical metadata without an update."""

    def setUp(self):
        self.filing_id = UUID(int=2)
        self.original = db.FilingInput(
            company_id=UUID(int=1), accession_number='0000789019-26-000001',
            form='10-Q', filing_date=date(2026, 4, 29), reporting_period_end=date(2026, 3, 31),
            source_url='https://example.invalid/filing/index.html',
            company_name_as_filed='Microsoft Corporation',
        )
        self.calls = []

        def execute(statement, parameters):
            sql = statement if isinstance(statement, str) else statement.as_string()
            self.calls.append((sql, parameters))
            if sql.startswith('SELECT id, company_name_as_filed'):
                row = (self.filing_id, self.original.company_name_as_filed)
            elif sql.startswith('INSERT INTO'):
                row = None  # Existing accession: ON CONFLICT does not insert.
            elif sql.startswith('SELECT id FROM'):
                row = (self.filing_id,) if list(parameters) == list(asdict(self.original).values()) else None
            else:
                raise AssertionError('Replay must not UPDATE or DELETE stored metadata.')
            return SimpleNamespace(fetchone=lambda: row)

        self.connection = SimpleNamespace(autocommit=False, execute=execute)

    def test_case_only_name_replay_preserves_first_spelling_without_update(self):
        incoming = replace(self.original, company_name_as_filed='MICROSOFT CORPORATION')
        self.assertEqual(db.register_filing(self.connection, incoming), self.filing_id)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[1][1][-1], 'Microsoft Corporation')
        self.assertEqual(self.calls[2][1][-1], 'Microsoft Corporation')
        self.assertFalse(any(sql.startswith(('UPDATE', 'DELETE')) for sql, _ in self.calls))

    def test_case_only_name_does_not_relax_other_filing_identity_fields(self):
        incoming = replace(self.original, company_name_as_filed='MICROSOFT CORPORATION', form='10-K')
        with self.assertRaises(db.DataConflict):
            db.register_filing(self.connection, incoming)
        self.assertEqual(len(self.calls), 3)

    def test_different_legal_name_still_conflicts_before_storage(self):
        for name in ('Microsoft Holdings', 'Microsoft Corporation.', 'Microsoft  Corporation'):
            with self.subTest(name=name):
                self.calls.clear()
                with self.assertRaises(db.DataConflict):
                    db.register_filing(self.connection, replace(self.original, company_name_as_filed=name))
                self.assertEqual(len(self.calls), 1)

    def test_absent_incoming_name_preserves_existing_spelling(self):
        incoming = replace(self.original, company_name_as_filed=None)
        self.assertEqual(db.register_filing(self.connection, incoming), self.filing_id)
        self.assertEqual(self.calls[1][1][-1], 'Microsoft Corporation')


@unittest.skipUnless(os.environ.get('SEC_DB_TEST') == '1', 'Set SEC_DB_TEST=1 for live database tests.')
class MetadataTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.DatabaseIntegrationTests.setUpClass.__func__)
    setUp = fixtures.DatabaseIntegrationTests.setUp
    fixture = fixtures.DatabaseIntegrationTests.fixture

    def test_company_profile_and_filing_snapshot_have_separate_lifetimes(self):
        company, filing, source = self.fixture()
        identity = db.register_company(self.conn, db.CompanyInput(cik='0000000001', name='Historical name'))
        self.assertEqual(identity, company)
        db.update_company_profile(self.conn, db.CompanyInput(cik='0000000001', name='Current name', ticker='NEW'))
        original = db.FilingInput(company_id=company, accession_number='0000000001-26-000001',
            form='10-Q', filing_date=date(2026, 4, 29), reporting_period_end=date(2026, 3, 31),
            source_url='https://example.invalid/1/index.html', company_name_as_filed='Historical name')
        self.assertEqual(db.register_filing(self.conn, original), filing)
        self.assertEqual(db.register_filing(self.conn, replace(original, company_name_as_filed=None)), filing)
        self.assertEqual(self.conn.execute('''SELECT c.name, c.ticker, f.company_name_as_filed
            FROM sec.filings f JOIN sec.companies c ON c.id=f.company_id WHERE f.id=%s''',
            (filing,)).fetchone(), ('Current name', 'NEW', 'Historical name'))
        with self.assertRaises(db.DataConflict):
            db.register_filing(self.conn, replace(original, company_name_as_filed='Conflicting name'))

    def test_refetch_and_relocation_preserve_document_identity_and_first_receipt(self):
        _, filing, source = self.fixture()
        row = self.conn.execute('''SELECT role, source_url, local_path, sha256, size_bytes, retrieved_at
            FROM sec.source_documents WHERE id=%s''', (source,)).fetchone()
        record = db.SourceDocumentInput(filing_id=filing, role=row[0], source_url=row[1],
            local_path='relocated/same.xml', sha256=row[3], size_bytes=row[4],
            retrieved_at=row[5] + timedelta(days=1))
        self.assertEqual(db.register_source_document(self.conn, record), source)
        self.assertEqual(self.conn.execute('SELECT local_path,retrieved_at FROM sec.source_documents WHERE id=%s',
            (source,)).fetchone(), (row[2], row[5]))
        with self.assertRaises(db.DataConflict):
            db.register_source_document(self.conn, replace(record, sha256='f' * 64))

    def test_profile_update_requires_existing_identity(self):
        with self.assertRaises(db.DatabaseError):
            db.update_company_profile(self.conn, db.CompanyInput(cik='0000000999', name='Missing'))
