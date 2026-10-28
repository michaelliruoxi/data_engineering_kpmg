"""Map existing tables to original-DOM sections, without assigning any IDs.

The parser/storage identities are inputs. A mapping is evidence, not approval
of a processing set. Persistence is opt-in through the reviewed SQL proposal.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import re
from uuid import UUID

from lxml import etree, html
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .database import DataConflict, DatabaseError
from .source_locators import executable_xpath


VERSION = "table-section-dom-v1"


def canonical(value):
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _nodes(tree, path):
    if not isinstance(path, str) or not path.startswith("/"):
        return []
    try:
        return [node for node in tree.xpath(executable_xpath(path)) if isinstance(node, etree._Element)]
    except etree.XPathError:
        return []


def _excluded(node):
    for ancestor in (node, *node.iterancestors()):
        if not isinstance(ancestor.tag, str):
            continue
        style = re.sub(r"\s+", "", ancestor.get("style", "").lower())
        tokens = set(re.split(r"\s+", ancestor.get("class", "").lower()))
        if (ancestor.tag in {"nav", "header", "footer"} or ancestor.get("role") in {"navigation", "banner"}
                or ancestor.get("hidden") is not None or ancestor.get("aria-hidden") == "true"
                or "display:none" in style or "visibility:hidden" in style
                or tokens & {"toc", "table-of-contents", "page-header", "running-header", "page-footer"}):
            return "navigation_header_or_hidden"
    # Empty named anchors are real headings. TOC hyperlinks point elsewhere.
    links = node.xpath(".//a[starts-with(@href,'#')]")
    if links and all(not text.strip() for text in node.xpath("./text()")):
        return "toc_hyperlink"
    return None


def map_table_section(table, report, sections, source, *, accession, processing_set=None,
                      document_set=None, source_sha256=None):
    """Return a deterministic mapping or an explicit unresolved/error status.

    ``sections`` are descriptors from exactly ``report``; database rows retain
    their real ``id``. Offline descriptors retain keys and leave UUIDs null.
    Original HTML hash and source/report/filing scope must agree before mapping.
    """
    structure = table.get("structure", {})
    locator = structure.get("source_xpath")
    digest = hashlib.sha256(source).hexdigest()
    result = {"accession": accession, "processing_set": processing_set,
              "source/document_set": document_set, "table_id": table.get("id"),
              "table_key": table.get("table_key"), "table_xpath": locator,
              "section_uuid": None, "section_key": None, "section_title": None,
              "section_locator": None, "report_id": report.get("id"),
              "filing_id": table.get("filing_id"), "source_document_id": table.get("source_document_id"),
              "table_version": table.get("extraction_version"), "report_version": report.get("extraction_version"),
              "mapping_status": "unresolved_section", "evidence": {"mapping_version": VERSION, "html_sha256": digest}}

    def failure(status, **evidence):
        return canonical(result | {"mapping_status": status, "evidence": result["evidence"] | evidence})

    if not structure.get("html_sha256") or structure["html_sha256"] != digest or (source_sha256 and source_sha256 != digest):
        return failure("source_hash_mismatch")
    for field in ("filing_id", "source_document_id"):
        if canonical(table.get(field)) != canonical(report.get(field)):
            return failure("scope_mismatch", field=field)
    for section in sections:
        if ((section.get("filing_id") is not None and canonical(section["filing_id"]) != canonical(table.get("filing_id")))
                or (section.get("report_id") is not None and canonical(section["report_id"]) != canonical(report.get("id")))):
            return failure("scope_mismatch", field="section_report_or_filing")
        if section.get("id") is not None:
            try:
                UUID(str(section["id"]))
            except (ValueError, TypeError):
                return failure("invalid_section_id")
            if not report.get("id"):
                return failure("unverified_section_uuid", reason="A section UUID requires its actual report UUID")
    tree = html.document_fromstring(source, parser=html.HTMLParser(encoding="utf-8", no_network=True)).getroottree()
    targets = _nodes(tree, locator)
    if len(targets) != 1 or targets[0].tag != "table":
        return failure("unresolved_table", matches=len(targets))
    nodes = list(tree.getroot().iter())
    order = {node: index for index, node in enumerate(nodes)}
    target = targets[0]
    start, end = order[target], max(order[node] for node in target.iter())
    boundaries, containers, rejected, missing = [], [], [], []
    for section in sections:
        key = section.get("section_key", section.get("section_id"))
        loc = section.get("source_locator") or {}
        if not isinstance(loc, dict):
            missing.append(key)
            continue
        path = loc.get("container_xpath", loc.get("xpath"))
        found = _nodes(tree, path)
        if len(found) != 1:
            missing.append(key)
            continue
        node = found[0]
        excluded = _excluded(node)
        if excluded:
            rejected.append({"section_key": key, "reason": excluded, "xpath": path})
            continue
        if "container_xpath" in loc:
            if node == target or node in list(target.iterancestors()):
                containers.append(section)
        else:
            boundaries.append((order[node], section))
    boundaries.sort(key=lambda item: item[0])
    duplicate_positions = [n for n, count in Counter(p for p, _ in boundaries).items() if count > 1]
    duplicate_keys = [key for key, count in Counter(s.get("section_key", s.get("section_id")) for _, s in boundaries).items() if count > 1]
    duplicate_titles = [title for title, count in Counter(" ".join(str(s.get("original_title", s.get("title", ""))).upper().split())
                        for _, s in boundaries).items() if title and count > 1]
    candidates, crossing = [], False
    for index, (position, section) in enumerate(boundaries):
        limit = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(nodes)
        if start < position <= end:
            crossing = True
        if position <= start and end < limit:
            candidates.append((section, {"section_dom_start": position, "next_section_dom_start": limit}))
    candidates += [(section, {"container_xpath": section["source_locator"]["container_xpath"]}) for section in containers]
    evidence = {"table_dom_start": start, "table_dom_end": end, "rejected_boundaries": rejected,
                "missing_boundaries": missing, "duplicate_positions": duplicate_positions, "duplicate_keys": duplicate_keys,
                **({"duplicate_titles": duplicate_titles} if duplicate_titles else {}),
                "candidate_count": len(candidates), "coordinates": "original DOM traversal; not cleaned text offsets"}
    if crossing:
        return failure("crosses_boundary", **evidence)
    if missing or duplicate_positions or duplicate_keys or duplicate_titles or len(candidates) > 1:
        return failure("ambiguous", **evidence)
    if not candidates:
        return failure("unresolved_section", **evidence)
    section, interval = candidates[0]
    result.update(section_uuid=section.get("id"), section_key=section.get("section_key", section.get("section_id")),
                  section_title=section.get("original_title", section.get("title")),
                  section_locator=deepcopy(section.get("source_locator")), mapping_status="mapped")
    result["evidence"].update(evidence | interval | {"section_validation_status": section.get("validation_status"),
            "uuid_status": "database_row" if section.get("id") else "not_read_offline_key_only"})
    return canonical(result)


def store_table_section_mapping(conn, mapping, source):
    """Recheck receiving-DB identities and store through the optional proposal.

    Requires a caller-owned active transaction; never creates schema, commits,
    changes sealed artifacts, or fabricates a missing section UUID.
    """
    from .table_relations import validate_selection
    if conn.autocommit or conn.info.transaction_status.name != "INTRANS":
        raise DatabaseError("An active caller-owned transaction is required.")
    if mapping.get("mapping_status") != "mapped" or not all(mapping.get(key) for key in ("table_id", "report_id", "section_uuid")):
        raise DatabaseError("Persistence requires verified receiving-database table, report and section IDs.")
    with conn.cursor(row_factory=dict_row) as cursor:
        table = cursor.execute("SELECT * FROM sec.financial_tables WHERE id=%s", (mapping["table_id"],)).fetchone()
        report = cursor.execute("SELECT * FROM sec.reports WHERE id=%s", (mapping["report_id"],)).fetchone()
        if table is None or report is None:
            raise DatabaseError("Missing receiving-database table or report.")
        source_row = cursor.execute("SELECT sha256 FROM sec.source_documents WHERE id=%s", (table["source_document_id"],)).fetchone()
        filing = cursor.execute("SELECT accession_number FROM sec.filings WHERE id=%s", (table["filing_id"],)).fetchone()
        sections = cursor.execute("SELECT * FROM sec.report_sections WHERE report_id=%s ORDER BY section_order", (report["id"],)).fetchall()
        if mapping.get("processing_set"):
            selected = validate_selection(conn, table, report, mapping["processing_set"])
            if canonical(selected.get("document_set_id")) != mapping.get("source/document_set"):
                raise DatabaseError("Document set does not match the selected processing set.")
        elif mapping.get("source/document_set") is not None:
            raise DatabaseError("A document-set association requires its explicit processing selection.")
        rebuilt = map_table_section(table, report, sections, source, accession=filing["accession_number"],
                    processing_set=mapping.get("processing_set"), document_set=mapping.get("source/document_set"),
                    source_sha256=source_row["sha256"])
        if rebuilt != canonical(mapping):
            raise DataConflict("Mapping differs from current source or receiving-database identities.")
        if not cursor.execute("SELECT to_regclass('sec.financial_table_sections') AS relation").fetchone()["relation"]:
            raise DatabaseError("The optional table-section persistence proposal is not installed.")
        values = (mapping["table_id"], mapping["report_id"], mapping["section_uuid"], mapping["filing_id"],
                  mapping["source_document_id"], mapping["evidence"]["html_sha256"], VERSION, Jsonb(mapping))
        with conn.transaction():
            cursor.execute("""INSERT INTO sec.financial_table_sections
                (table_id,report_id,section_id,filing_id,source_document_id,source_sha256,mapping_version,evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(table_id,report_id) DO NOTHING""", values)
            saved = cursor.execute("SELECT evidence FROM sec.financial_table_sections WHERE table_id=%s AND report_id=%s",
                                   values[:2]).fetchone()
            if saved["evidence"] != canonical(mapping):
                raise DataConflict("Existing immutable table-section mapping differs.")
    return mapping["section_uuid"]
