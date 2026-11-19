"""Recover bounded, original-DOM statement headings for conservative V2 use.

This module does not select financial tables. Callers must still require the
independent statement concept groups and reject competing financial tables.
Legacy table selection can remain unchanged and use these helpers on fallback.
"""

from contextlib import contextmanager
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


_VISIBILITY = None


@contextmanager
def cached_visibility():
    """Remember which nodes are hidden while one unchanged document is read.

    `_hidden` walks every ancestor of a node, and the title search asks it of
    every node of every block near every table. Within this context the answer
    for a node is computed once. The document must not be modified meanwhile.
    """
    global _VISIBILITY
    previous = _VISIBILITY
    if previous is None:
        _VISIBILITY = {}
    try:
        yield
    finally:
        _VISIBILITY = previous


def _hides(element):
    style = re.sub(r"\s+", "", element.get("style", "").lower())
    return bool(_local(element) == "hidden" or "hidden" in element.attrib
                or element.get("aria-hidden", "").lower() == "true"
                or re.search(r"(?:^|;)(?:display:none|visibility:hidden)(?:!important)?(?:;|$)", style))


def _hidden(node):
    cache = _VISIBILITY
    if cache is None:
        return any(_hides(element) for element in [node, *node.iterancestors()])
    known = cache.get(node)
    if known is None:
        parent = node.getparent()
        known = cache[node] = _hides(node) or (parent is not None and _hidden(parent))
    return known


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


_LEGAL_FORMS = (("inc", "incorporated"), ("corp", "corporation"), ("co", "company"), ("ltd", "limited"))
_SUBSIDIARIES = r"(?: and (?:its )?(?:consolidated )?(?:subsidiaries|subsidiary companies))?"
# The `header_lines` reading also takes the name joined by "&", which the
# normalized text no longer shows ("EMERSON ELECTRIC CO. & SUBSIDIARIES").
_LINE_SUBSIDIARIES = r"(?: (?:and )?(?:its )?(?:consolidated )?(?:subsidiaries|subsidiary companies))?"
_TITLE_WORDS = frozenset((
    "condensed consolidated combined interim unaudited audited statement statements of and balance sheet sheets "
    "income operations earnings comprehensive loss total net cash flow flows financial position condition results").split())
_MONTH_WORDS = frozenset(_MONTH[3:-1].split("|"))
_PERIOD_WORDS = _MONTH_WORDS | frozenset((
    "for the fiscal as of at and to through ended ending end month months quarter quarters week weeks year years "
    "period periods first second third fourth one two three four five six seven eight nine ten eleven twelve "
    "thirteen twenty thirty forty fifty unaudited audited respectively").split())
_RUNNING_HEADERS = re.compile(
    r"\b(?:(?:financial )?table of contents|index to (?:consolidated )?financial statements"
    r"|part i{1,3}(?: financial information)?"
    r"|item [0-9]{1,2}[ab]? (?:unaudited )?(?:condensed )?(?:consolidated )?financial statements"
    r"(?: and supplementary data)?(?: unaudited)?|continued)\b")
_LINE_UNITS = re.compile(
    r"(?:(?:un)?audited )?(?:in (?:us|u s)(?: dollars)? and )?"
    r"(?:(?:(?:amounts?|dollars?|u s dollars|us dollars)(?: (?:and )?shares)? )?in )?"
    r"(?:thousands|millions|billions)(?: of (?:u s |us )?dollars)?"
    r"(?: (?:except|and|per share)(?: (?:per|share|shares|par|value|values|data|amounts?|numbers?|and|or|of|in|for|as|where"
    r"|otherwise|noted|indicated|stated|ratios|percentages|percent|units?|common|stock|dollars|related|information))+)?"
    r"(?: (?:un)?audited)?")


def registrant_names(values, *, spellings=False):
    """Normalize the filing's own registrant names for `wide` title matching.

    `spellings` (the `header_lines` reading) drops the state suffix of an SEC
    conformed name ("/NY", "\\DE\\") and gives a closing legal form in both
    its short and its long spelling ("Corp" and "Corporation").
    """
    names = set()
    for value in values:
        if not isinstance(value, str):
            continue
        if spellings:
            value = re.sub(r"\s*[/\\]\s*[A-Za-z]{2,3}\s*[/\\]?\s*$", "", value)
        name = normalized_heading(value)
        if len(name) < 3:
            continue
        names.add(name)
        head, _, last = name.rpartition(" ")
        if spellings and head:
            names.update(f"{head} {form}" for forms in _LEGAL_FORMS if last in forms for form in forms)
    return tuple(sorted(names, key=len, reverse=True))


def _spaced(name):
    """A pattern for `name` however its letters are spaced ("AutoZone", "AUTOZONE")."""
    return r" ?".join(re.escape(character) for character in name.replace(" ", ""))


