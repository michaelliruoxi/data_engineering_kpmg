
from pathlib import Path

from lxml import html
import pytest

from sec_pipeline.report_cleaning import (
    clean_report,
    detect_sections,
)


# Location of the Microsoft 10-Q sample
SAMPLE_PATH = Path(
    "data/raw/sec/0001193125-26-191507/msft-20260331.htm"
)


def test_clean_report_extracts_text():
    """Check that HTML is converted into readable text."""
    report = clean_report(SAMPLE_PATH)

    assert len(report.full_text) > 0
    assert "FINANCIAL INFORMATION" in report.full_text
    assert "INCOME STATEMENTS" in report.full_text
    assert "<html" not in report.full_text.lower()


def test_detect_sections():
    """Check that important SEC sections are detected."""
    report = clean_report(SAMPLE_PATH)

    section_ids = {
        section["section_id"]
        for section in report.sections
    }

    assert "front-matter" in section_ids
    assert "part-i" in section_ids
    assert "part-i-item-1" in section_ids
    assert "part-i-item-2" in section_ids
    assert "part-ii" in section_ids
    assert "part-ii-item-1a" in section_ids


def test_section_offsets():
    """Check that sections cover the report without gaps."""
    report = clean_report(SAMPLE_PATH)

    sections = report.sections

    assert len(sections) > 0
    assert sections[0]["offset_start"] == 0
    assert sections[-1]["offset_end"] == len(report.full_text)

    for current, following in zip(sections, sections[1:]):
        assert current["offset_end"] == following["offset_start"]

    for section in sections:
        start = section["offset_start"]
        end = section["offset_end"]

        assert 0 <= start < end <= len(report.full_text)


def test_section_titles_match_offsets():
    """Check that detected sections start at the right text."""
    report = clean_report(SAMPLE_PATH)

    expected = {
        "part-i": "PART I. FINANCIAL INFORMATION",
        "part-i-item-1": "ITEM 1. FINANCIAL STATEMENTS",
        "part-ii": "PART II. OTHER INFORMATION",
        "part-ii-item-1a": "ITEM 1A. RISK FACTORS",
    }

    for section in report.sections:
        section_id = section["section_id"]

        if section_id in expected:
            start = section["offset_start"]
            actual_text = report.full_text[start:start + 150]

            assert actual_text.startswith(expected[section_id])


def test_empty_text():
    """Check how the detector handles empty input."""
    assert detect_sections("") == []


def test_unknown_structure():
    """Check fallback for a document without SEC headings."""
    text = "This document has no SEC section headings."

    sections = detect_sections(text)

    assert len(sections) == 1
    assert sections[0]["section_id"] == "full-report"
    assert sections[0]["offset_start"] == 0
    assert sections[0]["offset_end"] == len(text)

def test_part_i_skips_table_of_contents():
    """Part I should start in the body, not the table of contents."""

    text = "\n".join([
        "TABLE OF CONTENTS",
        "PART I. FINANCIAL INFORMATION",
        "ITEM 1. FINANCIAL STATEMENTS",
        "ITEM 2. MANAGEMENT'S DISCUSSION",
        "PART II. OTHER INFORMATION",
        "",
        "INTRODUCTION TO THE REPORT",
        "Some introductory text.",
        "",
        "PART I. FINANCIAL INFORMATION",
        "ITEM 1. FINANCIAL STATEMENTS",
        "Actual financial statement content.",
        "ITEM 2. MANAGEMENT'S DISCUSSION",
        "Actual management discussion.",
        "PART II. OTHER INFORMATION",
        "ITEM 1. LEGAL PROCEEDINGS",
    ])

    sections = detect_sections(text)

    part_i = next(
        section
        for section in sections
        if section["section_id"] == "part-i"
    )

    expected_start = text.rfind("PART I. FINANCIAL INFORMATION")

    assert part_i["offset_start"] == expected_start

