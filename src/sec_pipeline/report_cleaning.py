
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from lxml import etree, html

from .source_locators import source_xpath


@dataclass
class CleanedReport:
    full_text: str
    sections: list[dict]


class ReportEncodingError(ValueError):
    """The original bytes are not UTF-8, so visible characters would be lost."""


# Report versions that carry the audited section metadata and note-marker
# spacing. Earlier versions keep their exact output; stored reports are immutable.
AUDITED_REPORT_VERSIONS = frozenset({"report-v4"})
# Annual Items that the earlier versions left without a canonical type.
_AUDITED_ITEM_TYPES = {("I", "1B"): "unresolved_staff_comments", ("II", "9C"): "foreign_inspection_disclosure"}
# A superscript that is a note reference: 1, (2), (a), 1,2 or (a)(b). An ordinal
# suffix (st, th), a service mark (SM) and other raised text are not.
_NOTE_REFERENCE = re.compile(
    r"\d{1,3}(?:\s*,\s*\d{1,3})*"                                 # 1 or 1,2
    r"|(?:\(\s*(?:\d{1,3}|[a-z])\s*\)\s*,?\s*)+"                  # (2), (a) or (a)(b)
    r"|[*\u2020\u2021\u00a7\u00b6]+", re.I)
# The end of a heading that runs into its first sentence: "Business. Parker-Hannifin ...".
_RUN_IN_BOUNDARY = re.compile(r"(?<=[a-z\)\]])\.\s+(?=[A-Z(\u201c\"])")


class _NoteMark(str):
    """A zero-width marker in front of a superscript note reference."""


_NOTE_MARK = _NoteMark("")


