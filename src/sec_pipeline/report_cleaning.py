
import re
from dataclasses import dataclass
from pathlib import Path

from lxml import etree, html


@dataclass
class CleanedReport:
    full_text: str
    sections: list[dict]


# Canonical meaning is distinct from the item number, which changes by form.
# Patterns describe headings, not arbitrary mentions of the topic in body text.
_QUARTERLY_ITEMS = {
    ("I", "1"): ("financial_statements", r"(?:UNAUDITED |CONDENSED |CONSOLIDATED )*FINANCIAL STATEMENTS"),
    ("I", "2"): ("mda", r"MANAGEMENT[’']?S? DISCUSSION"),
    ("I", "3"): ("market_risk", r"QUANTITATIVE AND QUALITATIVE DISCLOSURES"),
    ("I", "4"): ("controls", r"CONTROLS AND PROCEDURES"),
    ("II", "1"): ("legal_proceedings", r"LEGAL PROCEEDINGS"),
    ("II", "1A"): ("risk_factors", r"RISK FACTORS"),
    ("II", "2"): ("equity_sales", r"UNREGISTERED SALES OF EQUITY SECURITIES"),
    ("II", "3"): ("defaults", r"DEFAULTS UPON SENIOR SECURITIES"),
    ("II", "4"): ("mine_safety", r"MINE SAFETY DISCLOSURES"),
    ("II", "5"): ("other_information", r"OTHER INFORMATION"),
    ("II", "6"): ("exhibits", r"EXHIBITS"),
}
_ANNUAL_ITEMS = {
    ("I", "1"): ("business", r"BUSINESS"),
    ("I", "1A"): ("risk_factors", r"RISK FACTORS"),
    ("I", "1B"): ("unknown", r"UNRESOLVED STAFF COMMENTS"),
    ("I", "1C"): ("cybersecurity", r"CYBERSECURITY"),
    ("I", "2"): ("properties", r"PROPERTIES"),
    ("I", "3"): ("legal_proceedings", r"LEGAL PROCEEDINGS"),
    ("I", "4"): ("mine_safety", r"MINE SAFETY DISCLOSURES"),
    ("II", "5"): ("market_equity", r"MARKET FOR REGISTRANT"),
    ("II", "6"): ("unknown", r"(?:\[?RESERVED\]?|SELECTED FINANCIAL DATA)"),
    ("II", "7"): ("mda", r"MANAGEMENT[’']?S? DISCUSSION"),
    ("II", "7A"): ("market_risk", r"QUANTITATIVE AND QUALITATIVE DISCLOSURES"),
    ("II", "8"): ("financial_statements", r"FINANCIAL STATEMENTS"),
    ("II", "9"): ("accounting_changes", r"CHANGES IN AND DISAGREEMENTS"),
    ("II", "9A"): ("controls", r"CONTROLS AND PROCEDURES"),
    ("II", "9B"): ("other_information", r"OTHER INFORMATION"),
    ("II", "9C"): ("unknown", r"DISCLOSURE REGARDING FOREIGN JURISDICTIONS"),
    ("III", "10"): ("directors_governance", r"DIRECTORS"),
    ("III", "11"): ("executive_compensation", r"EXECUTIVE COMPENSATION"),
    ("III", "12"): ("ownership", r"SECURITY OWNERSHIP"),
    ("III", "13"): ("related_party_transactions", r"CERTAIN RELATIONSHIPS"),
    ("III", "14"): ("audit_fees", r"PRINCIPAL ACCOUNT(?:ANT|ING) FEES"),
    ("IV", "15"): ("exhibits", r"EXHIBITS?(?: AND FINANCIAL STATEMENT SCHEDULES)?"),
    ("IV", "16"): ("summary", r"FORM 10-K SUMMARY"),
}
_ITEM_HEADING = re.compile(r"^ITEM[ \t]+(\d+[A-Z]?)[ \t]*[.:–—-]?[ \t]*(.*)$", re.I)
_PART_HEADING = re.compile(
    r"^PART[ \t]+(IV|III|II|I)(?:[ \t]*[.–—-]?[ \t]*(?:FINANCIAL|OTHER) INFORMATION)?[ \t.]*$", re.I)