def _registrant_prefix(value, registrants, *, spaced=False):
    """The length of a leading registrant name, with "and subsidiaries", or 0."""
    for name in registrants:
        match = re.match((_spaced(name) + _LINE_SUBSIDIARIES if spaced else re.escape(name) + _SUBSIDIARIES)
                         + r"(?: |$)", value)
        if match:
            return match.end()
    return 0


def _tokens(value):
    """Lower-case words and numbers, with qualifiers and company prose kept."""
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"([a-z])([A-Z])", r"\1 \2", value).lower()).strip()


def _split_glued(value, words):
    """Separate words printed without a space ("CONSOLIDATEDBALANCE SHEETS").

    A token is split only when all of it is made of `words`.
    """
    def split(token):
        if token in words or not token.isalpha():
            return token
        parts, rest = [], token
        while rest:
            size = next((size for size in range(len(rest), 1, -1) if rest[:size] in words), 0)
            if not size:
                return token
            parts.append(rest[:size])
            rest = rest[size:]
        return " ".join(parts)
    return " ".join(split(token) for token in value.split())


def _line_metadata(value, registrants):
    """Header lines printed between a title and its table (`header_lines`).

    The whole text must be made of the registrant's name, a units statement,
    period words and dates, running page headers and page numbers. Any other
    word, such as the first word of a sentence or of a note, fails.
    """
    def units(match):
        inner = _tokens(match.group(1))
        return " " if not inner or inner in {"unaudited", "audited"} or _LINE_UNITS.fullmatch(inner) else match.group(0)
    text = _tokens(re.sub(r"\(([^()]*)\)", units, value))
    text = _RUNNING_HEADERS.sub(" ", text)
    for name in registrants:
        text = re.sub(r"\b" + _spaced(name) + r"\b" + _LINE_SUBSIDIARIES, " ", text)
    text = _split_glued(" ".join(text.split()), _PERIOD_WORDS)
    if not text or _LINE_UNITS.fullmatch(text):
        return True
    return all(token in _PERIOD_WORDS or re.fullmatch(r"[0-9]{1,3}|(?:19|20)[0-9]{2}", token) for token in text.split())


def statement_kind(value, *, wide=False, registrants=(), header_lines=False):
    """Recognize only a complete supported title, never a substring in prose.

    `wide` (tables-v3 corroborated layouts only) also reads a title printed
    after the filing's own registrant name or before its units, the prefixes
    "interim" and "and", and these complete wordings: a net, loss or
    comprehensive-earnings variant of the income statement, "results of
    operations", and "statement of condition". The whole text must still be
    the title: prose that merely mentions a statement never matches.

    `header_lines` (with `wide`, tried only after `wide` alone found nothing)
    also reads a title whose words are printed without a space, one that
    carries a footnote marker, the registrant's name in another spacing, and
    the cash flow statement titled "Consolidated Cash Flows".
    """
    if _FORBIDDEN.search(_clean(value)):
        return None
    if header_lines:
        value = re.sub(r"\(\s*[0-9]\s*\)", " ", value)
    value = normalized_heading(value)
    if header_lines:
        value = _split_glued(value, _TITLE_WORDS)
    if wide:
        value = value[_registrant_prefix(value, registrants, spaced=header_lines):]
        value = re.sub(r"\s+(?:(?:amounts|dollars|u s dollars)\s+)?in (?:thousands|millions|billions)\b.*$", "", value)
        if header_lines and re.fullmatch(r"(?:(?:condensed|consolidated|combined|unaudited|interim) )+cash flows", value):
            return "cash_flow"
        value = re.sub(r"^(?:(?:condensed|consolidated|combined|unaudited|audited|interim|and)\s+)+", "", value)
    value = re.sub(r"^(?:(?:condensed|consolidated|combined|unaudited|audited)\s+)+", "", value)
    value = re.sub(r"^(statements? of )(?:(?:condensed|consolidated|combined)\s+)+", r"\1", value)
    if re.fullmatch(r"(?:statements? of (?:income|operations|earnings)(?: loss)?(?: and comprehensive (?:income|loss)(?: loss)?)?"
                    r"|statements? of comprehensive (?:income|loss)(?: loss)?|(?:income|operations|earnings) statements?)", value):
        return "income_statement"
    if re.fullmatch(r"(?:balance sheets?|statements? of financial (?:position|condition))", value):
        return "balance_sheet"
    if re.fullmatch(r"(?:statements? of cash flows?|cash flows? statements?)", value):
        return "cash_flow"
    if wide:
        if re.fullmatch(r"(?:statements? of (?:net )?(?:income|operations|earnings|loss)(?: (?:loss|income|earnings))?"
                        r"(?: and (?:total |other )?comprehensive (?:income|loss|earnings)(?: (?:loss|income))?)?"
                        r"|statements? of comprehensive (?:income|loss|earnings)(?: (?:loss|income))?"
                        r"|(?:statements? of )?results of operations)", value):
            return "income_statement"
        if re.fullmatch(r"statements? of condition", value):
            return "balance_sheet"
    return None