# Canonical meaning is distinct from the item number, which changes by form.
# Patterns describe headings, not arbitrary mentions of the topic in body text.
_QUARTERLY_ITEMS = {
    ("I", "1"): ("financial_statements", r"(?:INTERIM |UNAUDITED |CONDENSED |CONSOLIDATED )*FINANCIAL STATEMENTS"),
    ("I", "2"): ("mda", r"MANAGEMENT[’']?S? DISCUSSION"),
    ("I", "3"): ("market_risk", r"QUANTITATIVE AND QUALITATIVE DISCLOSURES?"),
    ("I", "4"): ("controls", r"CONTROLS (?:AND|&) PROCEDURES"),
    ("II", "1"): ("legal_proceedings", r"LEGAL PROCEEDINGS"),
    ("II", "1A"): ("risk_factors", r"RISK FACTORS"),
    ("II", "2"): ("equity_sales", r"UNREGISTERED SALES OF EQUITY SECURITIES"),
    ("II", "3"): ("defaults", r"DEFAULTS UPON SENIOR SECURITIES"),
    ("II", "4"): ("mine_safety", r"MINE SAFETY DISCLOSURES?"),
    ("II", "5"): ("other_information", r"OTHER INFORMATION"),
    ("II", "6"): ("exhibits", r"EXHIBITS"),
}
_ANNUAL_ITEMS = {
    ("I", "1"): ("business", r"(?:DESCRIPTION OF (?:THE )?)?BUSINESS"),
    ("I", "1A"): ("risk_factors", r"RISK FACTORS"),
    ("I", "1B"): ("unknown", r"UNRESOLVED STAFF COMMENTS"),
    ("I", "1C"): ("cybersecurity", r"CYBERSECURITY"),
    ("I", "2"): ("properties", r"PROPERTIES"),
    ("I", "3"): ("legal_proceedings", r"LEGAL PROCEEDINGS"),
    ("I", "4"): ("mine_safety", r"MINE SAFETY DISCLOSURES?"),
    ("II", "5"): ("market_equity", r"MARKET (?:PRICE OF AND DIVIDENDS ON|FOR) (?:(?:THE )?REGISTRANT|COMMON EQUITY"
                                   r"|.{1,100}? (?:COMMON (?:EQUITY|STOCK)|ORDINARY SHARES))"),
    ("II", "6"): ("unknown", r"(?:[\[(]?(?:REMOVED AND )?RESERVED[\])]?|INTENTIONALLY OMITTED|SELECTED (?:CONSOLIDATED )?FINANCIAL DATA)"),
    ("II", "7"): ("mda", r"MANAGEMENT[’']?S? DISCUSSION"),
    ("II", "7A"): ("market_risk", r"QUANTITATIVE AND QUALITATIVE DISCLOSURES?"),
    ("II", "8"): ("financial_statements", r"(?:CONSOLIDATED )?FINANCIAL STATEMENTS"),
    ("II", "9"): ("accounting_changes", r"CHANGES IN AND DISAGREEMENTS"),
    ("II", "9A"): ("controls", r"CONTROLS (?:AND|&) PROCEDURES"),
    ("II", "9B"): ("other_information", r"OTHER INFORMATION"),
    ("II", "9C"): ("unknown", r"DISCLOSURE REGARDING FOREIGN JURISDICTIONS"),
    ("III", "10"): ("directors_governance", r"DIRECTORS"),
    ("III", "11"): ("executive_compensation", r"EXECUTIVE COMPENSATION"),
    ("III", "12"): ("ownership", r"SECURITY OWNERSHIP"),
    ("III", "13"): ("related_party_transactions", r"CERTAIN RELATIONSHIPS"),
    ("III", "14"): ("audit_fees", r"PRINCIPAL ACCOUNT(?:ANT|ING) FEES"),
    ("IV", "15"): ("exhibits", r"(?:EXHIBITS?(?: AND FINANCIAL STATEMENT SCHEDULES)?"
                                r"|FINANCIAL STATEMENT SCHEDULES?(?: AND|,) EXHIBITS)"),
    ("IV", "16"): ("summary", r"FORM 10-K SUMMARY"),
}
# Before the cybersecurity Item was introduced, some issuers used Item 1C
# for executive officers. The original title, not the number alone, sets its type.
_ANNUAL_ITEM_VARIANTS = [
    (("I", "1C"), ("directors_governance", r"INFORMATION (?:ABOUT|REGARDING) (?:OUR |THE )?EXECUTIVE OFFICERS")),
]
_ITEM_HEADING = re.compile(r"^ITEM[ \t]+(\d+[A-Z]?)[ \t]*[.:–—-]?[ \t]*(.*)$", re.I)
# The same label on an unnormalized line, up to the first character of its title.
_ITEM_LABEL = re.compile(r"[ \t|]*ITEM[ \t]+\d+[A-Z]?[ \t]*[.:–—-]?[ \t|]*", re.I)
_PART_HEADING = re.compile(
    r"^PART[ \t]+(IV|III|II|I)(?:[ \t]*[.:–—-]?[ \t]*(?:FINANCIAL|OTHER) INFORMATION)?[ \t.]*$", re.I)


def _heading_text(line: str) -> str:
    return re.sub(r"[ \t]+", " ", line.replace("|", " ")).strip()


def _report_form(full_text, form):
    if form is None:
        form_match = re.search(r"(?im)^FORM[ \t]+(10-[KQ])(?:/A)?[ \t]*$", full_text[:12000])
        if form_match:
            form = form_match.group(1).upper()
        else:
            form = "10-K" if re.search(r"(?im)^ITEM[ \t]+(?:7|10)\.", full_text) else "10-Q"
    return form.upper().removesuffix("/A")


