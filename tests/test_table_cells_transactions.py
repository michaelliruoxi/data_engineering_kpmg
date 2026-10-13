"""Check standalone cell helper transaction ownership with a second session."""

from dataclasses import replace
from datetime import date, datetime, timezone
import os
import unittest

import psycopg
from psycopg.pq import TransactionStatus

from sec_pipeline import database as db
from sec_pipeline.table_cells import store_table_cells
from sec_pipeline.tables import FactScope, load_tables
import test_catalog_postgres as catalog_tests
from tests.test_table_cells import cell_fixture


@unittest.skipUnless(os.environ.get('SEC_DB_CONCURRENCY_TEST') == '1',
                     'Set SEC_DB_CONCURRENCY_TEST=1 for disposable local database tests.')
class TableCellTransactionTests(unittest.TestCase):
    def test_idle_cell_and_header_table_helpers_keep_caller_transaction_ownership(self):
        with catalog_tests.disposable_catalog_database() as options:
            # Commit a legacy JSON-only parent, facts, and memberships first.
            with psycopg.connect(**options) as setup:
                company = db.register_company(setup, db.CompanyInput(cik='0000000001', name='Cell transaction fixture'))
                filing = db.register_filing(setup, db.FilingInput(company_id=company,
                    accession_number='0000000001-26-000001', form='10-Q', filing_date=date(2026, 4, 1)))
                sources = [db.register_source_document(setup, db.SourceDocumentInput(
                    filing_id=filing, role=role, source_url=f'https://example.invalid/{name}',
                    local_path=name, sha256=char * 64, size_bytes=100,
                    retrieved_at=datetime(2026, 4, 2, tzinfo=timezone.utc)))
                    for role, name, char in (('main_inline_xbrl_filing', 'fixture.htm', 'a'),
                                             ('extracted_xbrl_instance', 'fixture.xml', 'b'))]
                table, facts = cell_fixture(filing, sources[0], sources[1])
                fact_ids = {fact.occurrence_key: db.store_fact(setup, fact) for fact in facts}
                table_id = db.store_financial_table(setup, table)
                for fact_id in fact_ids.values():
                    db.link_table_fact(setup, filing, table_id, fact_id)

            with psycopg.connect(**options) as writer:
                self.assertEqual(writer.info.transaction_status, TransactionStatus.IDLE)
                ids = store_table_cells(writer, table_id, table, fact_ids)
                self.assertEqual(len(ids), 4)
                self.assertEqual(writer.info.transaction_status, TransactionStatus.INTRANS)
                with psycopg.connect(**options) as observer:
                    self.assertEqual(observer.execute('SELECT count(*) FROM sec.table_cells').fetchone()[0], 0)
                    self.assertEqual(observer.execute('SELECT count(*) FROM sec.cell_fact_links').fetchone()[0], 0)
                    self.assertIsNone(observer.execute('SELECT relational_cells_version FROM sec.financial_tables WHERE id=%s',
                                                      (table_id,)).fetchone()[0])
                writer.rollback()
                self.assertEqual(writer.execute('SELECT count(*) FROM sec.table_cells').fetchone()[0], 0)
                self.assertEqual(writer.execute('SELECT count(*) FROM sec.cell_fact_links').fetchone()[0], 0)
                self.assertIsNone(writer.execute('SELECT relational_cells_version FROM sec.financial_tables WHERE id=%s',
                                                (table_id,)).fetchone()[0])
                self.assertEqual(writer.execute('SELECT count(*) FROM sec.financial_table_facts').fetchone()[0], 2)

            # No numeric fact lookup precedes load_tables for an all-header
            # table, so that entry point must also establish its own savepoint.
            header_table = replace(table, extraction_version='headers-v1',
                structure={**table.structure, 'rows': [table.structure['rows'][0]],
                           'inventory': {'header': 1}}, readable_text='Header only fixture')
            with psycopg.connect(**options) as writer:
                self.assertEqual(writer.info.transaction_status, TransactionStatus.IDLE)
                table_ids = load_tables(writer, [header_table], FactScope(
                    filing, sources[1], facts[0].extraction_version, {}))
                header_id = table_ids[header_table.table_key]
                self.assertEqual(writer.info.transaction_status, TransactionStatus.INTRANS)
                with psycopg.connect(**options) as observer:
                    self.assertEqual(observer.execute('SELECT count(*) FROM sec.financial_tables WHERE id=%s',
                                                      (header_id,)).fetchone()[0], 0)
                    self.assertEqual(observer.execute('SELECT count(*) FROM sec.table_cells WHERE table_id=%s',
                                                      (header_id,)).fetchone()[0], 0)
                writer.rollback()
                self.assertEqual(writer.execute('SELECT count(*) FROM sec.financial_tables WHERE id=%s',
                                                (header_id,)).fetchone()[0], 0)
                self.assertEqual(writer.execute('SELECT count(*) FROM sec.table_cells WHERE table_id=%s',
                                                (header_id,)).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
