"""Extract every XBRL fact occurrence from a filing's XML instance as FactInput records.

Extraction is local and offline: no network, no DTDs, no entity expansion, no taxonomy
downloads. It needs no database. store_facts() is the only function that writes, and it
leaves commit or rollback to the caller.

Run `python -m sec_pipeline.fact_extraction --help` for the developer command.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterator, Sequence
from uuid import UUID

from lxml import etree
import psycopg

from .database import DatabaseError, FactInput, connect, seed_manifest, store_fact

DEFAULT_VERSION = "facts-v1"
XML_ROLE = "extracted_xbrl_instance"
HTML_ROLE = "main_inline_xbrl_filing"

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XSD = "http://www.w3.org/2001/XMLSchema"
XML_NS = "http://www.w3.org/XML/1998/namespace"
IX = "http://www.xbrl.org/2013/inlineXBRL"
IXT_PREFIX = "http://www.xbrl.org/inlineXBRL/transformation/"

# Failure categories, matching the team's error contract.
MALFORMED = "malformed_source"
UNSUPPORTED = "unsupported_extraction"

_XML_SPACE = " \t\r\n"
_NUMBER = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DECIMALS = re.compile(r"INF|[+-]?[0-9]+")  # same rule as the table's CHECK constraint
_PRECISION = re.compile(r"INF|[+]?[0-9]+|-0+")
_SIBLING_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Top-level instance elements that are not facts.
_STRUCTURAL = {f"{{{XBRLI}}}context", f"{{{XBRLI}}}unit", f"{{{LINK}}}schemaRef",
               f"{{{LINK}}}linkbaseRef", f"{{{LINK}}}roleRef", f"{{{LINK}}}arcroleRef",
               f"{{{LINK}}}footnoteLink"}
# A concept's standard label is used when the filer supplies one; otherwise the first
# of these alternatives. The role actually used is recorded with each fact.
_LABEL_ROLES = ("http://www.xbrl.org/2003/role/label",
                "http://www.xbrl.org/2003/role/terseLabel",
                "http://www.xbrl.org/2003/role/totalLabel",
                "http://www.xbrl.org/2003/role/verboseLabel")


class FactExtractionError(ValueError):
    """The source as a whole cannot be extracted, or the result is incomplete."""


class _Reject(Exception):
    """One context, unit, or fact cannot be represented faithfully."""

    def __init__(self, category: str, reason: str):
        super().__init__(reason)
        self.category, self.reason = category, reason


@dataclass(frozen=True, kw_only=True)
class FactFailure:
    """A fact occurrence that was found but deliberately not extracted."""
    xpath: str
    concept: str
    element_id: str | None
    category: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class FactExtraction:
    facts: list[FactInput]
    failures: list[FactFailure]
    inventory: dict[str, Any]

    def require_complete(self) -> "FactExtraction":
        """Raise unless every occurrence in the source was extracted."""
        if self.failures:
            first = self.failures[0]
            raise FactExtractionError(
                f"{len(self.failures)} fact occurrence(s) were not extracted; first: "
                f"{first.concept} at {first.xpath}: {first.reason}")
        return self


@dataclass(frozen=True, kw_only=True)
class StoredFacts:
    """Fact UUIDs by occurrence key, valid only within this filing, source, and version."""
    filing_id: UUID
    source_document_id: UUID
    extraction_version: str
    fact_ids: dict[str, UUID]


# --------------------------------------------------------------------------- parsing

def _parse(data: bytes, what: str) -> etree._Element:
    """Parse bytes without network access, DTD loading, or entity expansion."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             dtd_validation=False, huge_tree=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise FactExtractionError(f"{what} is not well-formed XML: {exc.msg}") from exc
    # With a DOCTYPE, an unexpanded entity would silently truncate a value.
    info = root.getroottree().docinfo
    if info.doctype or info.internalDTD is not None:
        raise FactExtractionError(f"{what} declares a DOCTYPE, which is not supported.")
    return root