def _heading_text(line: str) -> str:
    return re.sub(r"[ \t]+", " ", line.replace("|", " ")).strip()


def detect_sections(full_text: str, form: str | None = None) -> list[dict]:
    """Find conservative form-aware body headings with exact Unicode offsets.

    Detected labels are heuristic, never marked validated. Unsupported layouts
    retain explicit unknown ranges rather than assigning a guessed topic.
    """
    if not full_text:
        return []
    if form is None:
        form_match = re.search(r"(?im)^FORM[ \t]+(10-[KQ])(?:/A)?[ \t]*$", full_text[:12000])
        if form_match:
            form = form_match.group(1).upper()
        else:
            form = "10-K" if re.search(r"(?im)^ITEM[ \t]+(?:7|10)\.", full_text) else "10-Q"
    form = form.upper().removesuffix("/A")
    vocabulary = _ANNUAL_ITEMS if form == "10-K" else _QUARTERLY_ITEMS if form == "10-Q" else {}
    lines = full_text.splitlines(keepends=True)
    offsets, position = [], 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    items, parts = [], []
    for index, raw_line in enumerate(lines):
        heading = _heading_text(raw_line)
        part_match = _PART_HEADING.fullmatch(heading)
        if part_match:
            part_last_line = index
            # Explicit <br> line breaks can split a heading. A blank line is a
            # paragraph boundary and must never be consumed into the heading.
            if "INFORMATION" not in heading.upper():
                combined = heading
                for continuation in range(index + 1, min(index + 3, len(lines))):
                    following = _heading_text(lines[continuation])
                    if not following:
                        break
                    combined += " " + following
                    if _PART_HEADING.fullmatch(combined) and "INFORMATION" in combined.upper():
                        heading = combined
                        part_last_line = continuation
                        break
            parts.append({"offset_start": offsets[index], "part": part_match.group(1).upper(),
                          "title": heading, "line": index, "last_line": part_last_line})
            continue
        match = _ITEM_HEADING.fullmatch(heading)
        if not match:
            continue
        # Leading empty table cells are characteristic index rows, not headings.
        if raw_line.lstrip().startswith("|") or re.search(r"\|[ \t]*\|\s*$", raw_line):
            continue
        item, title = match.group(1).upper(), match.group(2).strip()
        last_line = index
        if not title and index + 1 < len(lines):
            if lines[index + 1].lstrip().startswith("|"):
                continue
            title = _heading_text(lines[index + 1])
            if _ITEM_HEADING.match(title):
                continue
            last_line += 1
        title = re.sub(r"^ITEM[ \t]+\d+[A-Z]?[. ]*", "", title, flags=re.I)
        # Page columns and dotted leaders identify TOC rows, even without links.
        next_line = next((_heading_text(line) for line in lines[last_line + 1:last_line + 4]
                          if _heading_text(line)), "")
        if re.search(r"\.{2,}|[ \t]\d+[ \t]*$", title) or re.fullmatch(r"\d+(?:[-–]\d+)?", next_line):
            continue
        choices = [(part, kind) for (part, number), (kind, pattern) in vocabulary.items()
                   if number == item and re.match(pattern + r"(?:\b|$)", title, re.I)]
        if len(choices) != 1:
            continue
        part, kind = choices[0]
        items.append({"offset_start": offsets[index], "section_id": f"part-{part.lower()}-item-{item.lower()}",
                      "title": f"Item {item}. {title}", "original_title": "".join(lines[index:last_line + 1]).strip(),
                      "part": part, "item": item, "section_type": kind,
                      "line": index, "last_line": last_line})
    heading_lines = {candidate["line"] for candidate in [*items, *parts]}
    for index, candidate in enumerate(items):
        following = next((line for line in sorted(heading_lines) if line > candidate["last_line"]), len(lines))
        candidate["has_body"] = any(
            _heading_text(line) and not re.fullmatch(r"[\d \t|.–-]+", line.strip())
            for line in lines[candidate["last_line"] + 1:following])
    # TOC entries have no prose between successive headings. For repeated body
    # page headings, retain the first occurrence with content, not the last.
    chosen = {}
    for candidate in items:
        key = candidate["section_id"]
        if key not in chosen or (not chosen[key]["has_body"] and candidate["has_body"]):
            chosen[key] = candidate
    anchors = [candidate for candidate in chosen.values()
               if candidate["section_type"] in ({"business"} if form == "10-K" else {"financial_statements", "mda"})
               and candidate["has_body"]]
    body_start = min((candidate["offset_start"] for candidate in anchors), default=0)
    selected = [candidate for candidate in chosen.values() if candidate["offset_start"] >= body_start]
    boundaries = list(selected)
    for part in ("I", "II", "III", "IV"):
        first_item = min((candidate["offset_start"] for candidate in selected if candidate["part"] == part), default=None)
        if first_item is None:
            continue
        preceding = [candidate for candidate in parts
                     if candidate["part"] == part and candidate["offset_start"] < first_item]
        if preceding:
            candidate = preceding[-1]
            part_end = offsets[candidate["last_line"]] + len(lines[candidate["last_line"]])
            intervening = full_text[part_end:first_item]
            if re.search(r"[^\s\d|.]", intervening):
                # The only available Part label may be in a distant TOC.
                continue
            # A part heading between body items is handled once; repeated page
            # headings are not made into additional section boundaries.
            boundaries.append(candidate | {"section_id": f"part-{part.lower()}",
                                             "section_type": "part_heading", "item": None})
    if not selected:
        # A complete Part heading remains useful even when no supported Item
        # follows it. A bare Part label alone does not establish report content.
        standalone_parts = {}
        for candidate in parts:
            if "INFORMATION" in candidate["title"].upper():
                standalone_parts.setdefault(candidate["part"], candidate)
        boundaries = [candidate | {"section_id": f"part-{candidate['part'].lower()}",
                                    "section_type": "part_heading", "item": None}
                      for candidate in standalone_parts.values()]
    if not boundaries:
        return [{"section_id": "full-report", "title": "Full Report", "section_type": "unknown",
                 "validation_status": "unverified", "offset_start": 0, "offset_end": len(full_text)}]
    boundaries.sort(key=lambda candidate: candidate["offset_start"])
    if boundaries[0]["offset_start"] > 0:
        known_front = bool(anchors)
        boundaries.insert(0, {"offset_start": 0, "section_id": "front-matter" if known_front else "unclassified-prefix",
                              "title": "Front Matter" if known_front else "Unclassified Content",
                              "section_type": "front_matter" if known_front else "unknown", "part": None, "item": None})
    sections = []
    for index, boundary in enumerate(boundaries):
        start = boundary["offset_start"]
        end = boundaries[index + 1]["offset_start"] if index + 1 < len(boundaries) else len(full_text)
        if end <= start:
            continue
        section = {name: value for name, value in boundary.items() if name not in {"line", "last_line", "has_body"}}
        section.update(offset_end=end, validation_status="unverified" if boundary["section_type"] == "unknown" else "detected")
        # Retain an unresolved notice rather than implying the proxy was loaded.
        body = full_text[start:end]
        if re.search(r"incorporat(?:ed|ion).*?by reference", body, re.I | re.S) and re.search(r"proxy statement", body, re.I):
            section["source_locator"] = {"incorporation_status": "unresolved", "referenced_form": "DEF 14A"}
        sections.append(section)
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


def clean_report(html_path: str | Path, *, form: str | None = None) -> CleanedReport:
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
    sections = detect_sections(full_text, form=form)
    tree = document.getroottree()
    for section in sections:
        anchor = anchors[section["offset_start"]]
        locator = {"source_file": html_path.name, "xpath": tree.getpath(anchor)}
        if anchor.get("id"):
            locator["element_id"] = anchor.get("id")
        section["source_locator"] = section.get("source_locator", {}) | locator
    return CleanedReport(full_text=full_text, sections=sections)