def test_part_heading_without_period():
    """Detect Part headings even when the period is missing."""

    text = "\n".join([
        "PART I FINANCIAL INFORMATION",
        "ITEM 1. FINANCIAL STATEMENTS",
        "Financial statement content.",
        "PART II OTHER INFORMATION",
        "ITEM 1. LEGAL PROCEEDINGS",
        "Legal proceedings content.",
    ])

    sections = detect_sections(text)

    section_ids = {
        section["section_id"]
        for section in sections
    }

    assert "part-i" in section_ids
    assert "part-ii" in section_ids
    assert "part-i-item-1" in section_ids
    assert "part-ii-item-1" in section_ids

def test_part_ii_items_3_and_4():
    """Detect Part II Item 3 and Item 4 headings."""

    text = "\n".join([
        "PART I. FINANCIAL INFORMATION",
        "ITEM 1. FINANCIAL STATEMENTS",
        "Financial statement content.",
        "PART II. OTHER INFORMATION",
        "ITEM 3. DEFAULTS UPON SENIOR SECURITIES",
        "No defaults.",
        "ITEM 4. MINE SAFETY DISCLOSURES",
        "Not applicable.",
    ])

    sections = detect_sections(text)

    section_ids = {
        section["section_id"]
        for section in sections
    }

    assert "part-ii-item-3" in section_ids
    assert "part-ii-item-4" in section_ids

def test_part_heading_with_dash():
    """Detect Part headings separated by hyphens or en dashes."""

    for separator in ["-", "–"]:
        text = "\n".join([
            f"PART I {separator} FINANCIAL INFORMATION",
            "ITEM 1. FINANCIAL STATEMENTS",
            "Financial statement content.",
            f"PART II {separator} OTHER INFORMATION",
            "ITEM 1. LEGAL PROCEEDINGS",
            "Legal proceedings content.",
        ])

        sections = detect_sections(text)

        section_ids = {
            section["section_id"]
            for section in sections
        }

        assert "part-i" in section_ids
        assert "part-ii" in section_ids
        assert "part-i-item-1" in section_ids
        assert "part-ii-item-1" in section_ids

def test_part_heading_with_em_dash_and_no_spaces():
    """Detect Part headings with different dash separators."""

    for separator in ["-", "–", "—"]:
        text = "\n".join([
            f"PART I{separator}FINANCIAL INFORMATION",
            "ITEM 1. FINANCIAL STATEMENTS",
            "Financial statement content.",
            f"PART II{separator}OTHER INFORMATION",
            "ITEM 1. LEGAL PROCEEDINGS",
            "Legal proceedings content.",
        ])

        sections = detect_sections(text)

        section_ids = {
            section["section_id"]
            for section in sections
        }

        assert "part-i" in section_ids
        assert "part-i-item-1" in section_ids
        assert "part-ii" in section_ids
        assert "part-ii-item-1" in section_ids

def test_item_1_prefixed_financial_statements():
    """Detect Item 1 financial statements with common prefixes."""

    titles = [
        "FINANCIAL STATEMENTS",
        "CONDENSED CONSOLIDATED FINANCIAL STATEMENTS",
        "UNAUDITED CONDENSED FINANCIAL STATEMENTS",
    ]

    for title in titles:
        text = "\n".join([
            "PART I. FINANCIAL INFORMATION",
            f"ITEM 1. {title}",
            "Financial statement content.",
            "PART II. OTHER INFORMATION",
            "ITEM 1. LEGAL PROCEEDINGS",
            "Legal proceedings content.",
        ])

        sections = detect_sections(text)

        section_ids = {
            section["section_id"]
            for section in sections
        }

        assert "part-i-item-1" in section_ids, title

def test_clean_report_preserves_br_line_breaks(tmp_path):
    """Preserve line breaks represented by HTML br tags."""

    html = """
    <html>
      <body>
        <p>Three Months Ended<br/>March 31, 2026</p>
      </body>
    </html>
    """

    html_path = tmp_path / "test_br.html"
    html_path.write_text(html, encoding="utf-8")

    report = clean_report(html_path)

    assert "Three Months Ended\nMarch 31, 2026" in report.full_text

def test_clean_report_preserves_table_cells_on_same_row(tmp_path):
    """Keep table cells on the same line when they contain paragraphs."""

    html = """
    <html>
      <body>
        <table>
          <tr>
            <td><p>Revenue</p></td>
            <td><p>10</p></td>
          </tr>
        </table>
      </body>
    </html>
    """

    html_path = tmp_path / "test_table_cells.html"
    html_path.write_text(html, encoding="utf-8")

    report = clean_report(html_path)

    assert "Revenue | 10" in report.full_text