def _walk(element: etree._Element, path: str) -> Iterator[tuple[etree._Element, str]]:
    """Yield every descendant element with its positional XPath, in document order."""
    position = 0
    for child in element:
        if not isinstance(child.tag, str):  # comment or processing instruction
            continue
        position += 1
        child_path = f"{path}/*[{position}]"
        yield child, child_path
        yield from _walk(child, child_path)


def _resolve(qname: str | None, element: etree._Element) -> tuple[str | None, str]:
    """Resolve `prefix:local` to (namespace URI, local name) using the element's scope."""
    text = (qname or "").strip(_XML_SPACE)
    prefix, separator, local = text.rpartition(":")
    if not local:
        raise _Reject(MALFORMED, "empty qualified name")
    namespace = element.nsmap.get(prefix if separator else None)
    if separator and namespace is None:
        raise _Reject(MALFORMED, f"undeclared namespace prefix in {text!r}")
    return namespace, local


def _expanded(qname: str | None, element: etree._Element) -> str:
    """Return `{namespace}local`, the prefix-independent spelling stored in JSON."""
    namespace, local = _resolve(qname, element)
    return f"{{{namespace}}}{local}" if namespace else local


def _text(element: etree._Element) -> str:
    """Return the element's text exactly; reject child elements instead of dropping them."""
    parts = [element.text or ""]
    for child in element:
        if isinstance(child.tag, str):
            raise _Reject(UNSUPPORTED, "fact has child elements (fraction or tuple content)")
        parts.append(child.tail or "")
    return "".join(parts)


def _is_nil(element: etree._Element) -> bool:
    value = (element.get(f"{{{XSI}}}nil") or "false").strip(_XML_SPACE)
    if value not in {"true", "1", "false", "0"}:
        raise _Reject(MALFORMED, f"invalid xsi:nil value {value!r}")
    return value in {"true", "1"}


def _date(element: etree._Element | None, what: str) -> date:
    text = ((element.text if element is not None else None) or "").strip(_XML_SPACE)
    if not _DATE.fullmatch(text):
        # dateTime and time-zone forms would need rules this version does not define.
        raise _Reject(UNSUPPORTED, f"{what} {text!r} is not a plain YYYY-MM-DD date")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise _Reject(MALFORMED, f"{what} {text!r} is not a calendar date") from exc


def _context(element: etree._Element) -> dict[str, Any]:
    """Read one context: period, entity, and dimensions with their placement."""
    period = element.find(f"{{{XBRLI}}}period")
    if period is None:
        raise _Reject(MALFORMED, "context has no period")
    result: dict[str, Any] = {"period_start": None, "period_end": None, "instant_date": None}
    if period.find(f"{{{XBRLI}}}forever") is not None:
        result["period_kind"] = "forever"
    elif period.find(f"{{{XBRLI}}}instant") is not None:
        result["period_kind"] = "instant"
        result["instant_date"] = _date(period.find(f"{{{XBRLI}}}instant"), "instant")
    else:
        result["period_kind"] = "duration"
        result["period_start"] = _date(period.find(f"{{{XBRLI}}}startDate"), "startDate")
        result["period_end"] = _date(period.find(f"{{{XBRLI}}}endDate"), "endDate")
        if result["period_start"] > result["period_end"]:
            raise _Reject(MALFORMED, "period starts after it ends")

    identifier = element.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}identifier")
    if identifier is None:
        raise _Reject(MALFORMED, "context has no entity identifier")
    result["entity"] = {"scheme": identifier.get("scheme"),
                        "identifier": (identifier.text or "").strip(_XML_SPACE)}

    # Dimensions may sit in <segment> or <scenario>; keep that placement.
    found: list[tuple[dict[str, str], str]] = []
    containers = (("segment", element.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}segment")),
                  ("scenario", element.find(f"{{{XBRLI}}}scenario")))
    for placement, container in containers:
        for member in container if container is not None else ():
            if not isinstance(member.tag, str):
                continue
            if member.tag == f"{{{XBRLDI}}}explicitMember":
                found.append(({"kind": "explicit",
                               "dimension": _expanded(member.get("dimension"), member),
                               "member": _expanded(member.text, member)}, placement))
            elif member.tag == f"{{{XBRLDI}}}typedMember":
                # Exclusive canonical XML keeps the complete typed value together with
                # exactly the namespace declarations it uses.
                value = "".join(
                    etree.tostring(child, method="c14n", exclusive=True,
                                   with_comments=False).decode("utf-8")
                    for child in member if isinstance(child.tag, str))
                if not value:
                    raise _Reject(MALFORMED, "typed dimension has no value")
                found.append(({"kind": "typed",
                               "dimension": _expanded(member.get("dimension"), member),
                               "value": value}, placement))
            else:
                raise _Reject(UNSUPPORTED, f"non-dimensional {placement} content")
    # A context's dimensions are an unordered set; sort for one canonical form.
    found.sort(key=lambda item: (item[0]["dimension"], item[0]["kind"],
                                 item[0].get("member") or item[0].get("value")))
    result["dimensions"] = [dimension for dimension, _ in found]
    result["dimension_containers"] = [placement for _, placement in found]
    return result