def detect_sections(full_text: str, form: str | None = None, *,
                    excluded_heading_offsets: frozenset[int] | None = None,
                    linked_headings: dict[int, dict] | None = None, audited: bool = False) -> list[dict]:
    """Find conservative form-aware body headings with exact Unicode offsets.

    Detected labels are heuristic, never marked validated. Unsupported layouts
    retain explicit unknown ranges rather than assigning a guessed topic.
    """
    if not full_text:
        return []
    form = _report_form(full_text, form)
    vocabulary = _ANNUAL_ITEMS if form == "10-K" else _QUARTERLY_ITEMS if form == "10-Q" else {}
    heading_rules = list(vocabulary.items()) + (_ANNUAL_ITEM_VARIANTS if form == '10-K' else [])
    lines = full_text.splitlines(keepends=True)
    offsets, position = [], 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    items, parts = [], []
    for index, raw_line in enumerate(lines):
        if excluded_heading_offsets is not None and offsets[index] in excluded_heading_offsets:
            continue
        heading = _heading_text(raw_line)
        linked = (linked_headings or {}).get(offsets[index])
        if linked:
            heading = linked['label']
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
        if (re.search(r"\.{2,}|[ \t]\d+[ \t]*$", title)
                or (excluded_heading_offsets is None and re.fullmatch(r"\d+(?:[-–]\d+)?", next_line))):
            continue
        choices = [(part, kind, re.match(pattern + r"(?:\b|$)", title, re.I))
                   for (part, number), (kind, pattern) in heading_rules if number == item]
        choices = [choice for choice in choices if choice[2]]
        if len(choices) != 1:
            continue
        part, kind, recognized = choices[0]
        original_title = "".join(lines[index:last_line + 1]).strip()
        if audited:
            # A heading can run into its first sentence on the same line. The
            # title is the heading; the sentence stays in the section body.
            boundary = _RUN_IN_BOUNDARY.search(title, recognized.end())
            if boundary and len(title) - boundary.end() >= 20:
                title = title[:boundary.start()]
                wanted = len(re.sub(r"[\W_]+", "", f"Item {item} {title}"))
                seen = 0
                for position, character in enumerate(original_title):
                    seen += character.isalnum()
                    if seen == wanted:
                        original_title = original_title[:position + 1]
                        break
            if (part, item) in _AUDITED_ITEM_TYPES:
                kind = _AUDITED_ITEM_TYPES[part, item]
            elif (part, item) == ("II", "6") and form == "10-K":
                kind = "selected_financial_data" if re.match(r"SELECTED\b", title, re.I) else "reserved"
        items.append({"offset_start": offsets[index], "section_id": f"part-{part.lower()}-item-{item.lower()}",
                      "title": f"Item {item}. {title}", "original_title": original_title,
                      "part": part, "item": item, "section_type": kind,
                      "line": index, "last_line": last_line})
        if linked:
            items[-1]['source_locator'] = linked['evidence']
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
                # A source-backed body Part may have introductory prose before
                # its first Item (for example a Part III proxy notice).
                source_backed = excluded_heading_offsets is not None and bool(anchors)
                # The first Part precedes the first Item, so it can never follow
                # the body start. It is a body heading when every contents entry
                # lies before it and no other Part or Item line separates it
                # from its first Item (for example a forward-looking-statements
                # note between "PART I" and "Item 1. Business").
                leading = (source_backed and first_item == body_start
                           and all(offset < candidate['offset_start']
                                   for offset in excluded_heading_offsets if offset < first_item)
                           and not any(_ITEM_HEADING.match(heading) or _PART_HEADING.fullmatch(heading)
                                       for heading in map(_heading_text, intervening.splitlines())))
                if not leading and not (source_backed and candidate['offset_start'] >= body_start):
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
        # Only Part III defers its own content to the proxy statement. A cover
        # page or an exhibit list that mentions the proxy is fully present.
        if ((not audited or boundary.get("part") == "III")
                and re.search(r"incorporat(?:ed|es|ion).*?by reference", body, re.I | re.S)
                and re.search(r"proxy statement", body, re.I)):
            section["source_locator"] = section.get("source_locator", {}) | {
                "incorporation_status": "unresolved", "referenced_form": "DEF 14A"}
        sections.append(section)
    return sections