def test_part_i_skips_index_table_of_contents():
    """Select the body Part I instead of an INDEX entry."""

    text = "\n".join([
        "INDEX",
        "PART I. FINANCIAL INFORMATION",
        "ITEM 1. FINANCIAL STATEMENTS",
        "ITEM 2. MANAGEMENT'S DISCUSSION",
        "PART II. OTHER INFORMATION",
        "ITEM 1. LEGAL PROCEEDINGS",
        "PART I. FINANCIAL INFORMATION",
        "ITEM 1. CONDENSED CONSOLIDATED FINANCIAL STATEMENTS",
        "Actual financial statement content.",
        "ITEM 2. MANAGEMENT'S DISCUSSION",
        "Actual management discussion content.",
        "PART II. OTHER INFORMATION",
        "ITEM 1. LEGAL PROCEEDINGS",
        "Actual legal proceedings content.",
    ])

    sections = detect_sections(text)

    part_i = next(
        section
        for section in sections
        if section["section_id"] == "part-i"
    )

    expected_start = text.rfind(
        "PART I. FINANCIAL INFORMATION"
    )

    assert part_i["offset_start"] == expected_start

    part_ii = next(
        section
        for section in sections
        if section["section_id"] == "part-ii"
    )

    expected_part_ii_start = text.rfind(
        "PART II. OTHER INFORMATION"
    )

    assert part_ii["offset_start"] == expected_part_ii_start

    item_1 = next(
        section
        for section in sections
        if section["section_id"] == "part-i-item-1"
    )

    expected_item_1_start = text.index(
        "ITEM 1. CONDENSED CONSOLIDATED FINANCIAL STATEMENTS"
    )

    assert item_1["offset_start"] == expected_item_1_start

def test_detect_sections_with_table_separators():
    """Detect Part and Item headings containing table separators."""

    text = "\n".join([
        "PART I. | FINANCIAL INFORMATION |",
        "ITEM 1. | FINANCIAL STATEMENTS |",
        "Financial statement content.",
        "ITEM 2. MANAGEMENT'S DISCUSSION |",
        "Management discussion content.",
        "PART II. OTHER INFORMATION |",
        "ITEM 1. LEGAL PROCEEDINGS |",
        "Legal proceedings content.",
    ])

    sections = detect_sections(text)

    section_ids = {
        section["section_id"]
        for section in sections
    }

    assert "part-i" in section_ids
    assert "part-i-item-1" in section_ids
    assert "part-i-item-2" in section_ids
    assert "part-ii" in section_ids
    assert "part-ii-item-1" in section_ids

def test_clean_report_skips_hidden_and_empty_table_rows(tmp_path):
    """Skip hidden and empty rows while preserving real table data."""

    html = """
    <html>
      <body>
        <table>
          <tr style="visibility:collapse">
            <td>Hidden information</td>
          </tr>
          <tr style="display:none">
            <td>Also hidden</td>
          </tr>
          <tr>
            <td></td>
            <td>   </td>
          </tr>
          <tr>
            <td><p>Revenue</p></td>
            <td><p>100</p></td>
          </tr>
        </table>
      </body>
    </html>
    """

    html_path = tmp_path / "test_hidden_rows.html"
    html_path.write_text(html, encoding="utf-8")

    report = clean_report(html_path)

    assert "Hidden information" not in report.full_text
    assert "Also hidden" not in report.full_text
    assert report.full_text == "Revenue | 100"