def _unit(element: etree._Element) -> dict[str, list[str]]:
    """Read one unit as expanded numerator and denominator measure names."""
    def measures(parent: etree._Element | None) -> list[str]:
        if parent is None:
            return []
        return [_expanded(m.text, m) for m in parent.findall(f"{{{XBRLI}}}measure")]

    divide = element.find(f"{{{XBRLI}}}divide")
    if divide is None:
        unit = {"numerator": measures(element), "denominator": []}
    else:
        unit = {"numerator": measures(divide.find(f"{{{XBRLI}}}unitNumerator")),
                "denominator": measures(divide.find(f"{{{XBRLI}}}unitDenominator"))}
        if not unit["denominator"]:
            raise _Reject(MALFORMED, "divide unit has no denominator measure")
    if not unit["numerator"]:
        raise _Reject(MALFORMED, "unit has no numerator measure")
    return unit


def _definitions(root: etree._Element, name: str, read: Any) -> dict[str, Any]:
    """Read all contexts or units by id; an unreadable one becomes a stored _Reject."""
    definitions: dict[str, Any] = {}
    for element in root.findall(f"{{{XBRLI}}}{name}"):
        identifier = element.get("id") or ""
        if identifier in definitions:
            definitions[identifier] = _Reject(MALFORMED, f"duplicate {name} id {identifier!r}")
            continue
        try:
            definitions[identifier] = read(element)
        except _Reject as problem:
            definitions[identifier] = _Reject(
                problem.category, f"{name} {identifier!r}: {problem.reason}")
    return definitions


