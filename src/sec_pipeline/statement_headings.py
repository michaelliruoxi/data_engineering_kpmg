"""Recover bounded, original-DOM statement headings for conservative V2 use.

This module does not select financial tables. Callers must still require the
independent statement concept groups and reject competing financial tables.
Legacy table selection can remain unchanged and use these helpers on fallback.
"""

from dataclasses import dataclass
import re

from .source_locators import source_xpath


_BLOCKS = {"div", "p", "tr", "td", "th", "caption", "h1", "h2", "h3", "h4", "br"}
_MONTH = r"(?:january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
_FORBIDDEN = re.compile(r"\b(?:notes?\s+(?:to|[0-9])|supplemental|consolidating|parent\s+(?:company|only)|segment\s+(?:reporting|information)|selected\s+(?:financial|data))\b", re.I)
_CONTINUATION = re.compile(r"[\s:–—-]*(?:(?:\(\s*)?continued(?:\s*\))?|\(\s*cont\.\s*\))\s*$", re.I)


@dataclass(frozen=True)
class HeadingEvidence:
    kind: str
    text: str
    signature: str
    continued: bool
    consolidated: bool
    source_xpath: str


def _local(node):
    return str(node.tag).split("}")[-1].split(":")[-1].lower()


def _clean(value):
    value = re.sub(r"[\u00ad\u200b-\u200d\ufeff]", "", value)
    return " ".join(value.split())


def _hidden(node):
    for element in [node, *node.iterancestors()]:
        style = re.sub(r"\s+", "", element.get("style", "").lower())
        if (_local(element) == "hidden" or "hidden" in element.attrib
                or element.get("aria-hidden", "").lower() == "true"
                or re.search(r"(?:^|;)(?:display:none|visibility:hidden)(?:!important)?(?:;|$)", style)):
            return True
    return False


def _visible_text(node):
    if _hidden(node):
        return ""
    pieces = [node.text or ""]
    for child in node:
        if isinstance(child.tag, str):
            separator = " " if _local(child) in _BLOCKS else ""
            pieces.extend((separator, _visible_text(child), separator))
        pieces.append(child.tail or "")
    return _clean("".join(pieces))


def _units(value):
    return bool(re.fullmatch(r"\$000", value.strip()) or re.fullmatch(
        r"(?:(?:amounts|dollars)\s+)?in\s+(?:thousands|millions|billions)(?:[,;]\s*(?:unaudited|audited|except\s+[a-z ,.;-]+))*", value.strip(), re.I))


def _without_qualifiers(value):
    def remove(match):
        inner = match.group(1).strip()
        return " " if inner.lower() in {"unaudited", "audited"} or _units(inner) else match.group(0)
    value = re.sub(r"\(([^()]*)\)", remove, value)
    value = re.sub(r"[\s:–—,-]+(?:unaudited|audited)\s*$", "", value, flags=re.I)
    return value.strip()


def normalized_heading(value):
    """Normalize known formatting qualifiers, without stripping company prose."""
    value = _clean(value)
    value = _CONTINUATION.sub("", value)
    value = _without_qualifiers(value)
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def statement_kind(value):
    """Recognize only a complete supported title, never a substring in prose."""
    if _FORBIDDEN.search(_clean(value)):
        return None
    value = normalized_heading(value)
    value = re.sub(r"^(?:(?:condensed|consolidated|combined|unaudited|audited)\s+)+", "", value)
    value = re.sub(r"^(statements? of )(?:(?:condensed|consolidated|combined)\s+)+", r"\1", value)
    if re.fullmatch(r"(?:statements? of (?:income|operations|earnings)(?: loss)?(?: and comprehensive (?:income|loss)(?: loss)?)?"
                    r"|statements? of comprehensive (?:income|loss)(?: loss)?|(?:income|operations|earnings) statements?)", value):
        return "income_statement"
    if re.fullmatch(r"(?:balance sheets?|statements? of financial (?:position|condition))", value):
        return "balance_sheet"
    if re.fullmatch(r"(?:statements? of cash flows?|cash flows? statements?)", value):
        return "cash_flow"
    return None