def test_part_i_ignores_repeated_page_header():
    """Use the real Part I heading, not a repeated page header."""

    text = "\n".join([
        "PART I. FINANCIAL INFORMATION",
        "ITEM 1. FINANCIAL STATEMENTS",
        "Actual financial statement content.",
        "PART I. FINANCIAL INFORMATION",
        "More financial statement content.",
        "ITEM 2. MANAGEMENT'S DISCUSSION",
        "Actual management discussion content.",
        "PART II. OTHER INFORMATION",
        "ITEM 1. LEGAL PROCEEDINGS",
        "Actual legal proceedings content.",
    ])

    sections = detect_sections(text)

    part_i = next(
        section
        for section in sections
        if section["section_id"] == "part-i"
    )

    expected_start = text.find(
        "PART I. FINANCIAL INFORMATION"
    )

    assert part_i["offset_start"] == expected_start

    section_ids = {
        section["section_id"]
        for section in sections
    }

    assert "part-i-item-1" in section_ids
    assert "part-i-item-2" in section_ids
    assert "part-ii" in section_ids


def write_source(tmp_path, source):
    path = tmp_path / "report.html"
    path.write_text(source, encoding="utf-8")
    return path


@pytest.mark.parametrize("attributes", [
    'style="display: none"',
    'style="DISPLAY : NONE !important"',
    'style="visibility: hidden"',
    'style="visibility: collapse"',
    'style="display: /* comment */ none"',
    'style="display:none!important;display:block"',
    'hidden',
    'hidden="false"',  # HTML boolean attributes are true whenever present.
])
def test_hidden_elements_and_cells_are_removed_with_visible_tail(tmp_path, attributes):
    path = write_source(tmp_path, f"""<html><body>
      <div {attributes}><p>Hidden block</p></div>
      <p>Visible <span {attributes}>hidden inline</span>tail.</p>
      <table><tr><td>Revenue</td><td {attributes}>Hidden cell</td><td>42</td></tr></table>
    </body></html>""")
    report = clean_report(path)
    assert "Hidden" not in report.full_text
    assert "hidden inline" not in report.full_text
    assert "Visible tail." in report.full_text
    assert "Revenue | 42" in report.full_text


def test_inline_visibility_declaration_precedence(tmp_path):
    path = write_source(tmp_path, """<html><body>
      <p style="display:none;display:block">Last declaration</p>
      <p style="visibility:hidden;visibility:visible!important">Important visible</p>
      <p style="display:none!important;display:block!important">Last important</p>
    </body></html>""")
    assert clean_report(path).full_text == (
        "Last declaration\n\nImportant visible\n\nLast important"
    )


def test_visible_inline_text_and_comments_preserve_spacing(tmp_path):
    path = write_source(tmp_path, """<html><head><title>Metadata title</title></head><body>
      <p><span>Finan</span><b>cial</b> <span>state</span>ments<!-- not visible -->.</p>
      <ix:header><ix:hidden>Hidden XBRL</ix:hidden></ix:header>
      <script>Script payload</script><style>Style payload</style>
    </body></html>""")
    assert clean_report(path).full_text == "Financial statements."


def test_table_cell_breaks_and_paragraphs_survive_normalization(tmp_path):
    path = write_source(tmp_path, """<table>
      <tr><td>Three Months Ended<br>March 31, 2026</td><td>42</td></tr>
      <tr><td><p>First paragraph.</p><p>Second paragraph.</p></td><td>43</td></tr>
    </table>""")
    report = clean_report(path)
    assert "Three Months Ended\nMarch 31, 2026 | 42" in report.full_text
    assert "First paragraph.\n\nSecond paragraph. | 43" in report.full_text


def test_meaningful_paragraphs_keep_one_blank_line(tmp_path):
    path = write_source(tmp_path, """<html><body><div>
      <p>First <span>paragraph</span>.</p><p>營收 café é 📈.</p>
      <p>Third<br>line.</p>
    </div></body></html>""")
    report = clean_report(path)
    assert report.full_text == "First paragraph.\n\n營收 café é 📈.\n\nThird\nline."
    assert report == clean_report(path)