def _schema_labels(root: etree._Element, xml_path: Path
                   ) -> tuple[dict[tuple[str, str], tuple[str, str]], dict[str, Any]]:
    """Read concept labels from the extension schema the instance references.

    Only a plain file name beside the instance is followed, never a URL. Standard-taxonomy
    schemas are not downloaded, so their concepts are matched through the taxonomy's
    `prefix_LocalName` element-id convention.
    """
    info: dict[str, Any] = {"schema": None, "sha256": None}
    references = [ref.get(f"{{{XLINK}}}href") or ""
                  for ref in root.findall(f"{{{LINK}}}schemaRef")]
    local = [href for href in references if _SIBLING_FILE.fullmatch(href)]
    if len(local) != 1 or not (xml_path.parent / local[0]).is_file():
        return {}, info | {"note": "no single local schema beside the instance; labels left empty"}
    data = (xml_path.parent / local[0]).read_bytes()
    schema = _parse(data, "Extension schema")
    info |= {"schema": local[0], "sha256": hashlib.sha256(data).hexdigest()}

    target = schema.get("targetNamespace")
    own_names = {e.get("id"): e.get("name") for e in schema.findall(f"{{{XSD}}}element")}
    namespaces = {imported.get("schemaLocation"): imported.get("namespace")
                  for imported in schema.iter(f"{{{XSD}}}import")}
    candidates: dict[tuple[str, str], list[tuple[int, bool, int, str, str]]] = defaultdict(list)
    order = 0
    for link in schema.iter(f"{{{LINK}}}labelLink"):
        hrefs = {loc.get(f"{{{XLINK}}}label"): loc.get(f"{{{XLINK}}}href") or ""
                 for loc in link.findall(f"{{{LINK}}}loc")}
        resources = defaultdict(list)
        for resource in link.findall(f"{{{LINK}}}label"):
            resources[resource.get(f"{{{XLINK}}}label")].append(resource)
        for arc in link.findall(f"{{{LINK}}}labelArc"):
            base, _, fragment = hrefs.get(arc.get(f"{{{XLINK}}}from"), "").partition("#")
            if base in {"", local[0]}:
                namespace, name = target, own_names.get(fragment)
            else:
                namespace, name = namespaces.get(base), fragment.partition("_")[2]
            if not namespace or not name:
                continue
            for resource in resources.get(arc.get(f"{{{XLINK}}}to"), []):
                role = resource.get(f"{{{XLINK}}}role") or _LABEL_ROLES[0]
                text = "".join(resource.itertext()).strip()
                if role in _LABEL_ROLES and text:
                    english = (resource.get(f"{{{XML_NS}}}lang") or "").lower().startswith("en")
                    order += 1
                    candidates[(namespace, name)].append(
                        (_LABEL_ROLES.index(role), not english, order, text, role))
    return {concept: min(found)[3:] for concept, found in candidates.items()}, info


# ------------------------------------------------------------------------ extraction