# These boundaries describe readable text, not a browser CSS layout engine.
_BLOCK_TAGS = {
    "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "blockquote", "pre", "address", "header", "footer",
    "ul", "ol", "dl", "dt", "dd", "table",
    # Block-level in every browser; without a boundary their text fuses with
    # the neighbouring words and a heading inside them is never on its own line.
    "center", "main", "nav", "aside", "figure", "figcaption", "details", "summary",
    "form", "fieldset",
}
_OMITTED_TAGS = {"head", "script", "style"}
_INLINE_NAMESPACES = {
    "http://www.xbrl.org/2008/inlineXBRL",
    "http://www.xbrl.org/2013/inlineXBRL",
}


def _omitted_inline_container(element: etree._Element) -> bool:
    """Identify hidden inline resources by the nearest HTML xmlns declaration."""
    tag = element.tag.lower()
    prefix, separator, name = tag.partition(":")
    if not separator:
        name, prefix = prefix, None
    if name not in {"header", "hidden"}:
        return False
    attribute = "xmlns:" + prefix if prefix is not None else "xmlns"
    for ancestor in (element, *element.iterancestors()):
        namespace = ancestor.get(attribute)
        if namespace is not None:
            return namespace in _INLINE_NAMESPACES
    # Retain the established cleanup of legacy fragments with undeclared ix
    # tags. An explicit foreign namespace always takes precedence above.
    return prefix == "ix"


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


def _note_reference(element, tag):
    """A superscript whose whole content is a note reference such as 1, (2) or (a)."""
    style = element.get("style", "")
    # Filing software raises note references with <sup>, with a superscript
    # alignment, or with relatively positioned text moved up by a few points.
    raised = (tag == "sup" or re.search(r"vertical-align\s*:\s*super", style, re.I)
              or (re.search(r"position\s*:\s*relative", style, re.I)
                  and re.search(r"(?:^|;)\s*top\s*:\s*-\s*[0-9.]*[1-9]", style, re.I)))
    if not raised:
        return False
    return bool(_NOTE_REFERENCE.fullmatch("".join(element.itertext()).strip()))


def _render(element, anchor=None, row_anchor=None, *, track_links=False, enclosing_link=None, note_marks=False):
    """Render annotated fragments while leaving the original DOM untouched."""
    if not isinstance(element.tag, str):  # Comments and processing instructions.
        return []
    tag = element.tag.lower()
    if tag in _OMITTED_TAGS or _omitted_inline_container(element) or _hidden(element):
        return []
    if tag == 'a':
        enclosing_link = element
    if tag == "br":
        source_anchor = row_anchor if row_anchor is not None else anchor
        return [("\n", (source_anchor, enclosing_link) if track_links else source_anchor)]
    if tag == "tr":
        cells = [child for child in element
                 if isinstance(child.tag, str) and child.tag.lower() in {"td", "th"}
                 and not _hidden(child)]
        if cells:
            rendered = [fragments for cell in cells
                        if (fragments := _trim_fragments(_render(cell, element, element,
                            track_links=track_links, enclosing_link=enclosing_link, note_marks=note_marks)))]
            if not rendered:
                return []
            fragment_anchor = (element, enclosing_link) if track_links else element
            fragments = [("\n", fragment_anchor)]
            for index, cell in enumerate(rendered):
                if index:
                    fragments.append((" | ", fragment_anchor))
                fragments.extend(cell)
            fragments.append(("\n", fragment_anchor))
            return fragments
        return []

    is_block = tag in _BLOCK_TAGS
    if row_anchor is not None:
        anchor = row_anchor
    elif is_block or anchor is None:
        anchor = element
    fragment_anchor = (anchor, enclosing_link) if track_links else anchor
    fragments = [("\n\n", fragment_anchor)] if is_block else []
    if note_marks and _note_reference(element, tag):
        fragments.append((_NOTE_MARK, fragment_anchor))
    if element.text:
        fragments.append((re.sub(r"\s+", " ", element.text), fragment_anchor))
    for child in element:
        fragments.extend(_render(child, anchor, row_anchor,
                                 track_links=track_links, enclosing_link=enclosing_link, note_marks=note_marks))
        # Tail text belongs to the visible parent even when the child is hidden.
        if child.tail:
            fragments.append((re.sub(r"\s+", " ", child.tail), fragment_anchor))
    if is_block:
        fragments.append(("\n\n", fragment_anchor))
    return fragments


