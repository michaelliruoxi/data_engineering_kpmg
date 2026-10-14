"""Literal financial markers and explicit hidden-nil display bindings.

No amount, concept, or nil value is inferred for an unbound marker. Evidence is
rebuilt from unchanged source bytes for validation, independently of retained
classification and inventory claims. Ordinary V2 extraction is not modified.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace
import hashlib
import re

from lxml import html

from .database import FinancialTableInput
from .report_cleaning import _hidden, _normalize_fragments, _omitted_inline_container, _render
from .source_locators import inline_source_xpath, source_xpath


POLICY = "source-marker-v1"
MARKERS = frozenset({"-", "–", "—"})
INLINE_NAMESPACES = frozenset({"http://www.xbrl.org/2013/inlineXBRL", "http://www.xbrl.org/2008/inlineXBRL"})
XSI = "http://www.w3.org/2001/XMLSchema-instance"


class SourceCellError(ValueError):
    """A source cell lacks unambiguous evidence for the proposed representation."""


def _namespace(node, prefix):
    attribute = "xmlns:" + prefix if prefix else "xmlns"
    for element in (node, *node.iterancestors()):
        value = element.get(attribute)
        if value is not None:
            return value
    return None


def _inline(node, *names):
    if not isinstance(node.tag, str):
        return False
    prefix, separator, name = node.tag.partition(":")
    if not separator:
        prefix, name = None, prefix
    return name.lower() in names and _namespace(node, prefix) in INLINE_NAMESPACES


def _nil(node):
    values = []
    for key, value in node.attrib.items():
        prefix, separator, local = key.partition(":")
        if separator and local.lower() == "nil" and _namespace(node, prefix) == XSI:
            values.append(value)
    if len(values) > 1 or (values and values[0] not in {"true", "false", "1", "0"}):
        raise SourceCellError("Ambiguous or invalid nil attribute.")
    return bool(values and values[0] in {"true", "1"})


def _display(node):
    return " ".join("".join(node.itertext()).split())


def _visible_marker(node):
    if any(_hidden(ancestor) or _omitted_inline_container(ancestor)
           for ancestor in (node, *node.iterancestors())):
        return False
    return _normalize_fragments(_render(node))[0] == _display(node)


def _style_binding(style):
    """Parse declaration boundaries without guessing through CSS escapes/errors."""
    declarations, value, quote, depth = [], [], None, 0
    index = 0
    while index < len(style):
        character = style[index]
        if quote:
            value.append(character)
            if character == "\\":
                index += 1
                if index >= len(style):
                    raise SourceCellError("Incomplete CSS escape.")
                value.append(style[index])
            elif character == quote:
                quote = None
        elif style[index:index + 2] == "/*":
            stop = style.find("*/", index + 2)
            if stop < 0:
                raise SourceCellError("Incomplete CSS comment.")
            value.append(" ")
            index = stop + 1
        elif character in {"'", '"'}:
            quote = character
            value.append(character)
        elif character == "(":
            depth += 1
            value.append(character)
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise SourceCellError("Unbalanced CSS value.")
            value.append(character)
        elif character == ";" and depth == 0:
            declarations.append("".join(value))
            value = []
        else:
            value.append(character)
        index += 1
    if quote or depth:
        raise SourceCellError("Incomplete CSS declaration.")
    declarations.append("".join(value))
    targets = []
    for declaration in declarations:
        if not declaration.strip():
            continue
        name, separator, value = declaration.partition(":")
        name = name.strip()
        if not separator or not re.fullmatch(r"-?[-_A-Za-z][-_A-Za-z0-9]*", name):
            raise SourceCellError("Unsupported CSS property syntax.")
        if name.lower() == "-sec-ix-hidden":
            target = re.sub(r"\s*!\s*important\s*$", "", value, flags=re.I).strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", target):
                raise SourceCellError("Unsupported hidden-fact target syntax.")
            targets.append(target)
    if len(targets) > 1:
        raise SourceCellError("Duplicate hidden-fact declarations.")
    return targets[0] if targets else None


def fact_reference(fact):
    period = ({"instant": fact.instant_date.isoformat()} if fact.period_kind == "instant"
              else {"startDate": fact.period_start.isoformat(), "endDate": fact.period_end.isoformat()}
              if fact.period_kind == "duration" else {"forever": ""})
    return {"concept": "{" + fact.concept_namespace + "}" + fact.concept_name,
            "context_ref": fact.source_reference.get("context_ref"),
            "unit_ref": fact.source_reference.get("unit_ref"), "unit": fact.unit,
            "raw_value": "" if fact.is_nil else fact.raw_value,
            "is_nil": fact.is_nil, "period": period, "dimensions": fact.dimensions}


class SourceBindings:
    """A fresh binding inventory for one original primary HTML document."""

    def __init__(self, source, facts):
        self.sha256 = hashlib.sha256(source).hexdigest()
        self.tree = html.document_fromstring(
            source, parser=html.HTMLParser(encoding="utf-8", no_network=True)).getroottree()
        self.ids = defaultdict(list)
        self.targets = defaultdict(list)
        self.bindings, self.style_errors = {}, {}
        self.facts = {}
        for fact in facts:
            identifier = fact.source_reference.get("element_id")
            if not identifier or identifier in self.facts:
                raise SourceCellError("Facts require unique original element IDs.")
            self.facts[identifier] = fact
        self.unsupported_rendering = []
        self.external_scripts = []
        for node in self.tree.iter():
            if not isinstance(node.tag, str):
                continue
            if node.get("id"):
                self.ids[node.get("id")].append(node)
            tag = node.tag.lower()
            if tag == "script" and node.get("src") and not _display(node):
                self.external_scripts.append({"source_xpath": source_xpath(self.tree, node), "src": node.get("src")})
            elif tag in {"style", "script"} or (tag == "link" and "stylesheet" in node.get("rel", "").lower().split()):
                self.unsupported_rendering.append(source_xpath(self.tree, node))
            if node.get("style"):
                try:
                    target = _style_binding(node.get("style"))
                except SourceCellError as exc:
                    self.style_errors[node] = str(exc)
                    continue
                if target:
                    self.bindings[node] = target
                    self.targets[target].append(node)

    def cell(self, descriptor):
        nodes = self.tree.xpath(descriptor["source_xpath"])
        if len(nodes) != 1 or nodes[0].tag.lower() not in {"td", "th"}:
            raise SourceCellError("Cell locator must select one original anchor cell.")
        node = nodes[0]
        if _display(node) != descriptor["display_text"]:
            raise SourceCellError("Cell display text differs from source.")
        return node

    def representation(self, descriptor, scope):
        """Return direct, source_only, or a uniquely bound hidden-nil reference."""
        node = self.cell(descriptor)
        members = list(node.iter())
        ancestors = list(node.iterancestors())
        if any(_inline(member, "nonfraction", "fraction") for member in [*members, *ancestors]):
            return "direct", None
        if any(isinstance(member.tag, str) and member.tag.lower().split(":")[-1] in {"nonfraction", "fraction"}
               for member in ancestors):
            raise SourceCellError("A foreign numeric-looking ancestor prevents an absence claim.")
        if descriptor["display_text"] not in MARKERS:
            raise SourceCellError("Only an exact financial marker literal is supported.")
        if descriptor["column"] <= 0 or descriptor.get("column_period") is None:
            raise SourceCellError("Marker requires a financial column and established period.")
        if not _visible_marker(node):
            raise SourceCellError("Financial marker is not visible under supported local HTML rules.")
        if self.unsupported_rendering:
            raise SourceCellError("Stylesheet or active-content rendering is outside the marker policy.")
        for member in [*members, *ancestors]:
            if member in self.style_errors:
                raise SourceCellError("Cell overlaps an unsupported style declaration.")
        if any(member in self.bindings for member in ancestors):
            raise SourceCellError("An ancestor display binding cannot be assigned to one cell.")
        if any(_inline(member, "nonnumeric", "continuation") for member in members):
            raise SourceCellError("A nonnumeric inline binding is not an unbound financial marker.")
        for member in members:
            if not isinstance(member.tag, str):
                continue
            if member.tag.lower().split(":")[-1] in {"nonfraction", "fraction", "nonnumeric", "hidden"}:
                raise SourceCellError("Unsupported or foreign inline-looking content.")
        displays = [member for member in members if member in self.bindings]
        if not displays:
            return "source_only", {
                "verification_version": POLICY, "literal": descriptor["display_text"],
                "source_xpath": descriptor["source_xpath"], "html_sha256": self.sha256,
                "binding_scope": "archived_dom_numeric_and_sec_hidden_bindings",
                "runtime_scope": "scripts_not_executed", "external_scripts": list(self.external_scripts),
                "direct_numeric_ids": [], "hidden_binding_targets": [],
            }
        if len(displays) != 1:
            raise SourceCellError("Several display bindings occupy one financial marker cell.")
        display = displays[0]
        target = self.bindings[display]
        if self.style_errors:
            raise SourceCellError("Unparsed document styles prevent a unique hidden display-binding claim.")
        if len(self.targets[target]) != 1 or len(self.ids[target]) != 1:
            raise SourceCellError("Hidden target and display binding must be unique in the source document.")
        hidden = self.ids[target][0]
        containers = [ancestor for ancestor in hidden.iterancestors() if _inline(ancestor, "hidden")]
        fact = self.facts.get(target)
        if not _inline(hidden, "nonfraction") or len(containers) != 1 or fact is None:
            raise SourceCellError("Display target must identify an extracted numeric fact in ix:hidden.")
        if not _nil(hidden) or not fact.is_nil or fact.numeric_value is not None or fact.raw_value is not None:
            raise SourceCellError("Only explicit hidden nil facts are supported.")
        if _display(hidden) or _display(display) != descriptor["display_text"] or not _visible_marker(display):
            raise SourceCellError("Hidden nil content or visible marker differs from its binding.")
        prefix, separator, name = hidden.get("name", "").partition(":")
        namespace = _namespace(hidden, prefix if separator else None)
        if not separator:
            name = prefix
        if ((namespace, name) != (fact.concept_namespace, fact.concept_name)
                or hidden.get("contextref") != fact.source_reference.get("context_ref")
                or hidden.get("unitref") != fact.source_reference.get("unit_ref")
                or fact_reference(fact)["period"] != descriptor["column_period"]
                or str(fact.filing_id) != scope["filing_id"]
                or str(fact.source_document_id) != scope["xml_document_id"]
                or fact.extraction_version != scope["facts_version"]):
            raise SourceCellError("Hidden nil binding differs from its exact source fact and column scope.")
        return "hidden_nil", {
            **fact_reference(fact), "xml_id": target, "scope": dict(scope),
            "method": "hidden_inline_id", "inline_xpath": inline_source_xpath(self.tree, hidden),
            "binding_xpath": source_xpath(self.tree, display), "binding_property": "-sec-ix-hidden",
            "binding_target_id": target, "binding_style": display.get("style"),
            "hidden_container_xpath": source_xpath(self.tree, containers[0]),
            "binding_scope": "archived_dom", "runtime_scope": "scripts_not_executed",
            "external_scripts": list(self.external_scripts),
        }


def source_cell_counts(tables):
    counts = {"financial_cells": 0, "linked_financial_cells": 0, "source_only_marker_cells": 0,
              "hidden_nil_cells": 0, "unresolved_cells": 0}
    for table in tables:
        for row in table.structure["rows"]:
            for cell in row:
                status = cell["classification"]
                if status in {"resolved", "source_only_marker", "unresolved"}:
                    counts["financial_cells"] += 1
                if status == "resolved":
                    counts["linked_financial_cells"] += 1
                if status == "source_only_marker":
                    counts["source_only_marker_cells"] += 1
                if status == "unresolved":
                    counts["unresolved_cells"] += 1
                if any(ref["method"] == "hidden_inline_id" for ref in cell["fact_references"]):
                    counts["hidden_nil_cells"] += 1
    return counts


def transform_tables(tables, source, facts):
    """Create V3 records without mutating any V2 table or source node."""
    bindings = SourceBindings(source, facts)
    result = []
    for table in tables:
        if table.extraction_version != "tables-v2" or table.structure.get("schema_version") != 1:
            raise SourceCellError("The V3 transform requires original schema-v1 tables-v2 records.")
        structure = deepcopy(table.structure)
        structure.update(schema_version=2, source_value_policy=POLICY)
        for row in structure["rows"]:
            for cell in row:
                if (cell["classification"] != "unresolved"
                        or cell.get("unresolved_reason") != "untagged_financial_cell"
                        or cell["display_text"] not in MARKERS):
                    continue
                try:
                    kind, evidence = bindings.representation(cell, structure["fact_scope"])
                except SourceCellError as exc:
                    cell["unresolved_reason"] = "source_marker: " + str(exc)
                    continue
                if kind == "source_only":
                    cell.update(classification="source_only_marker", unresolved_reason=None,
                                fact_references=[], source_only_evidence=evidence)
                elif kind == "hidden_nil":
                    cell.update(classification="resolved", unresolved_reason=None, fact_references=[evidence])
        structure["inventory"] = dict(Counter(cell["classification"] for row in structure["rows"] for cell in row))
        result.append(replace(table, extraction_version="tables-v3", structure=structure))
    return result


def validate_representations(tables, source, facts):
    """Rebuild marker bindings from source; reject omitted or invented evidence."""
    bindings = SourceBindings(source, facts)
    for table in tables:
        if (not isinstance(table, FinancialTableInput) or table.extraction_version != "tables-v3"
                or table.structure.get("schema_version") != 2 or table.structure.get("source_value_policy") != POLICY):
            raise SourceCellError("Source-aware representations require the explicit V3 contract.")
        for row in table.structure["rows"]:
            for cell in row:
                source_only = cell["classification"] == "source_only_marker"
                hidden_refs = [ref for ref in cell["fact_references"] if ref["method"] == "hidden_inline_id"]
                candidate = cell["column"] > 0 and cell["display_text"] in MARKERS and cell.get("column_period") is not None
                if not (source_only or hidden_refs or candidate):
                    if "source_only_evidence" in cell:
                        raise SourceCellError("Only source-only markers may retain absence evidence.")
                    continue
                try:
                    kind, evidence = bindings.representation(cell, table.structure["fact_scope"])
                except SourceCellError:
                    if cell["classification"] == "unresolved":
                        continue
                    raise
                if kind == "source_only":
                    if (not source_only or cell["fact_references"] or cell.get("unresolved_reason") is not None
                            or cell.get("source_only_evidence") != evidence
                            or any(key in cell for key in ("numeric_value", "raw_value", "is_nil"))):
                        raise SourceCellError("Unbound financial marker representation or absence evidence differs from source.")
                elif kind == "hidden_nil":
                    if (cell["classification"] != "resolved" or cell["fact_references"] != [evidence]
                            or cell.get("unresolved_reason") is not None or "source_only_evidence" in cell):
                        raise SourceCellError("Hidden nil reference differs from its explicit source binding.")
                elif source_only or hidden_refs:
                    raise SourceCellError("Direct inline facts cannot be reclassified as unbound markers or hidden links.")
                elif cell["classification"] not in {"resolved", "resolved_label", "unresolved"}:
                    raise SourceCellError("A tagged financial marker cannot be treated as formatting.")
    return source_cell_counts(tables)
