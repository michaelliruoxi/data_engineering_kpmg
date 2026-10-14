"""Bounded V2 heading evidence, including cached real source regressions."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from lxml import etree, html

from sec_pipeline.statement_headings import recover_headings, statement_kind


ROOT = Path(__file__).resolve().parents[1]
DATA = '<table id="data"><tr><td><ix:nonFraction name="us-gaap:Revenues">42</ix:nonFraction></td></tr></table>'


def document(markup):
    return html.document_fromstring(markup).getroottree()


def headings(markup):
    tree = document(markup)
    table = tree.xpath('//table[@id="data"]')[0]
    before = etree.tostring(tree)
    result = recover_headings(table)
    if etree.tostring(tree) != before:
        raise AssertionError("Heading inspection changed the source DOM")
    return result


class HeadingGrammarTests(unittest.TestCase):
    def test_observed_exact_title_variants(self):
        titles = {
            "CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS — UNAUDITED": "income_statement",
            "Consolidated Statements of Operations (In millions, except per share data)": "income_statement",
            "Condensed Consolidated Statements of Operations and Comprehensive Income (Loss)": "income_statement",
            "STATEMENTS OF CONSOLIDATED INCOME": "income_statement",
            "Consolidated Statements ofIncome": "income_statement",
            "CONDENSED CONSOLIDATED STATEMENTS OF FINANCIAL POSITION": "balance_sheet",
            "Consolidated Condensed Statements of Cash Flows": "cash_flow",
        }
        for value, expected in titles.items():
            with self.subTest(value=value):
                self.assertEqual(statement_kind(value), expected)

    def test_prose_parent_and_supplemental_titles_are_not_primary_headings(self):
        for value in (
            "See Consolidated Statements of Income for more information",
            "Notes to Consolidated Statements of Income", "Selected Financial Statements of Income",
            "Parent Company Statements of Income", "Supplemental Consolidating Statements of Income",
            "Statements of Income for Our Subsidiary", "Income Tax Expense",
            "Consolidated Statements of Income (Parent Company, Unaudited)",
        ):
            with self.subTest(value=value):
                self.assertIsNone(statement_kind(value))


class BoundedHeadingTests(unittest.TestCase):
    def test_fifty_empty_anchors_do_not_hide_the_nearest_title(self):
        markup = '<p>CONSOLIDATED STATEMENTS OF OPERATIONS</p><p>(unaudited)</p>' + '<a name="anchor"></a>' * 50 + DATA
        result = headings(markup)
        self.assertEqual([item.kind for item in result], ["income_statement"])
        self.assertEqual(result[0].text, "CONSOLIDATED STATEMENTS OF OPERATIONS")
        self.assertTrue(result[0].consolidated)
        self.assertTrue(result[0].source_xpath.endswith("/p[1]"))

    def test_empty_anchor_budget_is_finite(self):
        self.assertEqual(headings('<p>Statements of Income</p>' + '<a></a>' * 129 + DATA), ())

    def test_units_and_period_qualifiers_allow_bounded_wrapper_ascent(self):
        markup = ('<p>THE COOPER COMPANIES, INC. AND SUBSIDIARIES</p>'
                  '<p>Consolidated Condensed Statements of Cash Flows</p>'
                  '<p>Nine Months Ended July 31,</p>'
                  '<div><span>(In millions, unaudited)</span>' + DATA + '</div>')
        self.assertEqual([item.kind for item in headings(markup)], ["cash_flow"])
        dell = '<p>CONDENSED CONSOLIDATED STATEMENTS OF FINANCIAL POSITION</p><div><span>(in millions; unaudited)</span>' + DATA + '</div>'
        self.assertEqual([item.kind for item in headings(dell)], ["balance_sheet"])

    def test_header_rows_preserve_company_and_title_node_boundaries(self):
        markup = '''<table id="data"><tr><td><p>INTUIT INC.</p><p>CONSOLIDATED STATEMENTS OF OPERATIONS</p></td></tr>
            <tr><td>(In millions, except per share data)</td></tr>
            <tr><td><ix:nonFraction>42</ix:nonFraction></td></tr></table>'''
        result = headings(markup)
        self.assertEqual([item.kind for item in result], ["income_statement"])
        self.assertEqual(result[0].text, "CONSOLIDATED STATEMENTS OF OPERATIONS")

    def test_company_title_unit_container_retains_the_actual_title(self):
        markup = '''<div><p>Lennar Corporation and Subsidiaries</p>
            <p>Condensed Consolidated Statements of Operations and Comprehensive Income (Loss)</p>
            <p>(In thousands, except per share amounts)</p><p>(Unaudited)</p></div><div>''' + DATA + '</div>'
        result = headings(markup)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "income_statement")
        self.assertIn("Comprehensive Income (Loss)", result[0].text)

    def test_text_only_layout_table_may_supply_exact_heading(self):
        markup = '<div><table><tr><td>CONDENSED CONSOLIDATED STATEMENT OF INCOME</td></tr></table></div><div><div>' + DATA + '</div></div>'
        self.assertEqual([item.kind for item in headings(markup)], ["income_statement"])

    def test_preceding_financial_table_is_a_hard_boundary(self):
        first = '<table><tr><td><ix:nonFraction>99</ix:nonFraction></td></tr></table>'
        self.assertEqual(headings('<p>Consolidated Statements of Income</p>' + first + DATA), ())
        untagged = '<table><tr><td>Consolidated Statements of Income</td><td>99</td></tr></table>'
        self.assertEqual(headings(untagged + DATA), ())

    def test_note_prose_and_embedded_title_link_cannot_lend_a_heading(self):
        for barrier in ('<p>Note 12. Segment reporting</p>', '<p>Revenue by operating segment is shown below.</p>',
                        '<p>See the <span>Consolidated Statements of Income</span> for more information.</p>'):
            with self.subTest(barrier=barrier):
                self.assertEqual(headings('<p>Consolidated Statements of Income</p>' + barrier + DATA), ())

    def test_parent_and_supplemental_header_rows_reject_primary_title(self):
        for label in ("Parent Company Only", "Supplemental Consolidating Data", "Selected Financial Data"):
            markup = f'<table id="data"><tr><td>{label}</td></tr><tr><td>Statements of Income</td></tr><tr><td><ix:nonFraction>42</ix:nonFraction></td></tr></table>'
            with self.subTest(label=label):
                self.assertEqual(headings(markup), ())

    def test_conflicting_heading_kinds_are_ambiguous(self):
        markup = '<div><p>Consolidated Statements of Income</p><p>Consolidated Balance Sheets</p></div>' + DATA
        self.assertEqual(headings(markup), ())
        own = '<table id="data"><tr><td>Statements of Income</td></tr><tr><td>Balance Sheets</td></tr><tr><td><ix:nonFraction>42</ix:nonFraction></td></tr></table>'
        self.assertEqual(headings(own), ())

    def test_heading_after_first_numeric_fact_is_not_a_header(self):
        markup = '<table id="data"><tr><td><ix:nonFraction>42</ix:nonFraction></td></tr><tr><td>Statements of Income</td></tr></table>'
        self.assertEqual(headings(markup), ())

    def test_hidden_title_cannot_promote_a_table(self):
        self.assertEqual(headings('<div style="display:none">Statements of Income</div>' + DATA), ())

    def test_continue_marker_is_metadata_not_an_invented_new_title(self):
        result = headings('<p>Consolidated Statements of Cash Flows (Continued)</p>' + DATA)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].continued)
        self.assertEqual(result[0].signature, "consolidated statements of cash flows")


class CachedSourceHeadingTests(unittest.TestCase):
    def test_all_ten_observed_primary_heading_layout_regressions(self):
        cases = (
            ("0001628280-26-064557", "/html/body/div[66]/table", "income_statement"),
            ("0001104659-26-108926", "/html/body/div[5]/div[2]/table[2]", "income_statement"),
            ("0001730168-26-000080", "/html/body/div[58]/table", "income_statement"),
            ("0001327567-26-000023", "/html/body/div[904]/table", "income_statement"),
            ("0001628280-26-061160", "/html/body/div[81]/table", "cash_flow"),
            ("0000896878-26-000037", "/html/body/div[833]/table", "income_statement"),
            ("0001571996-26-000046", "/html/body/div[64]/table", "balance_sheet"),
            ("0001104659-26-102442", "/html/body/div[9]/div[2]/table", "income_statement"),
            ("0001104659-26-102213", "/html/body/div[5]/div[2]/table", "income_statement"),
            ("0001104659-26-101932", "/html/body/div[17]/div[2]/table", "income_statement"),
        )
        checked = 0
        for accession, xpath, expected in cases:
            manifest_path = ROOT / "data/raw/sec" / accession / "source_manifest.json"
            if not manifest_path.exists():
                continue
            with self.subTest(accession=accession):
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                primary = next(row for row in manifest["files"] if row["role"] == "main_inline_xbrl_filing")
                tree = html.document_fromstring((ROOT / primary["local_path"]).read_bytes(), parser=html.HTMLParser(encoding="utf-8")).getroottree()
                tables = tree.xpath(xpath)
                self.assertEqual(len(tables), 1)
                result = recover_headings(tables[0])
                self.assertEqual([item.kind for item in result], [expected])
                checked += 1
        if not checked:
            self.skipTest("Optional representative live source archives are not present.")


class VersionedHeadingIntegrationTests(unittest.TestCase):
    def test_v2_recovers_new_exact_title_without_changing_v1_recognition(self):
        from sec_pipeline.tables import TableExtractionError, select_statement_segments, select_statements
        from tests.test_statement_segments import fixture as segments_fixture

        tree, facts = segments_fixture()
        income = tree.xpath('//table[@id="income"]')[0]
        income.getprevious().text = "STATEMENTS OF CONSOLIDATED INCOME"
        with self.assertRaisesRegex(TableExtractionError, "income_statement"):
            select_statements(tree)
        selected = select_statement_segments(tree, facts)
        self.assertEqual([item.table_key for item in selected],
                         ["income_statement", "balance_sheet", "cash_flow.segment_1", "cash_flow.segment_2"])
        self.assertIs(selected[0].element, income)

    def test_heading_recovery_does_not_replace_required_concepts_or_parent_guard(self):
        from sec_pipeline.tables import TableExtractionError, select_statement_segments
        from tests.test_statement_segments import fixture as segments_fixture

        for failure in ("missing_concept", "parent_context"):
            tree, facts = segments_fixture()
            tree.xpath('//table[@id="income"]')[0].getprevious().text = "STATEMENTS OF CONSOLIDATED INCOME"
            if failure == "missing_concept":
                tree.xpath('//*[@id="income-0"]')[0].set("name", "g:OtherRevenue")
                facts["income-0"]["concept"] = facts["income-0"]["concept"].replace("Revenues", "OtherRevenue")
            else:
                facts["income-0"]["dimensions"] = [{"kind": "explicit", "dimension": "{http://fasb.org/us-gaap/2025}LegalEntityAxis",
                    "member": "{http://fasb.org/us-gaap/2025}ParentCompanyMember"}]
            with self.subTest(failure=failure), self.assertRaises(TableExtractionError):
                select_statement_segments(tree, facts)

    def test_existing_v1_and_v2_heading_successes_never_call_recovery(self):
        from sec_pipeline.tables import select_statement_segments
        from tests.test_statement_segments import fixture as segments_fixture
        from tests.test_statement_selection import filing

        with patch("sec_pipeline.statement_headings.recover_headings", side_effect=AssertionError("Legacy success must be preserved")):
            self.assertEqual(len(select_statement_segments(filing(), {})), 3)
            tree, facts = segments_fixture()
            self.assertEqual(len(select_statement_segments(tree, facts)), 4)


if __name__ == "__main__":
    unittest.main()
