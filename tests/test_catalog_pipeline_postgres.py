"""Committed ingestion/catalog integration on disposable local databases only."""
import os
import unittest
from unittest.mock import patch

import psycopg

from sec_pipeline import database as db
from sec_pipeline.processing import ProcessingError, run_ingestion
import test_catalog_postgres as catalog_tests
import test_ingestion_postgres as ingestion_tests


@unittest.skipUnless(os.environ.get('SEC_DB_CONCURRENCY_TEST') == '1',
                     'Set SEC_DB_CONCURRENCY_TEST=1 for disposable local database tests.')
class CatalogPipelineTests(unittest.TestCase):
    def setUp(self):
        self.options = self.enterContext(catalog_tests.disposable_catalog_database())
        self.enterContext(patch.object(ingestion_tests.IngestionPostgresTests,
                                      'connection_options', self.options))
        self.runner = ingestion_tests.IngestionPostgresTests()
        self.runner.manifest = ingestion_tests.load_verified_test_inputs()
        self.runner.versions = {name: f'catalog-pipeline-{name}-v1'
                               for name in ('facts', 'tables', 'reports', 'chunks')}
        self.runner.required = ('metadata', 'facts', 'tables_links', 'report', 'chunks', 'uncommitted')
        self.runner.events = []
        self.runner.owned_filings = set()
        self.runner.owned_companies = set()
        self.coverage = {name: 'partial' for name in self.runner.versions}

    def run_import(self, **overrides):
        self.runner.observer_before = self.runner.snapshot()
        options = dict(
            manifest_path=ingestion_tests.ROOT / 'source_manifest.json',
            data_root=ingestion_tests.ROOT, versions=self.runner.versions,
            plan=self.runner.ingestion_fixture_plan(),
            verify_inputs=lambda path, root: ingestion_tests.load_verified_test_inputs(),
            processing_set_key='transaction-fixture-v1', coverage=self.coverage,
            prefer_processing_set=True,
        ) | overrides
        with psycopg.connect(**self.options) as conn:
            return run_ingestion(conn, **options)

    def test_ingestion_selection_commits_and_sealed_replay_keeps_ids(self):
        first = self.run_import()
        second = self.run_import()
        self.assertEqual(first, second)
        self.assertTrue(first['preferred'])
        with psycopg.connect(**self.options) as conn:
            row = conn.execute('SELECT processing_set_id, coverage, counts FROM sec.filing_overview').fetchone()
            self.assertEqual(str(row[0]), first['processing_set_id'])
            self.assertEqual(row[1], self.coverage)
            self.assertEqual(row[2]['facts'], 2)
            self.assertEqual(conn.execute('SELECT count(*) FROM sec.fact_catalog').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT count(*) FROM sec.section_catalog').fetchone()[0], 1)

    def test_selection_failure_rolls_back_every_stage_and_set(self):
        with patch('sec_pipeline.catalog.select_preferred_processing_set',
                   side_effect=db.DatabaseError('Deliberate selection failure')):
            with self.assertRaises(ProcessingError):
                self.run_import()
        with psycopg.connect(**self.options) as conn:
            self.assertTrue(all(count == 0 for count in db.status(conn)['counts'].values()))
        self.assertTrue(self.run_import()['preferred'])

    def test_failed_reference_validation_never_creates_or_selects_a_set(self):
        with self.assertRaises(ProcessingError):
            self.run_import(reference={'expected_counts': {'facts': 999}})
        with psycopg.connect(**self.options) as conn:
            self.assertTrue(all(count == 0 for count in db.status(conn)['counts'].values()))

    def test_report_helper_never_commits_an_idle_connection(self):
        self.run_import()
        with psycopg.connect(**self.options) as reader:
            filing, source = reader.execute('SELECT filing_id,source_document_id FROM sec.reports').fetchone()
        report = db.ReportInput(filing_id=filing, source_document_id=source,
                                extraction_version='uncommitted-report-v2', full_text='Uncommitted text')
        with psycopg.connect(**self.options) as writer:
            report_id = db.store_report(writer, report)
            with psycopg.connect(**self.options) as observer:
                self.assertEqual(observer.execute('SELECT count(*) FROM sec.reports WHERE id=%s',
                                                  (report_id,)).fetchone()[0], 0)
            writer.rollback()
            self.assertEqual(writer.execute('SELECT count(*) FROM sec.reports WHERE id=%s',
                                             (report_id,)).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