def _normalize_fragments(fragments):
    """Normalize spaces and keep at most one blank line with source anchors."""
    text, anchors = [], []
    newlines = 0
    space = False
    for fragment, anchor in fragments:
        if isinstance(fragment, _NoteMark):
            # "1,234" followed by note 2 must not read "1,2342", nor "revenues"
            # followed by note 1 "revenues1". A unit exponent (m2) stays joined.
            if text and not newlines and not space:
                run = 0
                while run < 3 and run < len(text) and text[-1 - run].isalpha():
                    run += 1
                if text[-1].isdigit() or text[-1] in "%)" or run >= 3:
                    space = True
            continue
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


def _paragraph_contents_offsets(full_text):
    """Recognize compact unlinked contents entries with separate page lines."""
    lines, offset = [], 0
    for line in full_text.splitlines(keepends=True):
        value = _heading_text(line)
        if value:
            lines.append((offset, value))
        offset += len(line)
    excluded = set()
    for index, (start, title) in enumerate(lines):
        if not re.fullmatch(r'TABLE OF CONTENTS', title, re.I):
            continue
        candidates, pending_parts, item_count = [], [], 0
        for following in range(index + 1, min(index + 81, len(lines))):
            offset, value = lines[following]
            if offset - start > 5000 or len(value) > 240 or re.fullmatch(r'TABLE OF CONTENTS', value, re.I):
                break
            if _ITEM_HEADING.match(value):
                page = lines[following + 1][1] if following + 1 < len(lines) else ''
                if not re.fullmatch(r'\d+(?:[-–]\d+)?', page):
                    # The first heading with body text ends the contents block.
                    break
                candidates.extend(pending_parts)
                pending_parts.clear()
                candidates.append(offset)
                item_count += 1
            elif _PART_HEADING.fullmatch(value):
                # A trailing Part may introduce the actual body. Commit it
                # only when a subsequent Item/page pair confirms TOC content.
                pending_parts.append(offset)
        # A lone reserved Item followed by a page footer is a body section.
        if item_count >= 2:
            excluded.update(candidates)
    return excluded


def _navigation_heading_offsets(full_text, anchors, heading_links):
    """Identify TOC headings from their original DOM, before losing blank cells.

    A navigation link is different from an empty named body anchor. Unlinked
    indexes also occur in SEC filings: several Item rows together with page
    columns establish a compact contents table. Neither a table alone nor a
    single empty layout cell is evidence of navigation.
    """
    excluded, tables = _paragraph_contents_offsets(full_text), {}
    offset = 0
    for line in full_text.splitlines(keepends=True):
        heading = _heading_text(line)
        if not (_ITEM_HEADING.match(heading) or _PART_HEADING.fullmatch(heading)):
            offset += len(line)
            continue
        anchor = anchors[offset]
        if anchor is None:
            offset += len(line)
            continue
        # Track the links covering the heading itself. A shared table-row
        # anchor may also contain prose links with the exact same label.
        covering = [heading_links[offset]]
        # A contents row can link only its title cell ("Item 1. | <a>Financial
        # Statements</a>"), leaving the Item label itself unlinked.
        label = _ITEM_LABEL.match(line)
        if label and line[label.end():].strip():
            covering.append(heading_links[offset + label.end()])
        local_targets = None
        for link in covering:
            if link is None or not link.get('href', '').startswith('#'):
                continue
            if local_targets is None:
                local_targets = {value for node in anchor.iter()
                                 for value in (node.get('id'), node.get('name')) if value}
                local_targets.update(value for node in anchor.iterancestors()
                                     for value in (node.get('id'), node.get('name')) if value)
            # A heading's own permalink points within its source block and
            # is not a table-of-contents link to a separate body section.
            if unquote(link.get('href')[1:]) not in local_targets:
                excluded.add(offset)
        table = next((node for node in anchor.iterancestors() if node.tag == 'table'), None)
        if table is not None:
            if table not in tables:
                rows = table.xpath('./tr|./tbody/tr|./thead/tr|./tfoot/tr')
                row_texts = [' '.join(' '.join(row.itertext()).split()) for row in rows]
                item_rows = sum(bool(_ITEM_HEADING.match(value)) for value in row_texts)
                page_rows = sum(bool(re.search(r'\s\d+(?:[-–]\d+)?$', value)) for value in row_texts)
                tables[table] = (item_rows >= 3 and page_rows >= 3
                                 and sum(map(len, row_texts)) < 15000)
            if tables[table]:
                excluded.add(offset)
        offset += len(line)
    return frozenset(excluded)


