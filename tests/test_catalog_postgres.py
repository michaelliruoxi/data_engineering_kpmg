"""Opt-in processing catalog tests on local dedicated PostgreSQL databases.

SEC_DB_TEST=1 enables rollback-only fixtures using SEC_DB_TEST_DATABASE.
SEC_DB_CONCURRENCY_TEST=1 additionally enables one disposable database created
with a unique name by this test, used for committed two-session cases, and
removed after all connections close. Never points at RDS or an app database.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import os
import re
from threading import Event, Thread
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from sec_pipeline import database as db
from sec_pipeline.catalog import ProcessingSetInput, select_preferred_processing_set, store_processing_set


def local_test_options():
    name = os.environ.get("SEC_DB_TEST_DATABASE", "sec_filings_test")
    if not re.fullmatch(r"[A-Za-z0-9_]+_test(?:_[A-Za-z0-9_]+)?", name):
        raise RuntimeError("Catalog tests require a dedicated *_test database name.")
    options = db.connection_settings(None) | {"dbname": name}
    if options["host"] not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("Catalog tests require an explicitly local database.")
    return options


@contextmanager
def disposable_catalog_database():
    """Yield migrated local connection kwargs; remove only the DB created here.

    Shared by committed pipeline/concurrency tests. Requires the additional
    explicit opt-in flag and never creates or drops the configured test DB.
    """
    if os.environ.get("SEC_DB_CONCURRENCY_TEST") != "1":
        raise RuntimeError("Disposable database tests require SEC_DB_CONCURRENCY_TEST=1.")
    base_options=local_test_options()
    database_name="sec_catalog_concurrency_test_"+uuid4().hex[:12]
    options=base_options | {"dbname":database_name}
    created=False
    try:
        with psycopg.connect(**base_options,autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
            created=True
        with psycopg.connect(**options) as conn:
            db.migrate(conn)
        yield options
    finally:
        if created:
            with psycopg.connect(**base_options,autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


def fixture(conn, number=71):
    company = db.register_company(conn, db.CompanyInput(cik=f"{number:010d}", name=f"Catalog fixture {number}"))
    filing = db.register_filing(conn, db.FilingInput(company_id=company,
        accession_number=f"{number:010d}-26-000001", form="10-Q", filing_date=date(2026,5,1)))
    sources = {}
    for role, extension in (("extracted_xbrl_instance", "xml"), ("main_inline_xbrl_filing", "html")):
        sources[extension] = db.register_source_document(conn, db.SourceDocumentInput(
            filing_id=filing, role=role, source_url=f"https://example.invalid/{number}/source.{extension}",
            local_path=f"catalog-fixture-{number}/source.{extension}", sha256=f"{number:064x}",
            size_bytes=100, retrieved_at=datetime(2026,5,2,tzinfo=timezone.utc)))
    fact_input = db.FactInput(filing_id=filing, source_document_id=sources["xml"],
        extraction_version="facts-v4", occurrence_key="revenue-1", concept_namespace="urn:fixture",
        concept_name="Revenue", numeric_value=Decimal("12"), raw_value="12", period_kind="duration",
        period_start=date(2026,1,1), period_end=date(2026,3,31))
    fact = db.store_fact(conn, fact_input)
    table_input = db.FinancialTableInput(filing_id=filing, source_document_id=sources["html"],
        extraction_version="tables-v2", table_key="income", statement_type="income_statement",
        structure={"rows": []}, readable_text="Fixture revenue 12")
    table = db.store_financial_table(conn, table_input)
    db.link_table_fact(conn, filing, table, fact)
    text = "MD&A 😀 revenue grew."
    report_input = db.ReportInput(filing_id=filing,source_document_id=sources["html"],
        extraction_version="reports-v3",full_text=text,
        sections=[{"section_id":"mda-1","title":"Management discussion","section_type":"mda",
            "part":"I","item":"2","offset_start":0,"offset_end":len(text),
            "source_locator":{"anchor":"mda"},"validation_status":"detected"}])
    report = db.store_report(conn, report_input)
    chunk_input = db.ChunkInput(filing_id=filing,report_id=report,chunking_version="chunks-v5",
        chunk_index=0,section="mda-1",text=text,offset_start=0,offset_end=len(text))
    chunk = db.store_chunk(conn,chunk_input)
    selection = ProcessingSetInput(filing_id=filing,set_key="fixture-excerpt-v1",
        fact_source_document_id=sources["xml"],fact_extraction_version="facts-v4",
        table_source_document_id=sources["html"],table_extraction_version="tables-v2",
        report_id=report,chunking_version="chunks-v5",
        coverage={"facts":"partial","tables":"partial","reports":"partial","chunks":"complete"},
        validation_status="passed",validation={"checks":{"fixture_consistency":True}},
        configuration={"fixture":True})
    return dict(filing=filing,sources=sources,fact=fact,table=table,report=report,chunk=chunk,
        fact_input=fact_input,table_input=table_input,report_input=report_input,chunk_input=chunk_input,selection=selection)


@unittest.skipUnless(os.environ.get("SEC_DB_TEST")=="1", "Set SEC_DB_TEST=1 for catalog database tests.")
class CatalogPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.options = local_test_options()
        with psycopg.connect(**cls.options) as conn:
            if conn.execute("SELECT to_regclass('sec.filing_processing_sets')").fetchone()[0] is None:
                raise RuntimeError("Migrate the dedicated test database first.")

    def setUp(self):
        self.conn=psycopg.connect(**self.options)
        self.addCleanup(self.conn.close)
        tx=self.conn.transaction(force_rollback=True)
        tx.__enter__()
        self.addCleanup(tx.__exit__,None,None,None)
        self.f=fixture(self.conn)

    def expect_rejected(self, action, error=psycopg.errors.CheckViolation):
        with self.assertRaises(error), self.conn.transaction():
            action()

    def test_preferred_views_show_one_explicit_coherent_partial_set(self):
        f=self.f
        second_fact = db.store_fact(self.conn,replace(f["fact_input"],extraction_version="facts-v6",numeric_value=Decimal("13")))
        selection_id=store_processing_set(self.conn,f["selection"])
        self.assertEqual(store_processing_set(self.conn,f["selection"]),selection_id)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sec.fact_catalog WHERE filing_id=%s",(f["filing"],)).fetchone()[0],0)
        select_preferred_processing_set(self.conn,f["filing"],selection_id)
        row=self.conn.execute("SELECT fact_id,extraction_version,stage_coverage FROM sec.fact_catalog WHERE filing_id=%s",(f["filing"],)).fetchone()
        self.assertEqual(row,(f["fact"],"facts-v4","partial"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sec.fact_provenance WHERE filing_id=%s",(f["filing"],)).fetchone()[0],2)
        self.assertEqual(self.conn.execute("SELECT section_type,text,section_validation_status FROM sec.section_catalog WHERE filing_id=%s",(f["filing"],)).fetchone(),
                         ("mda",f["report_input"].full_text,"detected"))
        self.assertEqual(self.conn.execute("SELECT section_type,extraction_version,chunking_version FROM sec.chunk_catalog WHERE filing_id=%s",(f["filing"],)).fetchone(),("mda","reports-v3","chunks-v5"))
        self.assertEqual(self.conn.execute("SELECT extraction_version,fact_extraction_version FROM sec.statement_catalog WHERE filing_id=%s",(f["filing"],)).fetchone(),("tables-v2","facts-v4"))
        counts=self.conn.execute("SELECT counts FROM sec.filing_overview WHERE filing_id=%s",(f["filing"],)).fetchone()[0]
        self.assertEqual(counts,dict(facts=1,tables=1,reports=1,chunks=1,table_fact_links=1,sections=1))
        next_selection=replace(f["selection"],set_key="facts-v6-only",fact_extraction_version="facts-v6",
            table_source_document_id=None,table_extraction_version=None,report_id=None,chunking_version=None,
            coverage={"facts":"partial","tables":"absent","reports":"absent","chunks":"absent"})
        next_id=store_processing_set(self.conn,next_selection)
        select_preferred_processing_set(self.conn,f["filing"],next_id)
        self.assertEqual(self.conn.execute("SELECT fact_id FROM sec.fact_catalog WHERE filing_id=%s",(f["filing"],)).fetchone()[0],second_fact)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sec.section_catalog WHERE filing_id=%s",(f["filing"],)).fetchone()[0],0)

    def test_missing_scope_wrong_filing_and_cross_version_links_are_rejected(self):
        f=self.f
        self.expect_rejected(lambda:store_processing_set(self.conn,replace(f["selection"],fact_extraction_version="missing")))
        db.store_fact(self.conn,replace(f["fact_input"],extraction_version="facts-v6"))
        self.expect_rejected(lambda:store_processing_set(self.conn,replace(f["selection"],fact_extraction_version="facts-v6")))
        other=fixture(self.conn,72)
        self.expect_rejected(lambda:store_processing_set(self.conn,replace(f["selection"],report_id=other["report"])))
        self.conn.execute("UPDATE sec.financial_tables SET structure=%s WHERE id=%s",
            (Jsonb({"rows":[],"fact_scope":{"xml_document_id":str(f["sources"]["xml"]),"facts_version":"wrong"}}),f["table"]))
        self.expect_rejected(lambda:store_processing_set(self.conn,f["selection"]))

    def test_validation_required_for_preference_and_sets_are_immutable(self):
        f=self.f
        pending=store_processing_set(self.conn,replace(f["selection"],validation_status="unvalidated",validation={}))
        self.expect_rejected(lambda:select_preferred_processing_set(self.conn,f["filing"],pending))
        with self.assertRaises(db.DataConflict):
            store_processing_set(self.conn,replace(f["selection"],configuration={"changed":True}))
        self.expect_rejected(lambda:self.conn.execute("UPDATE sec.filing_processing_sets SET validation_status='passed' WHERE id=%s",(pending,)))
        self.expect_rejected(lambda:self.conn.execute("DELETE FROM sec.filing_processing_sets WHERE id=%s",(pending,)))
        passed=store_processing_set(self.conn,replace(f["selection"],set_key="checked-v2"))
        select_preferred_processing_set(self.conn,f["filing"],passed)
        # A boolean-ish integer must not manufacture a pass through direct SQL.
        self.expect_rejected(lambda:self.conn.execute("""INSERT INTO sec.filing_processing_sets
            (filing_id,set_key,report_id,coverage,validation_status,validation,configuration)
            VALUES (%s,'false-pass',%s,%s,'passed',%s,'{}')""",
            (f["filing"],f["report"],Jsonb({"facts":"absent","tables":"absent","reports":"partial","chunks":"absent"}),Jsonb({"checks":{"x":1}}))))

    def test_seals_allow_exact_replay_and_block_new_rows_edits_and_deletes(self):
        f=self.f
        store_processing_set(self.conn,f["selection"])
        self.assertEqual(db.store_fact(self.conn,f["fact_input"]),f["fact"])
        self.assertEqual(db.store_financial_table(self.conn,f["table_input"]),f["table"])
        self.assertEqual(db.store_report(self.conn,f["report_input"]),f["report"])
        self.assertEqual(db.store_chunk(self.conn,f["chunk_input"]),f["chunk"])
        db.link_table_fact(self.conn,f["filing"],f["table"],f["fact"])
        actions=[
            lambda:db.store_fact(self.conn,replace(f["fact_input"],occurrence_key="additional")),
            lambda:db.store_financial_table(self.conn,replace(f["table_input"],table_key="additional")),
            lambda:db.store_chunk(self.conn,replace(f["chunk_input"],chunk_index=1)),
            lambda:self.conn.execute("UPDATE sec.financial_facts SET numeric_value=999 WHERE id=%s",(f["fact"],)),
            lambda:self.conn.execute("DELETE FROM sec.financial_table_facts WHERE table_id=%s",(f["table"],)),
            lambda:self.conn.execute("TRUNCATE sec.financial_facts CASCADE"),
            lambda:self.conn.execute("DELETE FROM sec.report_sections WHERE report_id=%s",(f["report"],)),
            lambda:self.conn.execute("UPDATE sec.reports SET sections='[]' WHERE id=%s",(f["report"],)),
            lambda:self.conn.execute("UPDATE sec.source_documents SET sha256=%s WHERE id=%s",("f"*64,f["sources"]["xml"])),
        ]
        for action in actions:
            self.expect_rejected(action)
        self.conn.execute("UPDATE sec.source_documents SET local_path='moved.xml',storage_uri='s3://example/moved.xml',title='Stored source' WHERE id=%s",(f["sources"]["xml"],))
        self.assertNotEqual(db.store_fact(self.conn,replace(f["fact_input"],extraction_version="facts-next")),f["fact"])


@unittest.skipUnless(os.environ.get("SEC_DB_CONCURRENCY_TEST")=="1", "Set SEC_DB_CONCURRENCY_TEST=1 for disposable local database concurrency tests.")
class CatalogConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        context=disposable_catalog_database()
        cls.options=context.__enter__()
        cls.addClassCleanup(context.__exit__,None,None,None)
        with psycopg.connect(**cls.options) as conn:
            cls.f=fixture(conn,73)

    def test_waiting_writer_rechecks_committed_seal_and_exact_replay_still_works(self):
        started=Event(); finished=Event(); result=[]
        def writer():
            try:
                with psycopg.connect(**self.options) as conn:
                    conn.execute("SET statement_timeout='10s'")
                    started.set()
                    db.store_fact(conn,replace(self.f["fact_input"],occurrence_key="late-fact"))
                result.append("unexpected success")
            except Exception as exc:
                result.append(exc)
            finally:
                finished.set()
        thread=Thread(target=writer)
        with psycopg.connect(**self.options) as publisher:
            selection=store_processing_set(publisher,self.f["selection"])
            thread.start()
            self.assertTrue(started.wait(3))
            self.assertFalse(finished.wait(0.25),"Writer did not wait for publication lock")
            select_preferred_processing_set(publisher,self.f["filing"],selection)
        self.assertTrue(finished.wait(10))
        thread.join(timeout=1)
        self.assertEqual(len(result),1)
        self.assertIsInstance(result[0],psycopg.errors.CheckViolation)
        with psycopg.connect(**self.options) as conn:
            self.assertEqual(db.store_fact(conn,self.f["fact_input"]),self.f["fact"])
            self.assertEqual(store_processing_set(conn,self.f["selection"]),selection)
            self.assertEqual(conn.execute("SELECT counts->>'facts' FROM sec.filing_overview WHERE filing_id=%s",(self.f["filing"],)).fetchone()[0],"1")

    def test_fixed_snapshot_writes_are_explicitly_rejected(self):
        for isolation in (psycopg.IsolationLevel.REPEATABLE_READ,psycopg.IsolationLevel.SERIALIZABLE):
            with psycopg.connect(**self.options) as conn:
                conn.isolation_level=isolation
                with self.assertRaises(psycopg.errors.FeatureNotSupported):
                    db.store_fact(conn,replace(self.f["fact_input"],extraction_version="new-isolation"))
                conn.rollback()


if __name__=="__main__":
    unittest.main()