def test_original_locators_choose_body_headings_and_resolve_untouched_source(tmp_path):
    path = write_source(tmp_path, """<html><body>
      <p hidden>Hidden preceding sibling</p>
      <p id="cover">TABLE OF CONTENTS</p>
      <p>PART I. FINANCIAL INFORMATION</p>
      <p>ITEM 1. FINANCIAL STATEMENTS</p>
      <p>PART II. OTHER INFORMATION</p>
      <p>ITEM 1A. RISK FACTORS</p>
      <p id="part-one"><span>PART I.</span> <b>FINANCIAL INFORMATION</b></p>
      <p id="duplicate"><span>ITEM 1.</span> <b>FINANCIAL STATEMENTS</b></p>
      <p>Statement body.</p>
      <p id="part-two"><span>PART II.</span> OTHER INFORMATION</p>
      <p id="duplicate">ITEM <b>1A.</b> RISK <span>FACTORS</span></p>
      <p>Risk body.</p>
    </body></html>""")
    original = path.read_bytes()
    document = html.document_fromstring(
        original, parser=html.HTMLParser(encoding="utf-8", no_network=True),
    )
    report = clean_report(path)
    assert path.read_bytes() == original
    expected = {
        "front-matter": ("cover", "/html/body/p[2]"),
        "part-i": ("part-one", "/html/body/p[7]"),
        "part-i-item-1": ("duplicate", "/html/body/p[8]"),
        "part-ii": ("part-two", "/html/body/p[10]"),
        "part-ii-item-1a": ("duplicate", "/html/body/p[11]"),
    }
    assert {s["section_id"] for s in report.sections} == set(expected)
    for section in report.sections:
        element_id, xpath = expected[section["section_id"]]
        locator = section["source_locator"]
        assert locator == {"source_file": path.name, "xpath": xpath, "element_id": element_id}
        matches = document.xpath(locator["xpath"])
        assert len(matches) == 1
        heading = " ".join(matches[0].text_content().split())
        assert report.full_text[section["offset_start"]:].startswith(heading)


def test_original_row_locator_covers_heading_split_between_table_cells(tmp_path):
    path = write_source(tmp_path, """<html><body><table>
      <tr hidden><td>Hidden row</td></tr>
      <tr id="part"><td>PART I.</td><td>FINANCIAL INFORMATION</td></tr>
      <tr id="statements"><td>ITEM 1.</td><td>FINANCIAL STATEMENTS</td></tr>
    </table><p>Content.</p></body></html>""")
    report = clean_report(path)
    sections = {s["section_id"]: s for s in report.sections}
    assert sections["part-i"]["source_locator"]["xpath"] == "/html/body/table/tr[2]"
    assert sections["part-i-item-1"]["source_locator"]["xpath"] == "/html/body/table/tr[3]"
    assert sections["part-i-item-1"]["source_locator"]["element_id"] == "statements"


def test_item_page_header_does_not_swallow_the_actual_heading():
    for separator in ("\n", "\n\n", " \n"):
        text = ("PART I. FINANCIAL INFORMATION\n\nItem 2" + separator
                + "ITEM 2. MANAGEMENT'S DISCUSSION\n\nBody.")
        section = next(s for s in detect_sections(text) if s["section_id"] == "part-i-item-2")
        assert section["offset_start"] == text.index("ITEM 2.")


def test_explicit_heading_line_breaks_keep_sections_and_original_locators(tmp_path):
    path = write_source(tmp_path, """<html><body>
      <p id="part-one">PART I.<br>FINANCIAL INFORMATION</p>
      <p id="statements">ITEM 1.<br>FINANCIAL STATEMENTS</p><p>Statement body.</p>
      <p id="part-two">PART II<br>OTHER INFORMATION</p>
      <p id="legal">ITEM 1.<br>LEGAL PROCEEDINGS</p><p>Legal body.</p>
    </body></html>""")
    report = clean_report(path)
    expected = {"part-i": "part-one", "part-i-item-1": "statements",
                "part-ii": "part-two", "part-ii-item-1": "legal"}
    assert {s["section_id"] for s in report.sections} == set(expected)
    original = html.document_fromstring(
        path.read_bytes(), parser=html.HTMLParser(encoding="utf-8", no_network=True),
    )
    for section in report.sections:
        locator = section["source_locator"]
        assert locator["element_id"] == expected[section["section_id"]]
        assert original.xpath(locator["xpath"])[0].get("id") == locator["element_id"]
        assert not report.full_text[section["offset_start"]].isspace()


def test_separate_paragraphs_do_not_form_a_heading():
    assert detect_sections("PART I.\n\nFINANCIAL INFORMATION")[0]["section_id"] == "full-report"
    text = "PART I. FINANCIAL INFORMATION\n\nITEM 1.\n\nFINANCIAL STATEMENTS"
    assert [s["section_id"] for s in detect_sections(text)] == ["part-i"]