def _read_fact(element: etree._Element, path: str, order: int, *, scope: dict[str, Any],
               contexts: dict[str, Any], units: dict[str, Any],
               labels: dict[tuple[str, str], tuple[str, str]], label_schema: str | None,
               id_counts: Counter) -> FactInput:
    """Build the record for one fact element, or raise _Reject with the reason it cannot be."""
    name = etree.QName(element)
    element_id = element.get("id") or None
    if id_counts[element_id] > 1:
        raise _Reject(MALFORMED, f"fact id {element_id!r} is used more than once")
    if not name.namespace:
        raise _Reject(MALFORMED, "fact element has no namespace")

    # 1. Context: the period, entity, and dimensions the value applies to.
    context_ref = element.get("contextRef")
    context = contexts.get(context_ref)
    if context is None:
        raise _Reject(MALFORMED, f"contextRef {context_ref!r} has no matching context")
    if isinstance(context, _Reject):
        raise context

    # 2. Unit. XBRL requires unitRef on numeric items and forbids it elsewhere, so its
    #    presence, not the look of the text, decides whether a value is numeric.
    unit_ref = element.get("unitRef")
    unit = None
    if unit_ref is not None:
        unit = units.get(unit_ref)
        if unit is None:
            raise _Reject(MALFORMED, f"unitRef {unit_ref!r} has no matching unit")
        if isinstance(unit, _Reject):
            raise unit

    # 3. Value: nil, exact number, or text. Nil and zero stay different things.
    is_nil = _is_nil(element)
    raw_value: str | None = _text(element)
    numeric_value = None
    if is_nil:
        if raw_value.strip(_XML_SPACE):
            raise _Reject(MALFORMED, "nil fact has content")
        raw_value = None
    elif unit is not None:
        lexical = raw_value.strip(_XML_SPACE)
        if not _NUMBER.fullmatch(lexical):
            raise _Reject(UNSUPPORTED, f"numeric value {lexical[:40]!r} is not a finite decimal")
        numeric_value = Decimal(lexical)  # exact: never passes through float

    reported = {}
    for attribute, rule in (("decimals", _DECIMALS), ("precision", _PRECISION)):
        value = element.get(attribute)
        if value is not None:
            value = value.strip(_XML_SPACE)
            if not rule.fullmatch(value):
                raise _Reject(MALFORMED, f"invalid {attribute} attribute {value!r}")
        reported[attribute] = value

    # 4. Source reference: where the value came from, plus context detail with no column.
    label = labels.get((name.namespace, name.localname))
    reference: dict[str, Any] = {"element_id": element_id} if element_id else {}
    reference |= {
        "xpath": path,
        "document_order": order,
        "qname": f"{element.prefix}:{name.localname}" if element.prefix else name.localname,
        "context_ref": context_ref,
    }
    if unit_ref is not None:
        reference["unit_ref"] = unit_ref
    reference |= {
        "entity": dict(context["entity"]),
        "dimension_containers": list(context["dimension_containers"]),
        "value_source": "xsi:nil" if is_nil else "element_text",
    }
    if element.get(f"{{{XML_NS}}}lang"):
        reference["lang"] = element.get(f"{{{XML_NS}}}lang")
    if label:
        reference["label_source"] = {"schema": label_schema, "role": label[1]}

    return FactInput(
        **scope,
        occurrence_key=element_id or f"xpath:{path}",
        concept_namespace=name.namespace,
        concept_name=name.localname,
        label=label[0] if label else None,
        numeric_value=numeric_value,
        raw_value=raw_value,
        is_nil=is_nil,
        reported_decimals=reported["decimals"],
        reported_precision=reported["precision"],
        unit={key: list(value) for key, value in unit.items()} if unit else None,
        period_kind=context["period_kind"],
        period_start=context["period_start"],
        period_end=context["period_end"],
        instant_date=context["instant_date"],
        dimensions=[dict(dimension) for dimension in context["dimensions"]],
        source_reference=reference,
    )