def _linked_exhibit_headings(document, full_text, anchors, form):
    """Resolve a quarterly Item 6 whose body is titled 'Index to Exhibits'.

    The original TOC must explicitly link Item 6 / Exhibits to a unique nearby
    body anchor. A matching title alone does not assign a guessed Item number.
    """
    if form is None or form.upper().removesuffix('/A') != '10-Q':
        return {}
    tree = document.getroottree()
    order = {node: index for index, node in enumerate(document.iter())}
    lines, position = [], 0
    for line in full_text.splitlines(keepends=True):
        anchor = anchors[position]
        if anchor is not None and line.strip():
            lines.append((position, _heading_text(line), anchor))
        position += len(line)
    aliases = [(offset, title, node) for offset, title, node in lines
               if re.fullmatch(r'(?:INDEX TO )?EXHIBITS[ .]*', title, re.I)]
    if not aliases:
        return {}
    result = {}
    for row in document.xpath('//tr'):
        title = ' '.join(' '.join(''.join(cell.itertext()).split()) for cell in row.xpath('./td|./th'))
        if not re.fullmatch(r'ITEM\s+6\s*\.?\s+EXHIBITS(?:\s+\d+)?', title.strip(), re.I):
            continue
        fragments = {unquote(link.get('href')[1:]) for link in row.xpath('.//a[starts-with(@href,"#")]')}
        if len(fragments) != 1:
            continue
        target_id, = fragments
        targets = document.xpath('//*[@id=$target or @name=$target]', target=target_id)
        if len(targets) != 1:
            continue
        target = targets[0]
        candidates = [(offset, node) for offset, _, node in aliases
                      if 0 <= order[node] - order[target] <= 30]
        if len(candidates) != 1:
            continue
        offset, node = candidates[0]
        containing_blocks = set(target.iterancestors())
        intervening = [text for start, text, anchor in lines if start < offset and (
            order[target] <= order[anchor] < order[node] or anchor in containing_blocks)]
        if any(not re.fullmatch(r'(?:TABLE OF CONTENTS|\d+)', text, re.I) for text in intervening):
            continue
        result[offset] = {'label': 'ITEM 6. EXHIBITS', 'evidence': {
            'heading_method': 'original_toc_target', 'toc_xpath': source_xpath(tree, row),
            'target_xpath': source_xpath(tree, target), 'target_element_id': target_id}}
    return result


