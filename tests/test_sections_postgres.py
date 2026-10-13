"""Opt-in PostgreSQL section constraints; fixtures always roll back.

Use SEC_DB_TEST=1 and a migrated SEC_DB_TEST_DATABASE ending in _test.
These tests never create, drop, truncate, or commit domain data.
"""

from dataclasses import replace
from datetime import date, datetime, timezone
import os
from pathlib import Path
import re
import unittest
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from sec_pipeline import database as db
from sec_pipeline.sections import store_report_sections


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("SEC_DB_TEST") == "1", "Set SEC_DB_TEST=1 for live database tests.")
class SectionsPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_name = os.environ.get("SEC_DB_TEST_DATABASE", "sec_filings_test")
        if not re.fullmatch(r"[A-Za-z0-9_]+_test(?:_[A-Za-z0-9_]+)?", cls.database_name):
            raise RuntimeError("Section tests require a dedicated database name containing _test.")
        if cls.database_name == db.connection_settings(ROOT / ".env")["dbname"]:
            raise RuntimeError("Test database must differ from the configured application database.")
        with db.connect(ROOT / ".env", dbname=cls.database_name) as conn:
            if conn.execute("SELECT to_regclass('sec.report_sections')").fetchone()[0] is None:
                raise RuntimeError("Apply migrations to the dedicated test database first.")

    def setUp(self):
        self.conn = db.connect(ROOT / ".env", dbname=self.database_name)
        self.addCleanup(self.conn.close)
        transaction = self.conn.transaction(force_rollback=True)
        transaction.__enter__()
        self.addCleanup(transaction.__exit__, None, None, None)

    def fixture(self, number=811, version="sections-v1", sections=True):
        company = db.register_company(self.conn, db.CompanyInput(cik=f"{number:010d}", name="Section test"))
        filing = db.register_filing(self.conn, db.FilingInput(
            company_id=company, accession_number=f"{number:010d}-26-000001", form="10-Q",
            filing_date=date(2026, 10, 1)))
        source = db.register_source_document(self.conn, db.SourceDocumentInput(
            filing_id=filing, role="main_inline_xbrl_filing", source_url="https://example.invalid/section.htm",
            local_path="section.htm", sha256="a" * 64, size_bytes=100,
            retrieved_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
        text = "😀风险 Café\u0301\nGrowth.\nOther."
        end = text.index("Other.")
        descriptors = [{"section_id": "mda", "title": "MD&A", "section_type": "mda",
                        "part": "I", "item": "2", "validation_status": "detected",
                        "offset_start": 0, "offset_end": end},
                       {"section_id": "other", "title": "Other", "offset_start": end,
                        "offset_end": len(text)}] if sections else []
        report = db.ReportInput(filing_id=filing, source_document_id=source,
                                extraction_version=version, full_text=text, sections=descriptors)
        report_id = db.store_report(self.conn, report)
        ids = store_report_sections(self.conn, report, report_id=report_id)
        return report, report_id, ids

    def chunk_sql(self, report, report_id, section_id, start=0, end=4):
        return self.conn.execute(
            """INSERT INTO sec.chunks
               (filing_id,report_id,chunking_version,chunk_index,text,offset_start,offset_end,section_id)
               VALUES (%s,%s,'sections-test',0,%s,%s,%s,%s) RETURNING id""",
            (report.filing_id, report_id, report.full_text[start:end], start, end, section_id)).fetchone()[0]

    def test_sections_replay_idempotently_with_unicode_offsets(self):
        report, report_id, ids = self.fixture()
        self.assertEqual(ids, store_report_sections(self.conn, report, report_id=report_id))
        self.assertEqual(self.conn.execute(
            "SELECT section_type, validation_status FROM sec.report_sections WHERE id=%s",
            (ids["mda"],)).fetchone(), ("mda", "detected"))
        self.chunk_sql(report, report_id, ids["mda"])
        self.assertEqual(self.conn.execute("SELECT text FROM sec.chunks").fetchone()[0], report.full_text[:4])

    def test_chunk_cannot_link_to_another_report_version(self):
        report, report_id, _ = self.fixture()
        _, _, other = self.fixture(version="sections-v2")
        with self.assertRaises(psycopg.errors.ForeignKeyViolation), self.conn.transaction():
            self.chunk_sql(report, report_id, other["mda"])

    def test_chunk_cannot_link_to_another_filing(self):
        report, report_id, _ = self.fixture()
        _, _, other = self.fixture(number=812)
        with self.assertRaises(psycopg.errors.ForeignKeyViolation), self.conn.transaction():
            self.chunk_sql(report, report_id, other["mda"])

    def test_chunk_crossing_section_boundary_is_rejected(self):
        report, report_id, ids = self.fixture()
        with self.assertRaises(psycopg.errors.CheckViolation), self.conn.transaction():
            self.chunk_sql(report, report_id, ids["mda"], end=len(report.full_text))

    def test_report_edit_cannot_corrupt_section_content_without_any_chunks(self):
        _, report_id, _ = self.fixture()
        with self.assertRaises(psycopg.errors.CheckViolation), self.conn.transaction():
            self.conn.execute("UPDATE sec.reports SET full_text='changed' WHERE id=%s", (report_id,))

    def test_overlapping_sections_and_out_of_range_sections_are_rejected(self):
        report, report_id, _ = self.fixture()
        for start, end in ((2, 5), (len(report.full_text), len(report.full_text) + 1)):
            with self.subTest(start=start), self.assertRaises(psycopg.errors.CheckViolation), self.conn.transaction():
                self.conn.execute("""INSERT INTO sec.report_sections
                    (filing_id,report_id,section_key,original_title,section_order,offset_start,offset_end)
                    VALUES (%s,%s,'extra','Extra',2,%s,%s)""", (report.filing_id, report_id, start, end))

    def test_section_shrink_cannot_invalidate_linked_chunks(self):
        report, report_id, ids = self.fixture()
        self.chunk_sql(report, report_id, ids["mda"], end=6)
        with self.assertRaises(psycopg.errors.CheckViolation), self.conn.transaction():
            self.conn.execute("UPDATE sec.report_sections SET offset_end=4 WHERE id=%s", (ids["mda"],))

    def test_report_input_cannot_be_used_with_an_unrelated_report_id(self):
        report, _, _ = self.fixture()
        _, other_id, _ = self.fixture(number=812)
        with self.assertRaises(db.DataConflict):
            store_report_sections(self.conn, report, report_id=other_id)

    def test_changed_section_payload_never_overwrites_existing_rows(self):
        report, report_id, ids = self.fixture()
        changed = replace(report, sections=[report.sections[0] | {"title": "Changed"}, report.sections[1]])
        with self.assertRaises(db.DataConflict):
            store_report_sections(self.conn, changed, report_id=report_id)
        self.assertEqual(self.conn.execute("SELECT original_title FROM sec.report_sections WHERE id=%s",
                                          (ids["mda"],)).fetchone()[0], "MD&A")

    def test_legacy_chunks_without_sections_remain_usable(self):
        report, report_id, ids = self.fixture(sections=False)
        self.assertEqual(ids, {})
        self.chunk_sql(report, report_id, None)
        self.assertIsNone(self.conn.execute("SELECT section_id FROM sec.chunks").fetchone()[0])

    def test_unpublished_report_deletion_removes_its_sections(self):
        _, report_id, _ = self.fixture()
        self.conn.execute("DELETE FROM sec.reports WHERE id=%s", (report_id,))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sec.report_sections WHERE report_id=%s",
                                          (report_id,)).fetchone()[0], 0)

    def test_legacy_backfill_preserves_unverified_labels_and_skips_invalid_ranges(self):
        # Apply both migrations inside a separate rollback-only namespace. This
        # exercises an actual upgrade without touching the migrated sec schema.
        namespace = "sec_upgrade_" + uuid4().hex
        def migration(name):
            return (ROOT / "src/sec_pipeline/migrations" / name).read_text().replace(
                "sec.", namespace + ".").replace("SCHEMA IF NOT EXISTS sec;", f"SCHEMA IF NOT EXISTS {namespace};")
        self.conn.execute(migration("001_initial.sql"))
        company = self.conn.execute(f"INSERT INTO {namespace}.companies(cik,name) VALUES ('0000000901','Legacy') RETURNING id").fetchone()[0]
        filing = self.conn.execute(f"""INSERT INTO {namespace}.filings
            (company_id,accession_number,form,filing_date) VALUES (%s,'0000000901-26-000001','10-Q','2026-10-01') RETURNING id""",
            (company,)).fetchone()[0]
        source = self.conn.execute(f"""INSERT INTO {namespace}.source_documents
            (filing_id,role,source_url,local_path,sha256,size_bytes,retrieved_at)
            VALUES (%s,'filing','https://example.invalid/legacy','legacy.htm',%s,1,now()) RETURNING id""",
            (filing, "a" * 64)).fetchone()[0]
        text = "😀alpha beta"
        descriptors = [
            {"section_id": "mda", "title": "Management discussion", "offset_start": 0, "offset_end": 6},
            {"title": "Other", "offset_start": 6, "offset_end": len(text)},
            {"section_id": "bad", "offset_start": 0, "offset_end": 10 ** 100},
            {"section_id": "mda", "offset_start": 0, "offset_end": 6},
            {"section_id": "overlap", "offset_start": 2, "offset_end": 8},
            {"section_id": "boolean", "offset_start": True, "offset_end": 3}, 42,
        ]
        report_id = self.conn.execute(f"""INSERT INTO {namespace}.reports
            (filing_id,source_document_id,extraction_version,full_text,sections)
            VALUES (%s,%s,'legacy',%s,%s) RETURNING id""", (filing, source, text, Jsonb(descriptors))).fetchone()[0]
        for index, end in enumerate((4, len(text))):
            self.conn.execute(f"""INSERT INTO {namespace}.chunks
                (filing_id,report_id,chunking_version,chunk_index,section,text,offset_start,offset_end)
                VALUES (%s,%s,'legacy',%s,'mda',%s,0,%s)""", (filing, report_id, index, text[:end], end))
        self.conn.execute(migration("002_report_sections.sql"))
        self.assertEqual(self.conn.execute(f"SELECT section_key,section_type,validation_status FROM {namespace}.report_sections ORDER BY section_order").fetchall(),
                         [("mda", "unknown", "unverified"), ("legacy-section-1", "unknown", "unverified")])
        self.assertEqual(self.conn.execute(f"SELECT section_id IS NOT NULL FROM {namespace}.chunks ORDER BY chunk_index").fetchall(), [(True,), (False,)])


if __name__ == "__main__":
    unittest.main()