def extract_facts(
    xml_path: Path | str,
    *,
    filing_id: UUID,
    source_document_id: UUID,
    extraction_version: str = DEFAULT_VERSION,
    use_schema_labels: bool = True,
) -> FactExtraction:
    """Turn every fact occurrence in an XBRL instance into a FactInput.

    Each occurrence ends up either in `facts` or, with a reason, in `failures`; none is
    dropped silently. Call `.require_complete()` on the result to insist on zero failures.
    """
    if not extraction_version.strip():
        raise FactExtractionError("extraction_version cannot be empty")
    xml_path = Path(xml_path)
    data = xml_path.read_bytes()
    root = _parse(data, "XBRL instance")
    if root.tag != f"{{{XBRLI}}}xbrl":
        raise FactExtractionError("The root element is not an XBRL instance (xbrli:xbrl).")

    contexts = _definitions(root, "context", _context)
    units = _definitions(root, "unit", _unit)
    labels, label_info = _schema_labels(root, xml_path) if use_schema_labels else (
        {}, {"schema": None, "sha256": None, "note": "label lookup disabled"})

    # A fact is any element carrying contextRef; its id becomes the occurrence key.
    occurrences = [(element, path) for element, path in _walk(root, "/*")
                   if element.get("contextRef") is not None]
    id_counts = Counter(element.get("id") for element, _ in occurrences if element.get("id"))
    scope = {"filing_id": filing_id, "source_document_id": source_document_id,
             "extraction_version": extraction_version}

    facts: list[FactInput] = []
    failures: list[FactFailure] = []
    for order, (element, path) in enumerate(occurrences, 1):
        try:
            facts.append(_read_fact(
                element, path, order, scope=scope, contexts=contexts, units=units,
                labels=labels, label_schema=label_info["schema"], id_counts=id_counts))
        except _Reject as problem:
            failures.append(FactFailure(
                xpath=path, concept=str(element.tag), element_id=element.get("id") or None,
                category=problem.category, reason=problem.reason))

    footnotes = root.findall(f"{{{LINK}}}footnoteLink")
    inventory = {
        "source": {"file": xml_path.name, "sha256": hashlib.sha256(data).hexdigest(),
                   "size_bytes": len(data)},
        "extraction_version": extraction_version,
        "fact_occurrences": len(occurrences),
        "extracted": len(facts),
        "failed": len(failures),
        "failures_by_category": dict(Counter(f.category for f in failures)),
        "contexts": len(contexts),
        "units": len(units),
        "with_unit_ref": sum(1 for e, _ in occurrences if e.get("unitRef") is not None),
        "without_unit_ref": sum(1 for e, _ in occurrences if e.get("unitRef") is None),
        "extracted_numeric": sum(1 for f in facts if f.numeric_value is not None),
        "extracted_nonnumeric": sum(1 for f in facts if f.unit is None),
        "extracted_nil": sum(1 for f in facts if f.is_nil),
        "extracted_zero": sum(1 for f in facts if f.numeric_value == 0),
        "by_period_kind": dict(Counter(f.period_kind for f in facts)),
        "by_namespace": dict(Counter(f.concept_namespace for f in facts)),
        "by_unit_ref": dict(Counter(f.source_reference.get("unit_ref", "(none)") for f in facts)),
        "with_dimensions": sum(1 for f in facts if f.dimensions),
        "dimension_kinds": dict(Counter(d["kind"] for f in facts for d in f.dimensions)),
        "distinct_concepts": len({(f.concept_namespace, f.concept_name) for f in facts}),
        "occurrence_keys_from_xpath": sum(1 for f in facts if f.occurrence_key.startswith("xpath:")),
        "labels": label_info | {"facts_with_label": sum(1 for f in facts if f.label),
                                "facts_without_label": sum(1 for f in facts if not f.label)},
        # Present in the source but outside this version; listed so nothing is hidden.
        "not_extracted": {
            "footnotes": sum(len(link.findall(f"{{{LINK}}}footnote")) for link in footnotes),
            "footnote_arcs": sum(len(link.findall(f"{{{LINK}}}footnoteArc")) for link in footnotes),
            "other_top_level_elements": dict(Counter(
                child.tag for child in root
                if isinstance(child.tag, str) and child.tag not in _STRUCTURAL
                and child.get("contextRef") is None)),
        },
    }
    return FactExtraction(facts=facts, failures=failures, inventory=inventory)


# --------------------------------------------------------------------------- loading

def store_facts(conn: psycopg.Connection, facts: Sequence[FactInput]) -> StoredFacts:
    """Store facts in the caller's transaction; return their UUIDs with explicit scope.

    Identical facts stored before keep their UUIDs. Changed content under the same
    version raises DataConflict: choose a new extraction_version instead.
    """
    if not facts:
        raise FactExtractionError("There are no facts to store.")
    scopes = {(f.filing_id, f.source_document_id, f.extraction_version) for f in facts}
    if len(scopes) != 1:
        raise FactExtractionError("All facts must share one filing, source document, and version.")
    if len({f.occurrence_key for f in facts}) != len(facts):
        raise FactExtractionError("Occurrence keys must be unique within one extraction.")
    filing_id, source_document_id, extraction_version = next(iter(scopes))
    return StoredFacts(
        filing_id=filing_id, source_document_id=source_document_id,
        extraction_version=extraction_version,
        fact_ids={fact.occurrence_key: store_fact(conn, fact) for fact in facts})


# ------------------------------------------------------- inline HTML correspondence

