"""Upgrade real populated 001 databases; use only databases created by the test."""
import hashlib
import os
from pathlib import Path
import unittest

import psycopg
from psycopg.types.json import Jsonb

from sec_pipeline import database as db
import test_catalog_postgres as catalog_tests


@unittest.skipUnless(os.environ.get('SEC_DB_CONCURRENCY_TEST') == '1',
                     'Set SEC_DB_CONCURRENCY_TEST=1 for disposable local database tests.')
class DatabaseUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.options = self.enterContext(catalog_tests.disposable_catalog_database())
        self.conn = self.enterContext(psycopg.connect(**self.options))
        # This schema belongs to the UUID-named database created above, never
        # to the user's configured app/test database.
        self.conn.execute('DROP SCHEMA finance CASCADE')
        self.conn.execute('DROP SCHEMA sec CASCADE')
        self.conn.execute('CREATE SCHEMA sec')
        self.conn.execute('CREATE TABLE sec.schema_migrations (name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz DEFAULT now())')
        initial = Path(db.__file__).parent / 'migrations/001_initial.sql'
        self.conn.execute(initial.read_text())
        self.conn.execute('INSERT INTO sec.schema_migrations(name,sha256) VALUES (%s,%s)',
                          (initial.name, hashlib.sha256(initial.read_bytes()).hexdigest()))
        company = self.conn.execute("INSERT INTO sec.companies(cik,name) VALUES ('0000000888','Legacy company') RETURNING id").fetchone()[0]
        self.filing = self.conn.execute("""INSERT INTO sec.filings(company_id,accession_number,form,filing_date)
            VALUES (%s,'0000000888-26-000001','10-Q','2026-05-01') RETURNING id""", (company,)).fetchone()[0]
        self.source = self.conn.execute("""INSERT INTO sec.source_documents
            (filing_id,role,source_url,local_path,sha256,size_bytes,retrieved_at)
            VALUES (%s,'main_inline_xbrl_filing','https://example.invalid/legacy.htm','legacy.htm',%s,100,now()) RETURNING id""",
            (self.filing, 'a'*64)).fetchone()[0]
        self.text = 'Legacy MD&A 😀.'
        self.sections = [{'title': 'MD&A', 'offset_start': 0, 'offset_end': len(self.text)}]
        self.report = self.conn.execute("""INSERT INTO sec.reports(filing_id,source_document_id,extraction_version,full_text,sections)
            VALUES (%s,%s,'legacy-v1',%s,%s) RETURNING id""",
            (self.filing,self.source,self.text,Jsonb(self.sections))).fetchone()[0]
        self.chunk = self.conn.execute("""INSERT INTO sec.chunks
            (filing_id,report_id,chunking_version,chunk_index,text,offset_start,offset_end,section)
            VALUES (%s,%s,'legacy-chunks',0,%s,0,%s,'legacy-section-0') RETURNING id""",
            (self.filing,self.report,self.text,len(self.text))).fetchone()[0]
        self.conn.commit()

    def test_populated_upgrade_preserves_evidence_and_never_promotes_it(self):
        packaged = sorted(path.name for path in (Path(db.__file__).parent / 'migrations').glob('[0-9][0-9][0-9]_*.sql'))
        self.assertEqual(db.migrate(self.conn), packaged[1:])
        self.assertEqual(db.migrate(self.conn), [])
        self.assertEqual(self.conn.execute('SELECT id,full_text,sections FROM sec.reports').fetchone(),
                         (self.report,self.text,self.sections))
        self.assertEqual(self.conn.execute('SELECT id,text FROM sec.chunks').fetchone(), (self.chunk,self.text))
        self.assertEqual(self.conn.execute('SELECT section_type,validation_status FROM sec.report_sections').fetchone(),
                         ('unknown','unverified'))
        self.assertIsNotNone(self.conn.execute('SELECT section_id FROM sec.chunks').fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM sec.preferred_processing_sets').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT company_name_as_filed FROM sec.filings').fetchone(), (None,))

    def test_ambiguous_source_identity_rolls_back_entire_pending_migration_batch(self):
        self.conn.execute("""INSERT INTO sec.source_documents
            (filing_id,role,source_url,local_path,sha256,size_bytes,retrieved_at)
            SELECT filing_id,role,source_url,'duplicate-location.htm',sha256,size_bytes,retrieved_at
            FROM sec.source_documents WHERE id=%s""", (self.source,))
        self.conn.commit()
        with self.assertRaisesRegex(psycopg.errors.RaiseException, 'Duplicate filing/source URLs'):
            db.migrate(self.conn)
        self.assertEqual(self.conn.execute('SELECT name FROM sec.schema_migrations').fetchall(), [('001_initial.sql',)])
        self.assertIsNone(self.conn.execute("SELECT to_regclass('sec.report_sections')").fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT id,full_text FROM sec.reports').fetchone(), (self.report,self.text))


if __name__ == '__main__':
    unittest.main()