def _metadata(value):
    value = _clean(value)
    if not value:
        return True
    if _FORBIDDEN.search(value):
        return False
    stripped = _without_qualifiers(value).strip(" ()").strip()
    if not stripped or stripped.lower() in {"unaudited", "audited"} or _units(stripped):
        return True
    company = re.fullmatch(r"[a-z0-9&][a-z0-9&'., -]{0,150}\b(?:inc|incorporated|corporation|corp|co|company|companies|ltd|limited|plc)\.?"
                           r"(?:\s+and\s+subsidiaries)?", stripped, re.I)
    if company:
        return True
    return bool(re.fullmatch(r"(?:for\s+)?(?:the\s+)?(?:(?:one|two|three|six|nine|twelve|[0-9]{1,2})\s+)?"
                             r"(?:fiscal\s+)?(?:months?|weeks?|quarters?|years?)\s+ended\s+"
                             + _MONTH + r"\.?\s+[0-9]{1,2},?(?:\s+(?:19|20)[0-9]{2})?,?", stripped, re.I))


def _numeric(node):
    return any(_local(child) == "nonfraction" and not _hidden(child) for child in node.iter())


def _block_headings(node):
    """Return local title nodes only when surrounding text is header metadata."""
    if _numeric(node):
        return ()
    visible = _visible_text(node)
    if not visible or len(visible) > 640 or _FORBIDDEN.search(visible):
        return ()
    candidates = []
    for child in node.iter():
        if not isinstance(child.tag, str) or _hidden(child):
            continue
        if any(ancestor in candidates for ancestor in child.iterancestors()):
            continue
        value = _visible_text(child)
        if len(value) <= 320 and statement_kind(value):
            candidates.append(child)
    if not candidates:
        return ()

    def residual(element):
        if element in candidates or _hidden(element):
            return []
        values = [element.text or ""]
        for child in element:
            if isinstance(child.tag, str):
                values.extend(residual(child))
            values.append(child.tail or "")
        return values

    remainder = _clean(" ".join(residual(node)))
    if not _metadata(remainder):
        return ()
    tree = node.getroottree()
    evidence = []
    for child in candidates:
        value = _visible_text(child)
        evidence.append(HeadingEvidence(
            kind=statement_kind(value), text=value, signature=normalized_heading(value),
            continued=bool(_CONTINUATION.search(value)),
            consolidated=bool(re.search(r"\bconsolidated\b", value, re.I)),
            source_xpath=source_xpath(tree, child),
        ))
    return _unique(evidence)


def _unique(evidence):
    by_signature = {(item.kind, item.signature): item for item in evidence}
    # A block naming two statements, or two conflicting title identities, is
    # not reliable evidence for choosing either one.
    return tuple(by_signature.values()) if len(by_signature) == 1 else ()


def _previous(node, max_empty, max_blocks):
    empty = blocks = 0
    sibling = node.getprevious()
    while sibling is not None:
        if _numeric(sibling):
            return (), False
        value = _visible_text(sibling)
        if not value:
            empty += 1
            if empty > max_empty:
                return (), False
        else:
            blocks += 1
            if blocks > max_blocks:
                return (), False
            found = _block_headings(sibling)
            if found:
                return found, False
            if (_local(sibling) == "table" or sibling.xpath(".//table")
                    or _numeric(sibling) or not _metadata(value)):
                return (), False
        sibling = sibling.getprevious()
    return (), True


def recover_headings(table, *, max_empty=128, max_blocks=8, max_ancestors=4):
    """Recover one title from its own header or nearest bounded header blocks.

    Empty anchors do not consume the meaningful-block budget. Parent traversal
    is allowed only past empty/known metadata siblings, and never through a
    containing table/cell, preceding financial table, note, or prose boundary.
    """
    if _local(table) != "table" or _hidden(table):
        return ()
    own = []
    for row in [*table.xpath("./caption"), *table.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr")[:24]]:
        if _numeric(row):
            break
        if _FORBIDDEN.search(_visible_text(row)):
            return ()
        own.extend(_block_headings(row))
    if own:
        return _unique(own)
    node = table
    for _ in range(max_ancestors + 1):
        found, may_ascend = _previous(node, max_empty, max_blocks)
        if found or not may_ascend:
            return found
        node = node.getparent()
        if node is None or _local(node) in {"body", "html", "td", "th", "table"}:
            break
    return ()