def test_empty_and_hidden_only_reports_have_no_sections(tmp_path):
    for source in ("", "<!-- no visible content -->", "<!DOCTYPE html>",
                   "<!DOCTYPE html>\n<!-- no visible content -->",
                   "<html><body><p hidden>Hidden</p></body></html>"):
        report = clean_report(write_source(tmp_path, source))
        assert report.full_text == ""
        assert report.sections == []


def test_recoverable_malformed_html_retains_visible_content(tmp_path):
    report = clean_report(write_source(tmp_path, "<p>Visible <b>bold<p>Next paragraph"))
    assert "Visible bold" in report.full_text
    assert "Next paragraph" in report.full_text


def test_layout_spacer_cells_do_not_generate_empty_separators(tmp_path):
    path = write_source(tmp_path, '''<table><tr><td>Total revenue</td>
      <td></td><td> </td><td>82,886</td><td></td><td></td><td>70,066</td>
      <td></td></tr><tr><td></td><td></td></tr></table>''')
    report = clean_report(path, form="10-Q")
    assert report.full_text == "Total revenue | 82,886 | 70,066"


def test_heading_in_layout_table_with_empty_cells_is_body_not_toc(tmp_path):
    path = write_source(tmp_path, '''<table><tr><td></td><td>Item 1.</td>
      <td>Financial Statements</td><td></td><td></td></tr></table>
      <p>Statement body.</p>''')
    report = clean_report(path, form="10-Q")
    assert report.sections[0]["section_id"] == "part-i-item-1"
    assert report.sections[0]["source_locator"]["xpath"] == "/html/body/table/tr"


def test_unlinked_toc_stays_excluded_after_spacer_cells_removed(tmp_path):
    path = write_source(tmp_path, '''<p>FORM 10-Q</p><table>
      <tr><td>Item 1.</td><td>Financial Statements</td><td></td></tr>
      <tr><td></td><td>Income statements</td><td>93</td></tr>
      <tr><td>Item 2.</td><td>Management’s Discussion and Analysis</td><td></td></tr>
      <tr><td></td><td>Introduction</td><td>4</td></tr>
      <tr><td>Item 3.</td><td>Quantitative and Qualitative Disclosures</td><td>201</td></tr>
      <tr><td>Item 4.</td><td>Controls and Procedures</td><td>201</td></tr>
      </table><p>INTRODUCTION</p><p>Unnumbered discussion.</p>
      <p>Item 3. Quantitative and Qualitative Disclosures</p><p>Risk body.</p>''')
    report = clean_report(path, form="10-Q")
    assert report.sections[0]["section_id"] == "unclassified-prefix"
    assert not any(s["section_type"] in {"mda", "financial_statements"} for s in report.sections)
    assert report.sections[-1]["section_id"] == "part-i-item-3"


def test_linked_toc_does_not_steal_body_heading_without_page_column(tmp_path):
    path = write_source(tmp_path, '''<p><a href="#risk">Item 1A. Risk Factors</a></p>
      <p>Index description that looks like body prose.</p>
      <p id="risk">Item 1A. Risk Factors</p><p>Actual risks.</p>''')
    report = clean_report(path, form="10-K")
    risk = next(s for s in report.sections if s["section_type"] == "risk_factors")
    assert risk["source_locator"]["element_id"] == "risk"
    assert risk["offset_start"] == report.full_text.rfind("Item 1A. Risk Factors")


def test_annual_market_heading_allows_the_registrant():
    text = ("PART II\nItem 5. Market for the Registrant’s Common Equity, Related "
            "Stockholder Matters and Issuer Purchases of Equity Securities.\nMarket discussion.")
    sections = detect_sections(text, form="10-K")
    assert [s["section_id"] for s in sections] == ["part-ii", "part-ii-item-5"]
    assert sections[1]["section_type"] == "market_equity"