def _inline_number(element: etree._Element) -> Decimal | None:
    """Return the value an ix:nonFraction displays, or None for an unhandled format."""
    try:
        namespace, local = _resolve(element.get("format"), element)
    except _Reject:
        return None
    if not (namespace or "").startswith(IXT_PREFIX) or local != "num-dot-decimal":
        return None
    shown = re.sub(r"[,\s\xa0]", "", "".join(element.itertext()))
    if not re.fullmatch(r"[0-9]+(\.[0-9]*)?|\.[0-9]+", shown):
        return None
    # Shift the exponent directly so scaling stays exact at any size.
    _, digits, exponent = Decimal(shown).as_tuple()
    negative = 1 if element.get("sign") == "-" else 0
    return Decimal((negative, digits, exponent + int(element.get("scale") or 0)))


def check_inline_correspondence(html_path: Path | str, facts: Sequence[FactInput]) -> dict[str, Any]:
    """Compare extracted facts with the inline XBRL tags in the filing's HTML.

    Facts are matched by id only. For each match the concept, contextRef, unitRef, and
    nil flag must agree, and a displayed number (after scale and sign) must equal the
    XML value. Text values are not compared, because HTML text is transformed.
    """
    data = Path(html_path).read_bytes()
    root = _parse(data, "Inline XBRL HTML")
    inline: dict[str | None, list[etree._Element]] = defaultdict(list)
    for element in root.iter(f"{{{IX}}}nonFraction", f"{{{IX}}}nonNumeric"):
        inline[element.get("id")].append(element)

    mismatches: list[dict[str, str]] = []
    matched = values_compared = values_not_compared = 0
    for fact in facts:
        element_id = fact.source_reference.get("element_id")
        found = inline.get(element_id, []) if element_id else []
        if len(found) != 1:
            mismatches.append({"occurrence_key": fact.occurrence_key,
                               "problem": f"{len(found)} inline elements share this id"})
            continue
        element = found[0]
        problems = []
        try:
            if _resolve(element.get("name"), element) != (fact.concept_namespace, fact.concept_name):
                problems.append("concept")
            if _is_nil(element) != fact.is_nil:
                problems.append("nil")
        except _Reject as problem:
            problems.append(problem.reason)
        if element.get("contextRef") != fact.source_reference.get("context_ref"):
            problems.append("contextRef")
        if element.get("unitRef") != fact.source_reference.get("unit_ref"):
            problems.append("unitRef")
        if (element.tag == f"{{{IX}}}nonFraction") != (fact.unit is not None):
            problems.append("numeric kind")
        if fact.numeric_value is not None:
            shown = _inline_number(element)
            if shown is None:
                values_not_compared += 1
            elif shown != fact.numeric_value:
                problems.append(f"value: HTML shows {shown:f}, XML has {fact.numeric_value:f}")
            else:
                values_compared += 1
        if problems:
            mismatches.append({"occurrence_key": fact.occurrence_key, "problem": "; ".join(problems)})
        else:
            matched += 1

    fact_ids = {fact.source_reference.get("element_id") for fact in facts}
    return {
        "html": {"file": Path(html_path).name, "sha256": hashlib.sha256(data).hexdigest()},
        "facts": len(facts),
        "inline_elements": sum(len(found) for found in inline.values()),
        "matched": matched,
        "numeric_values_equal": values_compared,
        "numeric_values_not_compared": values_not_compared,
        "mismatches": mismatches,
        "inline_ids_without_fact": sorted(str(key) for key in inline if key not in fact_ids),
    }


# ----------------------------------------------------------------- developer command

def _manifest_path(manifest: dict[str, Any], role: str) -> str:
    found = [record["local_path"] for record in manifest["files"] if record.get("role") == role]
    if len(found) != 1:
        raise FactExtractionError(f"The manifest must list exactly one {role} file.")
    return found[0]


