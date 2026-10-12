
import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup


@dataclass
class CleanedReport:
    full_text: str
    sections: list[dict]


def detect_sections(full_text: str) -> list[dict]:
    """Detect major sections in a SEC 10-Q filing."""

    if not full_text:
        return []

    # Expected item headings in a 10-Q.
    expected_titles = {
        "1": ["FINANCIAL STATEMENTS", "LEGAL PROCEEDINGS"],
        "1A": ["RISK FACTORS"],
        "2": [
            "MANAGEMENT",
            "UNREGISTERED SALES OF EQUITY SECURITIES",
        ],
        "3": [
            "QUANTITATIVE AND QUALITATIVE DISCLOSURES",
            "DEFAULTS UPON SENIOR SECURITIES",
        ],
        "4": [
            "CONTROLS AND PROCEDURES",
            "MINE SAFETY DISCLOSURES",
        ],
        "5": ["OTHER INFORMATION"],
        "6": ["EXHIBITS"],
    }

    # Find item headings.
    item_pattern = re.compile(
        r"(?im)^ITEM\s+(\d+[A-Z]?)\s*\.?\s+(.+)$"
    )

    # Find full Part I and Part II headings.
    # Requiring the complete title helps avoid page headers.
    part_pattern = re.compile(
        r"(?im)^PART\s+(I|II)\.?\s+(FINANCIAL|OTHER)\s+INFORMATION\s*$"
    )

    part_matches = list(part_pattern.finditer(full_text))

    # Find Part I headings.
    part1_candidates = [
        match
        for match in part_matches
        if match.group(1).upper() == "I"
    ]

    # Prefer the last Part I heading when a TOC is present.
    part1 = None

    if part1_candidates:
        if (
            "TABLE OF CONTENTS" in full_text.upper()
            and len(part1_candidates) > 1
        ):
            part1 = part1_candidates[-1]
        else:
            part1 = part1_candidates[0]

    # Fallback if the report structure is not recognized.
    if part1 is None:
        return [{
            "section_id": "full-report",
            "title": "Full Report",
            "offset_start": 0,
            "offset_end": len(full_text),
        }]

    # Start with front matter and Part I.
    boundaries = [
        (0, "front-matter", "Front Matter"),
        (
            part1.start(),
            "part-i",
            "Part I - Financial Information",
        ),
    ]

    # Find Part II after Part I.
    part2 = next(
        (
            match
            for match in part_matches
            if match.group(1).upper() == "II"
            and match.start() > part1.start()
        ),
        None,
    )

    if part2 is not None:
        boundaries.append((
            part2.start(),
            "part-ii",
            "Part II - Other Information",
        ))

    # Prevent duplicate sections.
    seen = set()

    for match in item_pattern.finditer(full_text):
        position = match.start()

        # Skip table of contents and front matter.
        if position <= part1.start():
            continue

        item_number = match.group(1).upper()
        title = match.group(2).strip().upper()

        # Remove a duplicated ITEM prefix if present.
        title = re.sub(
            r"^ITEM\s+\d+[A-Z]?\s*\.?\s*",
            "",
            title,
        )

        # Ignore repeated page headers and other text.
        valid_title = any(
            title.startswith(expected)
            for expected in expected_titles.get(item_number, [])
        )

        if not valid_title:
            continue

        # Determine whether the item belongs to Part I or II.
        part = (
            "ii"
            if part2 is not None and position > part2.start()
            else "i"
        )

        section_id = (
            f"part-{part}-item-{item_number.lower()}"
        )

        if section_id in seen:
            continue

        seen.add(section_id)

        boundaries.append((
            position,
            section_id,
            f"Item {item_number} - {title}",
        ))

    # Sort boundaries by their positions in full_text.
    boundaries.sort(key=lambda boundary: boundary[0])

    # Convert boundaries into continuous sections.
    sections = []

    for i, (start, section_id, title) in enumerate(boundaries):
        if i + 1 < len(boundaries):
            end = boundaries[i + 1][0]
        else:
            end = len(full_text)

        if start >= end:
            continue

        sections.append({
            "section_id": section_id,
            "title": title,
            "offset_start": start,
            "offset_end": end,
        })

    return sections


def clean_report(html_path: str | Path) -> CleanedReport:
    """Extract and organize readable text from SEC HTML."""

    html_path = Path(html_path)

    # Read the HTML file.
    html = html_path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    soup = BeautifulSoup(html, "html.parser")

    # Remove scripts, styles, and hidden XBRL metadata.
    for tag in soup.find_all(
        ["script", "style", "ix:header", "ix:hidden"]
    ):
        tag.decompose()

    # Add separators between table cells.
    for cell in soup.find_all(["td", "th"]):
        cell.append(" | ")

    # Preserve paragraph and row boundaries.
    for tag in soup.find_all(
        ["p", "div", "tr", "li", "h1", "h2", "h3"]
    ):
        tag.append("\n")

    # Extract text without adding extra spaces.
    text = soup.get_text(separator="", strip=False)

    # Normalize whitespace while preserving lines.
    lines = [
        " ".join(line.split())
        for line in text.splitlines()
    ]

    full_text = "\n".join(
        line for line in lines if line
    )

    # Detect report sections.
    sections = detect_sections(full_text)

    # Record the source HTML filename for each section.
    for section in sections:
        section["source_locator"] = html_path.name

    # Return the result to downstream pipeline modules.
    return CleanedReport(
        full_text=full_text,
        sections=sections,
    )