def test_reserved_body_item_followed_by_page_footer_is_not_a_toc(tmp_path):
    path = write_source(tmp_path, '''<p>PART II</p><p>Item 6. Reserved.</p>
      <p>31</p><p>Table of Contents</p>
      <p>Item 7. Management’s Discussion and Analysis</p><p>Discussion.</p>''')
    report = clean_report(path, form="10-K")
    assert "part-ii-item-6" in {s["section_id"] for s in report.sections}


def test_annual_market_heading_with_issuer_name_and_parenthesized_reserved():
    text = ("PART II\nItem 5. Market for Medtronic’s Common Equity, Related Shareholder Matters\n"
            "Market discussion.\nItem 6. (Reserved)\nNo disclosure.\n"
            "Item 7. Management’s Discussion and Analysis\nDiscussion.")
    sections = detect_sections(text, form="10-K")
    assert [s["section_id"] for s in sections] == [
        "part-ii", "part-ii-item-5", "part-ii-item-6", "part-ii-item-7"]


def test_body_part_keeps_proxy_preamble_before_first_item(tmp_path):
    path = write_source(tmp_path, '''<p>PART I</p><p>Item 1. Business</p><p>Business.</p>
      <p id="governance">PART III</p>
      <p>Part III incorporates information by reference from our proxy statement.</p>
      <p>Item 10. Directors, Executive Officers, and Corporate Governance</p><p>Directors.</p>''')
    report = clean_report(path, form="10-K")
    part = next(s for s in report.sections if s["section_id"] == "part-iii")
    assert part["source_locator"]["element_id"] == "governance"
    assert part["source_locator"]["incorporation_status"] == "unresolved"
    assert "proxy statement" in report.full_text[part["offset_start"]:part["offset_end"]]


@pytest.mark.parametrize('form', [None, '10-Q'])
def test_toc_link_establishes_unnumbered_exhibit_index(tmp_path, form):
    path = write_source(tmp_path, '''<p>FORM 10-Q</p><table><tr><td><a href="#exhibits">Item 6.</a></td>
      <td><a href="#exhibits">Exhibits</a></td><td>60</td></tr></table>
      <p>Item 1. Financial Statements</p><p>Statements.</p>
      <p>Item 5. Other Information</p><p>Other information.</p>
      <div id="exhibits"></div><p>Table of Contents</p>
      <p id="index">Index to Exhibits</p>
      <p>Exhibit descriptions incorporated by reference from the proxy statement.</p>''')
    report = clean_report(path, form=form)
    section = next(s for s in report.sections if s['section_id'] == 'part-ii-item-6')
    assert section['original_title'] == 'Index to Exhibits'
    assert section['source_locator']['element_id'] == 'index'
    assert section['source_locator']['heading_method'] == 'original_toc_target'
    assert section['source_locator']['target_element_id'] == 'exhibits'
    assert section['source_locator']['incorporation_status'] == 'unresolved'


def test_unlinked_exhibit_index_does_not_invent_an_item(tmp_path):
    path = write_source(tmp_path, '<p>Index to Exhibits</p><p>Descriptions.</p>')
    assert not any(s.get('item') == '6' for s in clean_report(path, form='10-Q').sections)


def test_inline_toc_target_with_intervening_prose_does_not_relabel_later_index(tmp_path):
    path = write_source(tmp_path, '''<table><tr><td><a href="#e">Item 6.</a></td>
      <td>Exhibits</td><td>60</td></tr></table>
      <p>Item 1. Financial Statements</p><p>Statements.</p>
      <div><a id="e"></a>Substantive material between target and index.</div>
      <p>Index to Exhibits</p><p>Descriptions.</p>''')
    assert not any(s.get('item') == '6' for s in clean_report(path, form='10-Q').sections)


@pytest.mark.parametrize('title,kind', [
    ('Information about our Executive Officers', 'directors_governance'),
    ('Cybersecurity', 'cybersecurity'),
])
def test_annual_item_1c_meaning_comes_from_original_title(title, kind):
    sections = detect_sections(f'PART I\nItem 1. Business\nBusiness.\nItem 1C. {title}\nDetails.', form='10-K')
    section = next(s for s in sections if s['section_id'] == 'part-i-item-1c')
    assert section['section_type'] == kind
    assert section['original_title'] == f'Item 1C. {title}'
