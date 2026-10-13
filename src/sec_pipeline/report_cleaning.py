
import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup


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
            parts.append({"offset_start": offsets[index], "part": part_match.group(1).upper(),
                          "title": heading, "line": index})
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
            intervening = full_text[candidate["offset_start"] + len(lines[candidate["line"]]):first_item]
            if re.search(r"[^\s\d|.]", intervening):
                # The only available Part label may be in a distant TOC.
                continue
            # A part heading between body items is handled once; repeated page
            # headings are not made into additional section boundaries.
            boundaries.append(candidate | {"section_id": f"part-{part.lower()}",
                                             "section_type": "part_heading", "item": None})
    if not selected:
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


def clean_report(html_path: str | Path, *, form: str | None = None) -> CleanedReport:
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
    # Normalize table rows while preserving cell boundaries.
    for row in soup.find_all("tr"):

    # Remove rows hidden by inline CSS.
        style = re.sub(r"\s+", "", row.get("style", "").lower())

        if "display:none" in style or "visibility:collapse" in style:
            row.decompose()
            continue

        cells = row.find_all(["td", "th"], recursive=False)

        if not cells:
            continue

        # Remove rows whose cells contain no visible text.
        if all(not cell.get_text(strip=True) for cell in cells):
            row.decompose()
            continue
        # Combine cells into one readable table row.
        cell_texts = []

        for cell in cells:
            for br in cell.find_all("br"):
                br.replace_with("\n")

            cell_text = cell.get_text(separator=" ", strip=True)
            cell_texts.append(" ".join(cell_text.split()))

        row.replace_with(
            soup.new_string(" | ".join(cell_texts) + " |\n")
        )       

    # Preserve explicit HTML line breaks.
    for br in soup.find_all("br"):
        br.replace_with("\n")

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
    sections = detect_sections(full_text, form=form)

    # Record the source HTML filename for each section.
    for section in sections:
        section.setdefault("source_locator", {})["document"] = html_path.name

    # Return the result to downstream pipeline modules.
    return CleanedReport(
        full_text=full_text,
        sections=sections,
    )