def _json_line(fact: FactInput) -> str:
    return json.dumps(asdict(fact), default=str, ensure_ascii=False, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sec_pipeline.fact_extraction",
        description="Extract XBRL facts from the manifest's XML instance. Without --load "
                    "this is a dry run: it parses and reports, and opens no database connection.")
    parser.add_argument("--manifest", type=Path, default=Path("source_manifest.json"),
                        help="Source manifest (default: source_manifest.json).")
    parser.add_argument("--data-root", type=Path, default=Path("."),
                        help="Folder the manifest's local paths start from (default: current folder).")
    parser.add_argument("--version", default=DEFAULT_VERSION, help=f"Extraction version (default: {DEFAULT_VERSION}).")
    parser.add_argument("--check-html", action="store_true",
                        help="Also compare every fact with the inline tags in the filing HTML.")
    parser.add_argument("--output", type=Path,
                        help="Write one JSON fact per line to a new file, such as data/processed/facts-v1.jsonl.")
    parser.add_argument("--load", action="store_true",
                        help="Store the facts in the database selected by --env-file, in one transaction.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"),
                        help="Connection settings for --load (default: .env, the local database).")
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        xml_local_path = _manifest_path(manifest, XML_ROLE)
        xml_path = args.data_root / xml_local_path
        expected_hash = next(r["sha256"] for r in manifest["files"] if r["local_path"] == xml_local_path)

        # Placeholder IDs make a dry run possible without a database; they are never stored.
        extraction = extract_facts(xml_path, filing_id=UUID(int=0), source_document_id=UUID(int=0),
                                   extraction_version=args.version)
        if extraction.inventory["source"]["sha256"] != expected_hash:
            raise FactExtractionError("The XML file does not match the manifest's SHA-256.")
        report: dict[str, Any] = {"inventory": extraction.inventory,
                                  "failures": [asdict(failure) for failure in extraction.failures]}
        if args.check_html:
            html_path = args.data_root / _manifest_path(manifest, HTML_ROLE)
            report["inline_correspondence"] = check_inline_correspondence(html_path, extraction.facts)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8", newline="\n") as handle:
                for fact in extraction.facts:
                    handle.write(_json_line(fact) + "\n")
            report["output"] = {"file": str(args.output), "facts": len(extraction.facts),
                                "note": "dry-run records carry placeholder filing and source IDs"}
        if args.load:
            extraction.require_complete()
            with connect(args.env_file) as conn:
                with conn.transaction():
                    seeded = seed_manifest(conn, args.manifest, args.data_root)
                    real = extract_facts(
                        xml_path, filing_id=seeded["filing_id"],
                        source_document_id=seeded["document_ids"][xml_local_path],
                        extraction_version=args.version).require_complete()
                    stored = store_facts(conn, real.facts)
                    in_database = conn.execute(
                        "SELECT count(*) FROM sec.fact_provenance "
                        "WHERE source_document_id = %s AND extraction_version = %s",
                        (stored.source_document_id, stored.extraction_version)).fetchone()[0]
                    if in_database != len(real.facts):
                        raise FactExtractionError(
                            f"Database holds {in_database} facts for this version; expected {len(real.facts)}.")
                report["loaded"] = {"database": conn.info.dbname, "filing_id": str(stored.filing_id),
                                    "source_document_id": str(stored.source_document_id),
                                    "extraction_version": stored.extraction_version,
                                    "facts_in_database": in_database}
        print(json.dumps(report, indent=2, default=str))
        problems = extraction.failures or report.get("inline_correspondence", {}).get("mismatches")
        return 1 if problems else 0
    except psycopg.Error as exc:
        # Connection errors and server details can contain credentials or input values.
        print(f"Database operation failed ({type(exc).__name__}, SQLSTATE {exc.sqlstate or 'unavailable'}). "
              "Nothing was committed.", file=sys.stderr)
    except (DatabaseError, FactExtractionError, OSError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
    except (KeyError, StopIteration, json.JSONDecodeError):
        print("Stopped: the manifest is not valid JSON or lacks a required field.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
