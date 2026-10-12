
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
