
from pathlib import Path

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

    assert "Revenue | 10 |" in report.full_text

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
    assert report.full_text == "Revenue | 100 |"

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
    