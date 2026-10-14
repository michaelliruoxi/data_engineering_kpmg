"""Prefetch only accepted table references without weakening stored-fact checks."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from sec_pipeline.database import FinancialTableInput
from sec_pipeline.tables import FactScope, TableExtractionError, load_tables


FILING, HTML, XML, FACT, UNRELATED = (UUID(int=value) for value in (1, 2, 3, 10, 99))
SCOPE = {"filing_id": str(FILING), "xml_document_id": str(XML), "facts_version": "facts-v1"}
UNIT = {"numerator": ["{http://www.xbrl.org/2003/iso4217}USD"], "denominator": []}


def table(version="tables-v2"):
    reference = {
        "xml_id": "revenue-1", "concept": "{http://fasb.org/us-gaap/2026}Revenues", "raw_value": "100",
        "unit": UNIT, "period": {"startDate": "2026-01-01", "endDate": "2026-03-31"},
        "dimensions": [], "is_nil": False, "scope": dict(SCOPE), "method": "inline_id",
        "inline_xpath": "/html/body/table/tr/td[2]/ix:nonfraction",
    }
    label = {"cell_id": "r000c000", "row": 0, "column": 0, "rowspan": 1, "colspan": 1,
             "classification": "label", "display_text": "Revenue", "source_xpath": "/html/body/table/tr/td[1]",
             "source_html_id": None, "fact_references": [], "unresolved_reason": None, "row_heading": "Revenue",
             "column_headings": [], "column_period": None, "is_total": False}
    value = {"cell_id": "r000c001", "row": 0, "column": 1, "rowspan": 1, "colspan": 1,
             "classification": "resolved", "display_text": "100", "source_xpath": "/html/body/table/tr/td[2]",
             "source_html_id": None, "fact_references": [reference], "unresolved_reason": None,
             "row_heading": "Revenue", "column_headings": ["Three months ended March 31, 2026"],
             "column_period": reference["period"], "is_total": False}
    return FinancialTableInput(filing_id=FILING, source_document_id=HTML, extraction_version=version,
        table_key="income_statement", statement_type="income_statement", readable_text="Revenue\t100",
        structure={"schema_version": 1, "fact_scope": dict(SCOPE), "rows": [[label, value]],
                   "inventory": {"label": 1, "resolved": 1}, "source_xpath": "/html/body/table"})


def fact_row(**changes):
    values = {
        "id": FACT, "filing_id": FILING, "source_document_id": XML, "extraction_version": "facts-v1",
        "concept_namespace": "http://fasb.org/us-gaap/2026", "concept_name": "Revenues", "raw_value": "100",
        "unit": deepcopy(UNIT), "period_kind": "duration", "period_start": date(2026, 1, 1),
        "period_end": date(2026, 3, 31), "instant_date": None, "dimensions": [], "is_nil": False,
        "numeric_value": Decimal("100"), "occurrence_key": "revenue-1", "source_reference": {"element_id": "revenue-1"},
    }
    values.update(changes)
    return tuple(values.values())


class ReadConnection:
    autocommit = False

    def __init__(self, records):
        self.records = records
        self.requested_ids = []
        self.queries = []
        self.transactions = 0

    def execute(self, query, args):
        self.queries.append(query)
        if "FROM sec.financial_facts" in query:
            if "ANY" in query:
                ids = args[0]
                self.requested_ids.append(list(ids))
                return SimpleNamespace(fetchall=lambda: [self.records[ident] for ident in ids if ident in self.records])
            self.requested_ids.append([args[0]])
            return SimpleNamespace(fetchone=lambda: self.records[args[0]][1:] if args[0] in self.records else None)
        if query.startswith("SELECT sec.lock_processing_filing"):
            return SimpleNamespace()
        raise AssertionError("Only read/lock SQL is allowed in the prefetch test connection")

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield


class TablePrefetchTests(unittest.TestCase):
    def writes(self):
        storage = patch("sec_pipeline.tables.store_financial_table", side_effect=[UUID(int=100), UUID(int=101)])
        membership = patch("sec_pipeline.bulk_storage.store_many", return_value=[])
        cells = patch("sec_pipeline.tables.store_table_cells", return_value={})
        result = storage.start(), membership.start(), cells.start()
        self.addCleanup(storage.stop)
        self.addCleanup(membership.stop)
        self.addCleanup(cells.stop)
        return result

    def test_unrelated_long_text_fact_is_never_fetched_and_payloads_are_unchanged(self):
        for version in ("tables-v1", "tables-v2"):
            with self.subTest(version=version):
                record = table(version)
                before = deepcopy(asdict(record))
                long_text = fact_row(id=UNRELATED, concept_name="AccountingPoliciesTextBlock",
                                     raw_value="Unrelated accounting policy. " * 100000,
                                     numeric_value=None, unit=None, occurrence_key="footnote-1",
                                     source_reference={"element_id": "footnote-1"})
                conn = ReadConnection({FACT: fact_row(), UNRELATED: long_text})
                scope = FactScope(FILING, XML, "facts-v1", {"revenue-1": FACT, "footnote-1": UNRELATED})
                with patch("sec_pipeline.tables.store_financial_table", return_value=UUID(int=100)) as store, \
                        patch("sec_pipeline.bulk_storage.store_many", return_value=[]) as membership, \
                        patch("sec_pipeline.tables.store_table_cells", return_value={}) as cells:
                    self.assertEqual(load_tables(conn, [record], scope, bulk=True), {"income_statement": UUID(int=100)})
                self.assertEqual(conn.requested_ids, [[FACT]])
                self.assertEqual(asdict(record), before)
                self.assertIs(store.call_args.args[1], record)
                self.assertEqual(membership.call_args.args[2], [{"filing_id": FILING, "table_id": UUID(int=100), "fact_id": FACT}])
                self.assertEqual(cells.call_args.kwargs, {"bulk": True})

    def test_referenced_database_content_and_xml_occurrence_are_still_verified(self):
        changes = (
            {"filing_id": UUID(int=999)}, {"source_document_id": UUID(int=999)}, {"extraction_version": "other-version"},
            {"concept_name": "Assets"}, {"raw_value": "101"}, {"numeric_value": Decimal("101")},
            {"unit": {"numerator": ["shares"], "denominator": []}},
            {"period_start": date(2025, 1, 1)}, {"dimensions": [{"kind": "explicit", "member": "other"}]},
            {"occurrence_key": "different", "source_reference": {"element_id": "different"}},
        )
        for change in changes:
            with self.subTest(change=change):
                conn = ReadConnection({FACT: fact_row(**change)})
                with patch("sec_pipeline.tables.store_financial_table") as store:
                    with self.assertRaises(TableExtractionError):
                        load_tables(conn, [table()], FactScope(FILING, XML, "facts-v1", {"revenue-1": FACT}), bulk=True)
                self.assertEqual(conn.requested_ids, [[FACT]])
                self.assertEqual(conn.transactions, 0)
                store.assert_not_called()

    def test_missing_mapping_and_bad_input_scope_fail_before_prefetch(self):
        for change in ("missing_mapping", "table_scope", "cell_scope", "unresolved"):
            with self.subTest(change=change):
                record = table()
                mapping = {"revenue-1": FACT}
                if change == "missing_mapping":
                    mapping = {"unrelated": UNRELATED}
                elif change == "table_scope":
                    record = replace(record, filing_id=UUID(int=999))
                elif change == "cell_scope":
                    record.structure["rows"][0][1]["fact_references"][0]["scope"]["filing_id"] = str(UUID(int=999))
                else:
                    record.structure["rows"][0][1].update(classification="unresolved", unresolved_reason="unverified", fact_references=[])
                    # A stale inventory must not hide a genuinely unresolved cell.
                    record.structure["inventory"] = {"label": 1, "resolved": 1}
                conn = ReadConnection({FACT: fact_row()})
                with patch("sec_pipeline.tables.store_financial_table") as store:
                    with self.assertRaises(TableExtractionError):
                        load_tables(conn, [record], FactScope(FILING, XML, "facts-v1", mapping), bulk=True)
                self.assertEqual(conn.queries, [])
                store.assert_not_called()

    def test_missing_referenced_database_row_blocks_all_writes(self):
        conn = ReadConnection({})
        with patch("sec_pipeline.tables.store_financial_table") as store:
            with self.assertRaisesRegex(TableExtractionError, "Stored fact differs"):
                load_tables(conn, [table()], FactScope(FILING, XML, "facts-v1", {"revenue-1": FACT}), bulk=True)
        self.assertEqual(conn.requested_ids, [[FACT]])
        self.assertEqual(conn.transactions, 0)
        store.assert_not_called()

    def test_shared_references_are_fetched_once_but_keep_each_table_membership(self):
        first = table()
        second = replace(deepcopy(first), table_key="income_detail")
        conn = ReadConnection({FACT: fact_row()})
        store, membership, cells = self.writes()
        ids = load_tables(conn, iter([first, second]), FactScope(FILING, XML, "facts-v1", {"revenue-1": FACT, "unrelated": UNRELATED}), bulk=True)
        self.assertEqual(conn.requested_ids, [[FACT]])
        self.assertEqual(set(ids), {"income_statement", "income_detail"})
        self.assertEqual(store.call_count, 2)
        self.assertEqual(cells.call_count, 2)
        self.assertEqual([call.args[2][0]["fact_id"] for call in membership.call_args_list], [FACT, FACT])
        self.assertEqual({call.args[2][0]["table_id"] for call in membership.call_args_list}, set(ids.values()))

    def test_table_without_fact_references_does_not_fetch_unrelated_scope_facts(self):
        record = table()
        record.structure["rows"][0][1].update(classification="header", fact_references=[])
        record.structure["inventory"] = {"label": 1, "header": 1}
        conn = ReadConnection({UNRELATED: fact_row(id=UNRELATED)})
        self.writes()
        load_tables(conn, [record], FactScope(FILING, XML, "facts-v1", {"unrelated": UNRELATED}), bulk=True)
        self.assertEqual(conn.requested_ids, [])
        self.assertEqual(conn.queries, ["SELECT sec.lock_processing_filing(%s)"])


if __name__ == "__main__":
    unittest.main()