def _original_statement_tables(document):
    """Find headed statement bodies, excluding a list of statement/page references.

    This corroborates broadened appendix labels; it is not the acceptance gate.
    Acceptance separately maps every actually selected primary statement table.
    """
    from .tables import _hidden, _statement_headings, local

    found = []
    for table in document.xpath('//table'):
        if table.xpath('.//table') or _hidden(table) or not _statement_headings(table):
            continue
        if any(local(node) == 'nonfraction' and not _hidden(node) for node in table.iter()):
            found.append(table)
            continue
        # Plain-HTML statement fixtures and older untagged statements still
        # need a financial row label and a separate numeric value, not a page.
        for row in table.xpath('./tr|./thead/tr|./tbody/tr|./tfoot/tr'):
            cells = [' '.join(''.join(cell.itertext()).split()) for cell in row.xpath('./td|./th')]
            cells = [value for value in cells if value]
            if (len(cells) > 1 and re.fullmatch(
                    r'(?:total )?(?:assets|liabilities)(?: and (?:stockholders|shareholders).? equity)?'
                    r'|cash(?: and cash equivalents)?|net (?:income|earnings|loss)|net cash.*|revenues?|sales',
                    cells[0], re.I) and any(re.fullmatch(r'[$(+-]?\s*\d[\d,.]*\s*\)?', value)
                                           for value in cells[1:])):
                found.append(table)
                break
    return found


def _statement_table_follows(anchor, limit, tables, order):
    start = order[anchor]
    return any(start <= order[table] < limit or
               (order[table] < start < limit and anchor in table.iterdescendants()) for table in tables)


def _separate_unnumbered_blocks(full_text, sections, anchors, heading_links, document, *, audited=False):
    """Keep explicit body appendices from inheriting the previous Item's topic.

    These are standalone source headings, never prose mentions. In particular,
    an index after Part IV introduces the separately paginated financials that
    some issuers incorporate by reference in Item 8.
    """
    if not sections:
        return sections
    added, seen, offset = [], set(), 0
    statement_tables, order = None, None
    for line in full_text.splitlines(keepends=True):
        title = _heading_text(line)
        owner = next((section for section in reversed(sections)
                      if section['offset_start'] <= offset), None)
        key, kind = None, None
        if owner and owner.get('section_id') not in {'front-matter', 'full-report', 'unclassified-prefix'}:
            if (owner.get('section_type') in {'exhibits', 'summary'}
                    and re.fullmatch(r'SIGNATURES?[ .]*', title, re.I)):
                key, kind = 'signatures', 'signatures' if audited else 'other_information'
            elif (owner.get('part') == 'IV' and re.fullmatch(
                    r'(?:(?:INDEX TO )?(?:THE )?(?:(?:CONDENSED|CONSOLIDATED|COMBINED) )*'
                    r'FINANCIAL STATEMENTS(?: AND (?:FINANCIAL STATEMENT )?SCHEDULES?)?'
                    r'|REPORTS? OF (?:THE )?INDEPENDENT REGISTERED PUBLIC ACCOUNTING FIRMS?'
                    r'|(?:(?:CONDENSED|CONSOLIDATED|COMBINED) )*(?:BALANCE SHEETS?'
                    r'|STATEMENTS? OF (?:INCOME|EARNINGS|OPERATIONS|CASH FLOWS?|FINANCIAL POSITION)'
                    r'(?: AND COMPREHENSIVE (?:INCOME|LOSS))?)(?: \(LOSS\))?(?: \(UNAUDITED\))?)[ .]*', title, re.I)):
                key, kind = 'financial-statements-appendix', 'financial_statements'
            elif (owner.get('section_type') == 'mine_safety' and re.fullmatch(
                    r'(?:INFORMATION (?:ABOUT|REGARDING) )?(?:OUR |THE )?EXECUTIVE OFFICERS(?: OF THE REGISTRANT)?[ .]*',
                    title, re.I)):
                key, kind = 'executive-officers', 'directors_governance'
        elif (owner and owner.get('section_id') == 'full-report' and re.fullmatch(
                r'(?:(?:INDEX TO )?(?:THE )?(?:(?:CONDENSED|CONSOLIDATED|COMBINED) )*'
                r'FINANCIAL STATEMENTS(?: AND (?:FINANCIAL STATEMENT )?SCHEDULES?)?'
                r'|(?:(?:CONDENSED|CONSOLIDATED|COMBINED) )*(?:BALANCE SHEETS?'
                r'|STATEMENTS? OF (?:INCOME|EARNINGS|OPERATIONS|CASH FLOWS?|FINANCIAL POSITION)'
                r'(?: AND COMPREHENSIVE (?:INCOME|LOSS))?)(?: \(LOSS\))?(?: \(UNAUDITED\))?)[ .]*', title, re.I)):
            # A supplemental statement member can have no numbered Items at all.
            # Its standalone statement heading still establishes financial ownership.
            key, kind = 'financial-statements-appendix', 'financial_statements'
        link = heading_links[offset]
        if key and key not in seen and not (link is not None and link.get('href')):
            anchor = anchors[offset]
            if key == 'financial-statements-appendix' and not re.fullmatch(
                    r'INDEX TO (?:THE )?(?:CONSOLIDATED )?FINANCIAL STATEMENTS[ .]*', title, re.I):
                if statement_tables is None:
                    statement_tables = _original_statement_tables(document)
                    order = {node: index for index, node in enumerate(document.iter())}
                end = owner['offset_end']
                limit = order[anchors[end]] if end < len(anchors) else len(order)
                if anchor is None or not _statement_table_follows(anchor, limit, statement_tables, order):
                    offset += len(line)
                    continue
            # A table of contents can link only the page column. Such rows
            # are navigation even when the title itself has no hyperlink.
            if anchor is not None and not (anchor.tag == 'tr' and anchor.xpath('.//a[starts-with(@href,"#")]')):
                seen.add(key)
                added.append({'section_id': key, 'section_type': kind, 'title': title,
                              'original_title': title, 'part': None, 'item': None,
                              'offset_start': offset, 'validation_status': 'detected'})
        offset += len(line)
    if any(section['offset_start'] == 0 for section in added):
        sections = [section for section in sections if section['section_id'] != 'full-report']
    boundaries = sorted(sections + added, key=lambda row: row['offset_start'])
    for index, section in enumerate(boundaries):
        section['offset_end'] = boundaries[index + 1]['offset_start'] if index + 1 < len(boundaries) else len(full_text)
    return boundaries


