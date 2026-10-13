
import re
from dataclasses import dataclass
from pathlib import Path

from lxml import etree, html


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
        r"(?im)^ITEM[^\S\n]+(\d+[A-Z]?)[^\S\n]*\.?"
        r"(?:[^\S\n]+|[^\S\n]*\n(?![^\S\n]*ITEM[^\S\n]+\d)[^\S\n]*)(.+)$"
    )

    # Find full Part I and Part II headings.
    # Requiring the complete title helps avoid page headers.
    part_pattern = re.compile(
        r"(?im)^PART[^\S\n]+(I|II)(?=[\s.\-–—|])"
        r"[^\S\n]*[.\-–—]?[^\S\n]*\|?[^\S\n]*(?:\n[^\S\n]*)?"
        r"(FINANCIAL|OTHER)(?:[^\S\n]+|\n[^\S\n]*)INFORMATION[^\S\n]*\|?[^\S\n]*$"
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
        complete_part1_candidates = [
            candidate
            for candidate in part1_candidates
            if any(
                match.group(1).upper() == "II"
                and match.start() > candidate.start()
                for match in part_matches
            )
        ]

        if complete_part1_candidates:
            if "INDEX" in full_text.upper() or "TABLE OF CONTENTS" in full_text.upper():
                part1 = complete_part1_candidates[-1]
            else:
                part1 = complete_part1_candidates[0]
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

        if (
            not valid_title
            and item_number == "1"
            and (part2 is None or position < part2.start())
            and re.search(r"\bFINANCIAL STATEMENTS\b", title)
        ):
            valid_title = True

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


# These boundaries describe readable text, not a browser CSS layout engine.
_BLOCK_TAGS = {
    "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "blockquote", "pre", "address", "header", "footer",
    "ul", "ol", "dl", "dt", "dd", "table",
}
_OMITTED_TAGS = {"head", "script", "style", "ix:header", "ix:hidden"}


def _hidden(element: etree._Element) -> bool:
    """Recognize local visibility rules without fetching stylesheets."""
    if "hidden" in element.attrib:
        return True
    # Later inline declarations win; !important overrides ordinary declarations.
    declarations = {}
    style = re.sub(r"/\*.*?\*/", "", element.get("style", ""), flags=re.S)
    for declaration in style.split(";"):
        name, separator, value = declaration.partition(":")
        if not separator:
            continue
        name, value = name.strip().lower(), value.strip().lower()
        important = bool(re.search(r"!\s*important\s*$", value))
        value = re.sub(r"!\s*important\s*$", "", value).strip()
        previous = declarations.get(name)
        if previous is None or important or not previous[1]:
            declarations[name] = (value, important)
    return (declarations.get("display", (None,))[0] == "none"
            or declarations.get("visibility", (None,))[0] in {"hidden", "collapse"})


def _trim_fragments(fragments):
    """Trim cell boundaries without discarding internal line/paragraph breaks."""
    while fragments and not fragments[0][0].strip():
        fragments.pop(0)
    while fragments and not fragments[-1][0].strip():
        fragments.pop()
    if fragments:
        fragments[0] = (fragments[0][0].lstrip(), fragments[0][1])
        fragments[-1] = (fragments[-1][0].rstrip(), fragments[-1][1])
    return fragments


def _render(element, anchor=None, row_anchor=None):
    """Render annotated fragments while leaving the original DOM untouched."""
    if not isinstance(element.tag, str):  # Comments and processing instructions.
        return []
    tag = element.tag.lower()
    if tag in _OMITTED_TAGS or _hidden(element):
        return []
    if tag == "br":
        return [("\n", row_anchor if row_anchor is not None else anchor)]
    if tag == "tr":
        cells = [child for child in element
                 if isinstance(child.tag, str) and child.tag.lower() in {"td", "th"}
                 and not _hidden(child)]
        if cells:
            rendered = [_trim_fragments(_render(cell, element, element)) for cell in cells]
            if not any(rendered):
                return []
            fragments = [("\n", element)]
            for cell in rendered:
                fragments.extend(cell)
                fragments.append((" | ", element))
            fragments.append(("\n", element))
            return fragments
        return []

    is_block = tag in _BLOCK_TAGS
    if row_anchor is not None:
        anchor = row_anchor
    elif is_block or anchor is None:
        anchor = element
    fragments = [("\n\n", anchor)] if is_block else []
    if element.text:
        fragments.append((re.sub(r"\s+", " ", element.text), anchor))
    for child in element:
        fragments.extend(_render(child, anchor, row_anchor))
        # Tail text belongs to the visible parent even when the child is hidden.
        if child.tail:
            fragments.append((re.sub(r"\s+", " ", child.tail), anchor))
    if is_block:
        fragments.append(("\n\n", anchor))
    return fragments


def _normalize_fragments(fragments):
    """Normalize spaces and keep at most one blank line with source anchors."""
    text, anchors = [], []
    newlines = 0
    space = False
    for fragment, anchor in fragments:
        for character in fragment:
            if character.isspace():
                if character == "\n":
                    newlines = min(2, newlines + 1)
                else:
                    space = True
                continue
            if text:
                separator = "\n" * newlines if newlines else (" " if space else "")
                text.extend(separator)
                anchors.extend([None] * len(separator))
            text.append(character)
            anchors.append(anchor)
            newlines, space = 0, False
    return "".join(text), anchors


def clean_report(html_path: str | Path) -> CleanedReport:
    """Render visible SEC HTML with original-node locators and final text offsets.

    Locators resolve against the unchanged bytes parsed with
    lxml.html.document_fromstring and HTMLParser(encoding="utf-8", no_network=True).
    XPath is authoritative; an original element ID is supplementary. A section's
    locator identifies its starting block/row, not every character in the section.
    """
    html_path = Path(html_path)
    source = html_path.read_bytes()
    if not source.strip():
        return CleanedReport(full_text="", sections=[])
    try:
        document = html.document_fromstring(
            source, parser=html.HTMLParser(encoding="utf-8", no_network=True),
        )
    except etree.ParserError:
        # lxml has no document element for comment/doctype-only input. Do not
        # turn other parse failures or malformed visible content into success.
        if re.fullmatch(rb"\s*(?:(?:<!--.*?-->|<!DOCTYPE[^>]*>)\s*)*", source, re.I | re.S):
            return CleanedReport(full_text="", sections=[])
        raise
    # No nodes are removed or rewritten: anchors always refer to the original DOM.
    full_text, anchors = _normalize_fragments(_render(document))
    sections = detect_sections(full_text)
    tree = document.getroottree()
    for section in sections:
        anchor = anchors[section["offset_start"]]
        locator = {"source_file": html_path.name, "xpath": tree.getpath(anchor)}
        if anchor.get("id"):
            locator["element_id"] = anchor.get("id")
        section["source_locator"] = locator
    return CleanedReport(full_text=full_text, sections=sections)
