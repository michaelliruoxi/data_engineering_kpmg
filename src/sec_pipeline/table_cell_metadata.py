"""Read-only RAG metadata from receiving-DB tables, cells and accepted links.

No IDs, chunks, headers, DB rows or persistent relationships are generated.
Section association uses a unique whole-table token alignment to the supplied
immutable report, explicitly separate from original-HTML/DOM validation.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import re
from uuid import UUID


VERSION = "table-cell-metadata-v1"


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Expected an existing canonical UUID string")
    return value


def _section(table, report, sections):
    tokens = list(re.finditer(r"[^\s|]+", report["full_text"]))
    words = [match.group() for match in tokens]
    wanted = re.findall(r"[^\s|]+", table["readable_text"])
    if not wanted:
        raise ValueError("Cannot locate an empty table")
    hits = [i for i, word in enumerate(words)
            if word == wanted[0] and words[i:i + len(wanted)] == wanted]
    if len(hits) != 1:
        raise ValueError("Table text must have exactly one whole-table report match")
    first = hits[0]
    start, end = tokens[first].start(), tokens[first + len(wanted) - 1].end()
    owners = [s for s in sections if s["report_id"] == report["id"]
              and s["filing_id"] == table["filing_id"]
              and s["offset_start"] <= start < end <= s["offset_end"]]
    if len(owners) != 1:
        raise ValueError("Table must belong to one exact report section")
    owner = owners[0]
    return {"section_id": _uuid(owner["id"]), "section_key": owner["section_key"],
            "report_id": report["id"], "table_text_start": start, "table_text_end": end,
            "mapping_method": "unique_retained_whole_table_token_alignment",
            "report_text_sha256": hashlib.sha256(report["full_text"].encode()).hexdigest(),
            "original_html_verified": False, "persisted_table_section_mapping": False}


def export_table_metadata(*, cells, sections, tables, report):
    """Validate receiving keys against retained anchors and export table records.

    ``cells`` are sec.statement_cells_all_versions records, with its ordered
    linked_facts; ``tables`` use canonical sec.financial_tables field names.
    Numeric values select no links: only existing accepted fact UUIDs are used.
    has_accepted_numeric_fact is a cell-level existence flag, not processing-set
    approval. The consumer must still pin and validate its publication scope.
    """
    _uuid(report["id"])
    _uuid(report["filing_id"])
    _uuid(report["source_document_id"])
    table_index = {}
    for table in tables:
        key = _uuid(table["id"])
        if key in table_index:
            raise ValueError("Duplicate table identity")
        if any(table[field] != report[field] for field in ("filing_id", "source_document_id")):
            raise ValueError("Table/report filing or original HTML scope mismatch")
        table_index[key] = table
    grouped = {key: [] for key in table_index}
    seen_ids, seen_keys = set(), set()
    for cell in cells:
        key, identifier = cell["table_id"], _uuid(cell["cell_id"])
        if key not in table_index:
            raise ValueError("Cell belongs to an undeclared table")
        if identifier in seen_ids or (key, cell["cell_key"]) in seen_keys:
            raise ValueError("Duplicate cell UUID or table/cell key")
        seen_ids.add(identifier)
        seen_keys.add((key, cell["cell_key"]))
        grouped[key].append(cell)
    output = []
    for key, table in sorted(table_index.items()):
        group = sorted(grouped[key], key=lambda c: (c["row_index"], c["column_index"]))
        if not group:
            raise ValueError("Declared table has no relational cells")
        anchors = {}
        for row in table["structure"]["rows"]:
            for anchor in row:
                if anchor["cell_id"] in anchors:
                    raise ValueError("Duplicate retained cell key")
                anchors[anchor["cell_id"]] = anchor
        if set(anchors) != {c["cell_key"] for c in group}:
            raise ValueError("Relational cell keys do not cover the retained table")
        exported, occupied = [], set()
        for cell in group:
            anchor = anchors[cell["cell_key"]]
            if any(cell[field] != table[field] for field in ("filing_id", "source_document_id", "extraction_version")):
                raise ValueError("Cross-source, cross-version or cross-filing cell")
            if cell["source_sha256"] != table["structure"]["html_sha256"]:
                raise ValueError("Cell/table original HTML hash mismatch")
            for field, source, minimum in (("row_index", "row", 0), ("column_index", "column", 0),
                                            ("row_span", "rowspan", 1), ("column_span", "colspan", 1)):
                if type(cell[field]) is not int or cell[field] < minimum or cell[field] != anchor[source]:
                    raise ValueError("Cell row, column or span differs from retained anchor")
            slots = {(r, c) for r in range(cell["row_index"], cell["row_index"] + cell["row_span"])
                     for c in range(cell["column_index"], cell["column_index"] + cell["column_span"])}
            if occupied & slots:
                raise ValueError("Overlapping relational cell spans")
            occupied.update(slots)
            if (cell["display_text"] != anchor["display_text"]
                    or cell["source_locator"].get("xpath") != anchor["source_xpath"]):
                raise ValueError("Cell text or locator differs from retained anchor")
            facts, refs = cell["linked_facts"], anchor["fact_references"]
            if (cell["fact_count"] != len(facts) or len(facts) != len(refs)
                    or [f["reference_order"] for f in facts] != list(range(len(facts)))):
                raise ValueError("Accepted fact references or reference order differ")
            if ((cell["resolution_status"] == "resolved" and not facts)
                    or (cell["resolution_status"] != "resolved" and facts)):
                raise ValueError("Cell status does not permit these accepted fact links")
            ids = []
            for fact, ref in zip(facts, refs):
                ids.append(_uuid(fact["fact_id"]))
                scope = table["structure"]["fact_scope"]
                if (fact["source_document_id"] != scope["xml_document_id"]
                        or fact["extraction_version"] != scope["facts_version"]
                        or fact["source_reference"].get("element_id") != ref["xml_id"]
                        or fact["evidence"].get("scope", {}).get("filing_id") != table["filing_id"]):
                    raise ValueError("Fact UUID does not follow the retained occurrence/source scope")
                value = fact["numeric_value"]
                if value is not None:
                    try:
                        if not isinstance(value, str) or not Decimal(value).is_finite():
                            raise ValueError("Numeric facts must retain exact decimal strings")
                    except InvalidOperation as exc:
                        raise ValueError("Invalid exact numeric value") from exc
            if len(ids) != len(set(ids)):
                raise ValueError("Repeated fact UUID in one cell")
            fields = ("cell_id", "cell_key", "row_index", "column_index", "row_span", "column_span",
                      "kind", "display_text", "row_heading", "column_headings", "column_period",
                      "source_locator", "resolution_status", "unresolved_reason", "is_total", "linked_facts")
            exported.append(deepcopy({k: cell[k] for k in fields}) | {
                "fact_ids": ids, "has_accepted_numeric_fact": cell["resolution_status"] == "resolved"
                    and cell["kind"] == "data" and any(not f["is_nil"] and f["numeric_value"] is not None for f in facts)})
        accessions = {c["accession_number"] for c in group}
        if len(accessions) != 1:
            raise ValueError("Mixed accession scope")
        section = _section(table, report, sections)
        output.append({"metadata_version": VERSION, "table_id": key, "table_key": table["table_key"],
                       "accession": accessions.pop(), "filing_id": table["filing_id"],
                       "source_document_id": table["source_document_id"],
                       "source_sha256": table["structure"]["html_sha256"],
                       "table_version": table["extraction_version"], "report_id": report["id"],
                       "report_version": report["extraction_version"],
                       "section_id": section["section_id"], "section_key": section["section_key"],
                       "section_mapping_evidence": section,
                       "row_column_basis": "zero_based_physical_anchor_indices_in_sec.table_cells",
                       "presentation_columns": deepcopy(table["structure"].get("presentation_columns", [])),
                       "processing_set": None, "processing_selection_verified": False,
                       "cells": exported})
    return output


def table_fragment_metadata(*, chunk_id, selections, tables):
    """Attach explicit DB cell keys to an actual caller-supplied table fragment.

    selections is {existing_table_uuid: [cell_key, ...]}. It must describe the
    original rows/cells used to create that fragment, not cells found by amount.
    The producer owns chunk identity/text. All requested keys validate together.
    """
    if not isinstance(chunk_id, str) or not chunk_id.strip():
        raise ValueError("Supply the actual producer chunk_id")
    index = {t["table_id"]: t for t in tables}
    if len(index) != len(tables) or not isinstance(selections, dict) or not selections:
        raise ValueError("Unique tables and explicit nonempty selections required")
    refs, selected, scopes = [], [], set()
    for table_id, keys in sorted(selections.items()):
        if table_id not in index:
            raise ValueError("Unknown receiving table_id")
        table = index[table_id]
        if (not isinstance(keys, list) or not keys or any(not isinstance(k, str) for k in keys)
                or len(keys) != len(set(keys))):
            raise ValueError("Supply unique nonempty cell_keys for every selected table")
        cells = {c["cell_key"]: c for c in table["cells"]}
        if set(keys) - set(cells):
            raise ValueError("Unknown cell_key; partial table-fragment links are rejected")
        scopes.add(tuple(table[k] for k in ("accession", "filing_id", "source_document_id", "table_version", "report_id")))
        selected.append({k: table[k] for k in ("table_id", "section_id", "section_key", "report_id")})
        for cell in sorted((cells[k] for k in keys), key=lambda c: (c["row_index"], c["column_index"])):
            refs.append({"table_id": table_id, **deepcopy(cell)})
    if len(scopes) != 1:
        raise ValueError("A table fragment cannot mix source/version/report scopes")
    scope, = scopes
    return {"metadata_version": VERSION, "chunk_id": chunk_id, "table_ids": sorted(selections),
            "accession": scope[0], "filing_id": scope[1], "source_document_id": scope[2],
            "table_version": scope[3], "report_id": scope[4], "sections": selected,
            "cells": refs, "fact_ids": list(dict.fromkeys(f for c in refs for f in c["fact_ids"])),
            "selection_method": "explicit_producer_cell_keys", "processing_selection_verified": False,
            "shared_deployed": False}