def _metadata(value, registrants=(), header_lines=False):
    value = _clean(value)
    if not value:
        return True
    if _FORBIDDEN.search(value):
        return False
    if header_lines and (not re.search(r"[A-Za-z0-9]", value) or _line_metadata(value, registrants)):
        return True
    stripped = _without_qualifiers(value).strip(" ()").strip()
    if not stripped or stripped.lower() in {"unaudited", "audited"} or _units(stripped):
        return True
    if registrants:
        # The filing's own registrant name, whatever its legal form.
        normalized = normalized_heading(stripped)
        if normalized and _registrant_prefix(normalized, registrants) == len(normalized):
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


def _block_headings(node, *, wide=False, registrants=(), header_lines=False):
    """Return local title nodes only when surrounding text is header metadata."""
    if _numeric(node):
        return ()
    visible = _visible_text(node)
    if not visible or len(visible) > 640 or _FORBIDDEN.search(visible):
        return ()
    kind = (lambda text: statement_kind(text, wide=True, registrants=registrants, header_lines=header_lines)) if wide else statement_kind
    candidates = []
    for child in node.iter():
        if not isinstance(child.tag, str) or _hidden(child):
            continue
        if any(ancestor in candidates for ancestor in child.iterancestors()):
            continue
        value = _visible_text(child)
        if len(value) <= 320 and kind(value):
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
    if not _metadata(remainder, registrants if wide else (), header_lines):
        return ()
    tree = node.getroottree()
    evidence = []
    for child in candidates:
        value = _visible_text(child)
        evidence.append(HeadingEvidence(
            kind=kind(value), text=value, signature=normalized_heading(value),
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


def _two_line_heading(second, registrants):
    """A title printed over two lines, the second being `second` (`header_lines`).

    The break must fall inside the title: the first line ends in "of" or
    "and", or the second begins with "and". Together the two lines must be a
    complete title, and neither may hold a table or a number.
    """
    first = second.getprevious()
    while first is not None and not _visible_text(first):
        first = first.getprevious()
    if first is None:
        return ()
    lines = [_visible_text(first), _visible_text(second)]
    if any(len(line) > 200 or _numeric(node) or _local(node) == "table" or node.xpath(".//table")
           for line, node in zip(lines, (first, second))):
        return ()
    words = [normalized_heading(line).split() for line in lines]
    if not all(words) or not (words[0][-1] in {"of", "and"} or words[1][0] == "and"):
        return ()
    value = " ".join(lines)
    kind = statement_kind(value, wide=True, registrants=registrants, header_lines=True)
    if kind is None:
        return ()
    return (HeadingEvidence(kind=kind, text=value, signature=normalized_heading(value),
                            continued=bool(_CONTINUATION.search(value)),
                            consolidated=bool(re.search(r"\bconsolidated\b", value, re.I)),
                            source_xpath=source_xpath(first.getroottree(), first)),)


def _previous(node, max_empty, max_blocks, *, wide=False, registrants=(), header_lines=False):
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
            found = _block_headings(sibling, wide=wide, registrants=registrants, header_lines=header_lines)
            if not found and header_lines:
                found = _two_line_heading(sibling, registrants)
            if found:
                return found, False
            if (_local(sibling) == "table" or sibling.xpath(".//table")
                    or _numeric(sibling) or not _metadata(value, registrants if wide else (), header_lines)):
                return (), False
        sibling = sibling.getprevious()
    return (), True


def recover_headings(table, *, max_empty=128, max_blocks=8, max_ancestors=4, wide=False, registrants=(),
                     header_lines=False):
    """Recover one title from its own header or nearest bounded header blocks.

    Empty anchors do not consume the meaningful-block budget. Parent traversal
    is allowed only past empty/known metadata siblings, and never through a
    containing table/cell, preceding financial table, note, or prose boundary.
    `wide` and `registrants` are described at `statement_kind`.

    `header_lines` (with `wide`) also passes the lines a filing prints between
    a title and its table, as `_line_metadata` defines them, and reads a title
    printed over two lines.
    """
    if _local(table) != "table" or _hidden(table):
        return ()
    own = []
    for row in [*table.xpath("./caption"), *table.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr")[:24]]:
        if _numeric(row):
            break
        if _FORBIDDEN.search(_visible_text(row)):
            return ()
        own.extend(_block_headings(row, wide=wide, registrants=registrants, header_lines=header_lines))
    if own:
        return _unique(own)
    node = table
    for _ in range(max_ancestors + 1):
        found, may_ascend = _previous(node, max_empty, max_blocks, wide=wide, registrants=registrants,
                                      header_lines=header_lines)
        if found or not may_ascend:
            return found
        node = node.getparent()
        if node is None or _local(node) in {"body", "html", "td", "th", "table"}:
            break
    return ()
