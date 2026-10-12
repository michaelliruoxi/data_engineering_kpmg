"""Actual sample facts/table compatibility; storage is mocked, not PostgreSQL."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID

from sec_pipeline.facts_bridge import parse_facts
from sec_pipeline.processing import ProcessingError, SourceIds, WriteResult
from sec_pipeline.tables import TableExtractionError
from sec_pipeline.tables_bridge import parse_tables, write_tables

ROOT = Path(__file__).resolve().parents[1]


class TablesBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = SimpleNamespace(
            manifest=json.loads((ROOT / 'source_manifest.json').read_text()),
            data_root=ROOT, source_ids=SourceIds(UUID(int=1), UUID(int=3), UUID(int=2)),
            versions={'facts': 'facts-v1', 'tables': 'tables-v1',
                      'reports': 'reports-v1', 'chunks': 'chunks-v1'}, outputs={})
        cls.facts = parse_facts(cls.context)
        cls.stored = WriteResult(cls.facts, [UUID(int=i + 100) for i in range(len(cls.facts.items))])

    def context_with_facts(self):
        context = copy.deepcopy(self.context)
        context.outputs['facts'] = self.stored
        return context

    def database_for_facts(self):
        records = {}
        for fact, ident in zip(self.facts.items, self.stored.stored_ids):
            records[ident] = (
                fact.filing_id, fact.source_document_id, fact.extraction_version,
                fact.concept_namespace, fact.concept_name, fact.raw_value,
                fact.unit, fact.period_kind, fact.period_start, fact.period_end,
                fact.instant_date, fact.dimensions, fact.is_nil, fact.numeric_value,
                fact.occurrence_key, fact.source_reference)
        conn = MagicMock(autocommit=False)
        conn.execute.side_effect = lambda query, args: SimpleNamespace(fetchone=lambda: records.get(args[0]))
        return conn

    def test_actual_sample_links_use_bryce_records(self):
        self.assertEqual(len(self.facts.items), 1622)
        conn = self.database_for_facts()
        with patch('sec_pipeline.tables.store_financial_table', side_effect=[UUID(int=3000+i) for i in range(3)]) as store, patch('sec_pipeline.tables.link_table_fact') as link:
            result = write_tables(conn, self.context_with_facts())
        self.assertEqual(result.parsed.unresolved_cells, 0)
        self.assertEqual(len(result.stored_ids), 3)
        self.assertEqual(store.call_count, 3)
        self.assertEqual(link.call_count, 283)
        actual_ids = set(self.stored.stored_ids)
        self.assertTrue(all(call.args[3] in actual_ids for call in link.call_args_list))
        conn.commit.assert_not_called()

    def test_pure_stage_needs_no_stored_ids(self):
        result = parse_tables(self.context)
        self.assertEqual([t.table_key for t in result.items],
                         ['income_statement', 'balance_sheet', 'cash_flow'])
        self.assertEqual(result.unresolved_cells, 0)

    def test_missing_facts_stage_blocks_write(self):
        with self.assertRaises(ProcessingError):
            write_tables(self.database_for_facts(), self.context)

    def test_wrong_fact_scope_blocks_write(self):
        context = self.context_with_facts()
        context.source_ids = SourceIds(UUID(int=99), UUID(int=3), UUID(int=2))
        with self.assertRaises(ProcessingError):
            write_tables(self.database_for_facts(), context)

    def test_missing_inline_mapping_blocks_write(self):
        context = self.context_with_facts()
        tables = parse_tables(context).items
        key = next(ref['xml_id'] for row in tables[0].structure['rows']
                   for cell in row for ref in cell['fact_references'])
        pairs = [(f, i) for f, i in zip(self.facts.items, self.stored.stored_ids) if f.occurrence_key != key]
        context.outputs['facts'] = WriteResult(type(self.facts)([p[0] for p in pairs]), [p[1] for p in pairs])
        with patch('sec_pipeline.tables.store_financial_table') as store:
            with self.assertRaises(TableExtractionError):
                write_tables(self.database_for_facts(), context)
            store.assert_not_called()

    def test_database_fact_mismatch_blocks_write(self):
        conn = self.database_for_facts()
        conn.execute.side_effect = lambda query, args: SimpleNamespace(fetchone=lambda: None)
        with patch('sec_pipeline.tables.store_financial_table') as store:
            with self.assertRaises(TableExtractionError):
                write_tables(conn, self.context_with_facts())
            store.assert_not_called()

    def test_unsupported_versions_fail(self):
        for name in ('facts', 'tables'):
            context = copy.deepcopy(self.context)
            context.versions[name] = 'unexpected-version'
            with self.assertRaises(ProcessingError):
                parse_tables(context)

    def test_source_path_escape_and_duplicates_fail(self):
        for duplicate in (False, True):
            context = copy.deepcopy(self.context)
            record = next(r for r in context.manifest['files'] if r['role'] == 'main_inline_xbrl_filing')
            if duplicate:
                context.manifest['files'].append(copy.deepcopy(record))
            else:
                record['local_path'] = '../outside.htm'
            with self.assertRaises(ProcessingError):
                parse_tables(context)