def clean_report(html_path: str | Path, *, form: str | None = None,
                 report_version: str | None = None) -> CleanedReport:
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
    audited = report_version in AUDITED_REPORT_VERSIONS
    # No nodes are removed or rewritten: anchors always refer to the original DOM.
    full_text, annotations = _normalize_fragments(_render(document, track_links=True, note_marks=audited))
    # Every parse of an original document reads its bytes as UTF-8. Bytes in
    # another encoding (Windows-1252 quotes, dashes, no-break spaces) become
    # U+FFFD, which would be stored as report text and, in a heading, hide the
    # section. A replacement character the source does not itself contain is a
    # decoding loss, never report content.
    if "\ufffd" in full_text and not (b"\xef\xbf\xbd" in source
                                      or re.search(rb"&#(?:0*65533|x0*fffd);", source, re.I)):
        raise ReportEncodingError(
            "The document is not UTF-8: its visible text cannot be read without replacement characters.")
    anchors = [annotation[0] if annotation is not None else None for annotation in annotations]
    heading_links = [annotation[1] if annotation is not None else None for annotation in annotations]
    form = _report_form(full_text, form)
    sections = detect_sections(full_text, form=form,
                               excluded_heading_offsets=_navigation_heading_offsets(full_text, anchors, heading_links),
                               linked_headings=_linked_exhibit_headings(document, full_text, anchors, form), audited=audited)
    sections = _separate_unnumbered_blocks(full_text, sections, anchors, heading_links, document, audited=audited)
    tree = document.getroottree()
    for section in sections:
        anchor = anchors[section["offset_start"]]
        locator = {"source_file": html_path.name, "xpath": source_xpath(tree, anchor)}
        if anchor.get("id"):
            locator["element_id"] = anchor.get("id")
        section["source_locator"] = section.get("source_locator", {}) | locator
    return CleanedReport(full_text=full_text, sections=sections)
