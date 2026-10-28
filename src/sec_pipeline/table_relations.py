"""Read-only, occurrence-level verification of existing financial table data."""
from collections import Counter
from dataclasses import asdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
import tempfile
from uuid import UUID

from lxml import html
from psycopg.rows import dict_row

from .database import DatabaseError, FactInput, FinancialTableInput
from .source_locators import executable_xpath
from .table_cells import relational_cells
from .table_sections import canonical, map_table_section
from .tables import layout_grid, local, same_row_repeat_reference, text, verify_inline


def _rows(conn, statement, parameters=()):
    with conn.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(statement, parameters).fetchall()


def validate_selection(conn, table, report, selection):
    """Check the actual DB selection, never select or promote a set."""
    if not isinstance(selection, dict) or set(selection) != {"contract", "id"}:
        raise DatabaseError("Selection requires explicit contract and receiving-database id.")
    if selection["contract"] == "legacy":
        rows = _rows(conn, "SELECT * FROM sec.filing_processing_sets WHERE id=%s AND filing_id=%s",
                     (selection["id"], table["filing_id"]))
        if not rows:
            raise DatabaseError("Missing or cross-filing processing set.")
        chosen = rows[0]
        if (chosen["table_source_document_id"] != table["source_document_id"]
                or chosen["table_extraction_version"] != table["extraction_version"]
                or str(chosen["report_id"]) != str(report["id"])):
            raise DatabaseError("Table/report source or version is outside this processing set.")
        return chosen
    if selection["contract"] == "source-v4":
        rows = _rows(conn, """SELECT p.*,d.xml_document_id FROM sec.source_processing_sets p
            JOIN sec.source_document_sets d ON d.id=p.document_set_id
            JOIN sec.source_processing_tables t ON t.processing_set_id=p.id
            JOIN sec.source_processing_reports r ON r.processing_set_id=p.id
            WHERE p.id=%s AND p.filing_id=%s AND t.table_id=%s AND r.report_id=%s""",
                     (selection["id"], table["filing_id"], table["id"], report["id"]))
        if (not rows or rows[0]["versions"]["tables"] != table["extraction_version"]
                or rows[0]["versions"]["reports"] != report["extraction_version"]
                or table["source_document_id"] != report["source_document_id"]):
            raise DatabaseError("Table/report is not a member of this source processing set.")
        if not _rows(conn, "SELECT 1 FROM sec.source_document_set_members WHERE document_set_id=%s AND source_document_id=%s",
                     (rows[0]["document_set_id"], table["source_document_id"])):
            raise DatabaseError("Table HTML is outside the selected document set.")
        return rows[0]
    raise DatabaseError("Unknown processing-set contract.")


def read_snapshot(conn, accession, *, selection=None):
    """Read all relevant rows, including bad parents an inner join could hide.

    Caller starts a READ ONLY transaction. With no explicit choice, use only an
    actual legacy preferred selection. Otherwise list candidates, not 'latest'.
    """
    if conn.info.transaction_status.name != "INTRANS":
        raise DatabaseError("Start an explicit read-only transaction first.")
    if _rows(conn, "SHOW transaction_read_only")[0]["transaction_read_only"] != "on":
        raise DatabaseError("Table relation snapshot requires a read-only transaction.")
    relations = {row["table_name"] for row in _rows(conn,
        "SELECT table_name FROM information_schema.tables WHERE table_schema='sec'")}
    filing = _rows(conn, "SELECT * FROM sec.filings WHERE accession_number=%s", (accession,))
    if len(filing) != 1:
        raise DatabaseError("Accession must resolve to exactly one filing.")
    filing_id = filing[0]["id"]
    result = {"accession": accession, "filing": filing[0], "selection": selection,
              "sources": _rows(conn, "SELECT * FROM sec.source_documents WHERE filing_id=%s ORDER BY id", (filing_id,)),
              "schema_relations": sorted(relations)}
    for relation in ("filing_processing_sets", "preferred_processing_sets", "source_processing_sets", "source_document_sets"):
        result[relation] = _rows(conn, f"SELECT * FROM sec.{relation} WHERE filing_id=%s ORDER BY 1", (filing_id,)) if relation in relations else []
    if selection is None and result["preferred_processing_sets"]:
        result["selection"] = {"contract": "legacy", "id": result["preferred_processing_sets"][0]["processing_set_id"]}
    result["tables"] = _rows(conn, "SELECT * FROM sec.financial_tables WHERE filing_id=%s ORDER BY extraction_version,table_key", (filing_id,))
    result["reports"] = _rows(conn, "SELECT * FROM sec.reports WHERE filing_id=%s ORDER BY id", (filing_id,))
    if result["selection"]:
        choice = result["selection"]
        candidate = next((row for row in result["filing_processing_sets"] if str(row["id"]) == str(choice["id"])), None) if choice["contract"] == "legacy" else None
        if choice["contract"] == "legacy":
            if candidate is None:
                raise DatabaseError("Chosen processing set is absent from this filing.")
            result["tables"] = [t for t in result["tables"] if t["source_document_id"] == candidate["table_source_document_id"] and t["extraction_version"] == candidate["table_extraction_version"]]
            result["reports"] = [r for r in result["reports"] if r["id"] == candidate["report_id"]]
        elif choice["contract"] == "source-v4":
            candidate = next((row for row in result["source_processing_sets"] if str(row["id"]) == str(choice["id"])), None)
            if candidate is None:
                raise DatabaseError("Chosen source processing set is absent from this filing.")
            table_ids = {row["table_id"] for row in _rows(conn, "SELECT table_id FROM sec.source_processing_tables WHERE processing_set_id=%s", (choice["id"],))}
            report_ids = {row["report_id"] for row in _rows(conn, "SELECT report_id FROM sec.source_processing_reports WHERE processing_set_id=%s", (choice["id"],))}
            result["tables"] = [t for t in result["tables"] if t["id"] in table_ids]
            result["reports"] = [r for r in result["reports"] if r["id"] in report_ids]
        else:
            raise DatabaseError("Unknown processing-set contract.")
        result["selected_processing_set"] = candidate
    table_ids = [t["id"] for t in result["tables"]]
    report_ids = [r["id"] for r in result["reports"]]
    for relation in ("table_cells", "cell_fact_links", "financial_table_facts", "cell_source_observations", "cell_source_fact_bindings"):
        cell_ids = [c["id"] for c in result.get("table_cells", [])]
        predicate = "table_id=ANY(%s) OR cell_id=ANY(%s)" if relation in {"cell_fact_links", "cell_source_observations", "cell_source_fact_bindings"} else "table_id=ANY(%s)"
        parameters = (table_ids, cell_ids) if "OR" in predicate else (table_ids,)
        result[relation] = _rows(conn, f"SELECT * FROM sec.{relation} WHERE {predicate} ORDER BY 1", parameters) if relation in relations and table_ids else []
    # Bring linked facts into scope even if they incorrectly claim another filing.
    linked_ids = [row["fact_id"] for name in ("cell_fact_links", "financial_table_facts", "cell_source_fact_bindings") for row in result[name]]
    result["facts"] = _rows(conn, "SELECT * FROM sec.financial_facts WHERE filing_id=%s OR id=ANY(%s) ORDER BY id", (filing_id, linked_ids))
    result["sections"] = _rows(conn, "SELECT * FROM sec.report_sections WHERE report_id=ANY(%s) ORDER BY section_order", (report_ids,)) if "report_sections" in relations and report_ids else []
    result["chunks"] = _rows(conn, "SELECT * FROM sec.chunks WHERE report_id=ANY(%s) ORDER BY chunk_index", (report_ids,)) if report_ids else []
    for relation in ("fact_source_occurrences", "source_document_set_members", "source_quality_issues"):
        result[relation] = _rows(conn, f"SELECT * FROM sec.{relation} WHERE filing_id=%s ORDER BY 1", (filing_id,)) if relation in relations else []
    for table in result["tables"]:
        if result["selection"]:
            reports = [r for r in result["reports"] if r["source_document_id"] == table["source_document_id"]]
            if len(reports) != 1:
                raise DatabaseError("Selected table lacks a unique report from its original HTML.")
            validate_selection(conn, table, reports[0], result["selection"])
    return canonical(result)


def fact_evidence(fact):
    period = ({"instant": str(fact["instant_date"])} if fact["period_kind"] == "instant" else
              {"startDate": str(fact["period_start"]), "endDate": str(fact["period_end"])} if fact["period_kind"] == "duration" else {"forever": ""})
    source = fact["source_reference"]
    return {"concept": "{" + fact["concept_namespace"] + "}" + fact["concept_name"],
            "raw_value": "" if fact["is_nil"] else fact["raw_value"], "is_nil": fact["is_nil"],
            "context_ref": source.get("context_ref"), "unit_ref": source.get("unit_ref"),
            "unit": fact["unit"], "period": period, "dimensions": fact["dimensions"]}


def _fact_input(row):
    fields = {key: row[key] for key in FactInput.__dataclass_fields__}
    for key in ("filing_id", "source_document_id"):
        fields[key] = UUID(fields[key])
    for key in ("period_start", "period_end", "instant_date"):
        fields[key] = date.fromisoformat(fields[key]) if fields[key] else None
    fields["numeric_value"] = Decimal(fields["numeric_value"]) if fields["numeric_value"] is not None else None
    return FactInput(**fields)


def _same_original_fact(original, fact):
    return (original is not None and fact_evidence(original) == fact_evidence(fact)
            and (original["numeric_value"] is None) == (fact["numeric_value"] is None)
            and (original["numeric_value"] is None or Decimal(original["numeric_value"]) == Decimal(fact["numeric_value"]))
            and original["reported_decimals"] == fact["reported_decimals"]
            and original["reported_precision"] == fact["reported_precision"])


def _occurrence_matches_fact_evidence(occurrence_evidence, fact):
    """The existing source contract permits NULL or '' for a NIL raw value.

    Do not relax non-nil values, nil flags, mapped values or any other evidence.
    FactSourceOccurrenceInput still independently validates the full occurrence.
    """
    wanted = fact_evidence(fact)
    observed = dict(occurrence_evidence)
    if fact["is_nil"] is True and observed.get("is_nil") is True and observed.get("raw_value") in (None, ""):
        observed["raw_value"] = ""
    return all(observed.get(key) == value for key, value in wanted.items())


def _valid_occurrence(occurrence, ref, fact, table, document_set, tree):
    """Reuse the v4 source contract for accepted as well as review references."""
    from .source_set_storage import FactSourceOccurrenceInput
    try:
        FactSourceOccurrenceInput(**{key: UUID(occurrence[key]) for key in
            ("filing_id", "document_set_id", "fact_id", "html_document_id")},
            inline_id=occurrence["inline_id"], source_xpath=occurrence["source_xpath"],
            evidence=occurrence["evidence"], fragments=tuple(occurrence.get("fragments", [])))
    except (ValueError, KeyError, TypeError):
        return False
    valid = (occurrence["filing_id"] == table["filing_id"] == fact["filing_id"]
             and occurrence["fact_id"] == fact["id"] and occurrence["inline_id"] == ref["xml_id"]
             and occurrence["html_document_id"] == table["source_document_id"]
             and (not document_set or occurrence["document_set_id"] == document_set)
             and occurrence["evidence"].get("html_sha256") == table["structure"]["html_sha256"]
             and occurrence["evidence"].get("xml_sha256") == table["structure"]["xml_sha256"]
             and _occurrence_matches_fact_evidence(occurrence["evidence"], fact))
    if tree is not None:
        from .table_sections import _nodes
        original = _nodes(tree, occurrence["source_xpath"])
        inline = _nodes(tree, ref["inline_xpath"])
        valid = valid and len(original) == len(inline) == 1 and original[0] == inline[0]
    return valid


def audit_snapshot(snapshot, sources):
    """Validate stored cells/links against layout, scoped facts and raw sources.

    ``sources`` maps actual source_document_id strings to unchanged bytes.
    Missing original sources or legacy relational rows remain unverified.
    """
    from .v4_table_cells import v4_relational_cells
    snapshot = canonical(snapshot)
    errors, unverified, summaries, mappings, trace = [], [], [], [], []
    facts = {f["id"]: f for f in snapshot["facts"]}
    original_facts = {}
    receipts = {s["id"]: s for s in snapshot["sources"]}
    choice = snapshot.get("selected_processing_set") or {}
    document_set = choice.get("document_set_id")
    if not snapshot["tables"]:
        unverified.append({"reason": "no_selected_tables"})
    if snapshot.get("selection") and (not choice or choice.get("id") != snapshot["selection"]["id"]
            or choice.get("filing_id") != snapshot["filing"]["id"]):
        errors.append({"reason": "processing_selection_identity_mismatch"})
    cell_index = {c["id"]: c for c in snapshot["table_cells"]}
    for relation in ("cell_fact_links", "cell_source_observations", "cell_source_fact_bindings"):
        for link in snapshot.get(relation, []):
            parent = cell_index.get(link["cell_id"])
            if not parent or (link["table_id"], link["filing_id"]) != (parent["table_id"], parent["filing_id"]):
                errors.append({"reason": "wrong_or_missing_cell_parent", "relation": relation, "cell_id": link["cell_id"]})

    def error(table, reason, **details):
        errors.append({"table_id": table["id"], "table_key": table["table_key"], "reason": reason, **details})

    for table in snapshot["tables"]:
        structure, tid = table["structure"], table["id"]
        if table["extraction_version"] == "tables-v4" and not document_set:
            unverified.append({"table_id": tid, "reason": "source_document_set_unselected"})
        cells = [c for c in snapshot["table_cells"] if c["table_id"] == tid]
        links = [link for link in snapshot["cell_fact_links"] if link["table_id"] == tid]
        members = [m for m in snapshot["financial_table_facts"] if m["table_id"] == tid]
        record = FinancialTableInput(**{k: table[k] for k in FinancialTableInput.__dataclass_fields__})
        try:
            parsed = v4_relational_cells(tid, record) if table["extraction_version"] == "tables-v4" else relational_cells(tid, record)
        except ValueError as failure:
            if table.get("relational_cells_version") is None:
                unverified.append({"table_id": tid, "reason": "legacy_structure_not_relational", "detail": str(failure)})
                continue
            error(table, "invalid_structure", detail=str(failure))
            continue
        actual = {cell["cell_key"]: cell for cell in cells}
        expected = {item[0].cell_key: canonical(asdict(item[0])) for item in parsed}
        if len(actual) != len(cells) or set(actual) != set(expected):
            error(table, "cell_inventory_mismatch", expected=len(expected), actual=len(cells))
        for key in sorted(set(actual) & set(expected)):
            if any(canonical(actual[key].get(field)) != value for field, value in expected[key].items()):
                error(table, "cell_parent_or_content_mismatch", cell_key=key)
        html_id = table["source_document_id"]
        source = sources.get(html_id)
        tree = None
        if source is None:
            unverified.append({"table_id": tid, "reason": "original_HTML_missing"})
        elif (hashlib.sha256(source).hexdigest() != structure.get("html_sha256")
              or receipts.get(html_id, {}).get("sha256") != structure.get("html_sha256")):
            error(table, "HTML_hash_mismatch")
        else:
            tree = html.document_fromstring(source, parser=html.HTMLParser(encoding="utf-8", no_network=True)).getroottree()
            nodes = tree.xpath(executable_xpath(structure["source_xpath"]))
            if len(nodes) != 1 or nodes[0].tag != "table":
                error(table, "table_locator_not_unique")
                tree = None
            else:
                physical, grid = layout_grid(nodes[0], tree)
                fields = ("cell_id", "row", "column", "rowspan", "colspan", "display_text", "source_xpath")
                if ([{k: c[k] for k in fields} for row in physical for c in row]
                        != [{k: c[k] for k in fields} for row in structure["rows"] for c in row]
                        or grid != structure.get("grid")):
                    error(table, "original_grid_mismatch")
        scope = structure.get("fact_scope", {})
        xml_id, version = scope.get("xml_document_id"), scope.get("facts_version")
        xml_source = sources.get(xml_id)
        if xml_source is None:
            unverified.append({"table_id": tid, "reason": "original_XML_missing"})
        elif (hashlib.sha256(xml_source).hexdigest() != structure.get("xml_sha256")
              or receipts.get(xml_id, {}).get("sha256") != structure.get("xml_sha256")):
            error(table, "XML_hash_mismatch")
        elif xml_id not in original_facts:
            from .fact_extraction import extract_facts
            # The existing parser is path-based. Only public source bytes are
            # copied temporarily; no original archive or database is modified.
            with tempfile.TemporaryDirectory(prefix="table-link-xml-") as directory:
                temporary_xml = Path(directory) / "instance.xml"
                temporary_xml.write_bytes(xml_source)
                extracted = extract_facts(temporary_xml, filing_id=UUID(table["filing_id"]),
                                          source_document_id=UUID(xml_id), extraction_version=version).require_complete()
            original_facts[xml_id] = {f.occurrence_key: canonical(asdict(f)) for f in extracted.facts}
        if scope.get("filing_id") != table["filing_id"]:
            error(table, "declared_fact_scope_wrong_filing")
        if choice:
            selected_version = choice.get("versions", {}).get("tables", choice.get("table_extraction_version"))
            if table["extraction_version"] != selected_version or (not document_set and table["source_document_id"] != choice.get("table_source_document_id")):
                error(table, "table_outside_processing_set")
            wanted_xml = choice.get("fact_source_document_id")
            wanted_version = choice.get("fact_extraction_version")
            if document_set:
                selected_document = next((d for d in snapshot.get("source_document_sets", []) if d["id"] == document_set), {})
                wanted_xml, wanted_version = selected_document.get("xml_document_id"), choice.get("versions", {}).get("facts")
                source_members = [m for m in snapshot.get("source_document_set_members", []) if m["document_set_id"] == document_set]
                if html_id not in {m["source_document_id"] for m in source_members}:
                    error(table, "table_source_not_in_document_set")
            if (xml_id, version) != (wanted_xml, wanted_version):
                error(table, "fact_scope_outside_processing_set")
        accepted_ids = set()
        expected_pairs = set()
        refs_by_key = {item[0].cell_key: item[1] for item in parsed}
        descriptors = {c["cell_id"]: c for row in structure["rows"] for c in row}
        from .source_cells_v3 import SourceBindings
        needs_marker_check = any(c.get("source_only_evidence") or any(r.get("method") == "hidden_inline_id" for r in c["fact_references"])
                                 for row in structure["rows"] for c in row)
        bindings_from_source = SourceBindings(source, [_fact_input(f) for f in facts.values()
            if f["filing_id"] == table["filing_id"] and f["source_document_id"] == xml_id and f["extraction_version"] == version]) if tree is not None and needs_marker_check else None
        if bindings_from_source:
            for row in structure["rows"]:
                for descriptor in row:
                    if not (descriptor.get("source_only_evidence") or any(r.get("method") == "hidden_inline_id" for r in descriptor["fact_references"])):
                        continue
                    try:
                        kind, evidence = bindings_from_source.representation(descriptor, scope)
                        if (kind == "source_only" and evidence == descriptor.get("source_only_evidence")) or (kind == "hidden_nil" and descriptor["fact_references"] == [evidence]):
                            continue
                        raise ValueError("Retained marker/hidden binding differs from the original DOM")
                    except ValueError as failure:
                        error(table, "original_marker_evidence_mismatch", cell_key=descriptor["cell_id"], detail=str(failure))
        for key, refs in refs_by_key.items():
            cell = actual.get(key)
            for ordinal, ref in enumerate(refs):
                candidates = [f for f in facts.values() if f["filing_id"] == table["filing_id"]
                              and f["source_document_id"] == xml_id and f["extraction_version"] == version
                              and f["occurrence_key"] == ref["xml_id"]]
                if len(candidates) != 1:
                    error(table, "missing_or_ambiguous_scoped_fact", cell_key=key, occurrence_key=ref["xml_id"])
                    continue
                fact = candidates[0]
                original = original_facts.get(xml_id, {}).get(fact["occurrence_key"])
                if xml_id in original_facts and not _same_original_fact(original, fact):
                    error(table, "stored_fact_differs_from_original_XML", cell_key=key, fact_id=fact["id"])
                if table["extraction_version"] == "tables-v4" and document_set:
                    occurrences = [o for o in snapshot.get("fact_source_occurrences", []) if o["fact_id"] == fact["id"]
                                   and o["document_set_id"] == document_set]
                    if len(occurrences) != 1 or not _valid_occurrence(occurrences[0], ref, fact, table, document_set, tree):
                        error(table, "accepted_source_occurrence_mismatch", cell_key=key)
                accepted_ids.add(fact["id"])
                if cell:
                    expected_pairs.add((cell["id"], fact["id"], ordinal))
                if canonical(ref.get("scope")) != canonical(scope) or any(ref.get(k) != value for k, value in canonical(fact_evidence(fact)).items()):
                    error(table, "fact_evidence_or_scope_mismatch", cell_key=key, fact_id=fact["id"])
                if tree is not None and ref["method"] != "hidden_inline_id":
                    inline = tree.xpath(executable_xpath(ref["inline_xpath"]))
                    try:
                        if len(inline) != 1 or inline[0].get("id") != ref["xml_id"]:
                            raise ValueError("inline id/locator mismatch")
                        namespaces = {k[6:]: v for k, v in tree.getroot().attrib.items() if k.startswith("xmlns:")}
                        verify_inline(inline[0], ref, namespaces)
                        for field, attribute, default in (("scale", "scale", "0"), ("inline_sign", "sign", None), ("format", "format", None)):
                            if field in ref and ref[field] != inline[0].get(attribute, default):
                                raise ValueError("Stored scale/sign/format differs from the original inline tag")
                        target = tree.xpath(executable_xpath(expected[key]["source_locator"]["xpath"]))
                        if len(target) != 1:
                            raise ValueError("cell locator mismatch")
                        if ref["method"] == "inline_id" and target[0] not in list(inline[0].iterancestors()):
                            raise ValueError("fact is not inside this cell")
                        if ref["method"] == "same_row_semantic_repeat":
                            origin_rows = list(inline[0].iterancestors("tr"))
                            target_rows = list(target[0].iterancestors("tr"))
                            if not origin_rows or not target_rows or origin_rows[0] != target_rows[0] or ref.get("repeated_from_inline_xpath") != ref["inline_xpath"]:
                                raise ValueError("semantic repeat origin differs")
                            if descriptors[key]["column"] == 0 or any(local(node) == "nonfraction" for node in target[0].iter()):
                                raise ValueError("semantic repeat target contains an inline fact")
                            row_refs = [candidate for row in structure["rows"] for descriptor in row
                                        if descriptor["row"] == descriptors[key]["row"]
                                        for candidate in descriptor["fact_references"] if candidate["method"] == "inline_id"]
                            repeated = same_row_repeat_reference(
                                descriptors[key] | {"display_text": text(target[0])}, row_refs)
                            if repeated != ref:
                                raise ValueError("semantic repeat value, period or unique row evidence differs")
                    except (ValueError, InvalidOperation, TypeError, KeyError) as failure:
                        error(table, "original_inline_evidence_mismatch", cell_key=key, detail=str(failure))
                for link in [l for l in links if cell and l["cell_id"] == cell["id"] and l["reference_order"] == ordinal]:
                    if link.get("evidence") != ref or link.get("link_method") != ref["method"]:
                        error(table, "stored_link_evidence_mismatch", cell_key=key)
                    if link["fact_id"] == fact["id"] and len(trace) < 3:
                        trace.append({"table_id": tid, "table_key": table["table_key"], "table_xpath": structure["source_xpath"],
                                      "cell_id": cell["id"], "cell_key": key, "cell_xpath": expected[key]["source_locator"]["xpath"],
                                      "fact_id": fact["id"], "fact": fact_evidence(fact), "occurrence_key": fact["occurrence_key"]})
        observed_pairs = {(l["cell_id"], l["fact_id"], l["reference_order"]) for l in links}
        if observed_pairs != expected_pairs or len(links) != len(expected_pairs):
            error(table, "cell_fact_links_mismatch", missing=sorted(expected_pairs-observed_pairs), extra=sorted(observed_pairs-expected_pairs))
        if {m["fact_id"] for m in members} != accepted_ids or len(members) != len(accepted_ids):
            error(table, "table_memberships_mismatch")
        for link in [*links, *members]:
            fact = facts.get(link["fact_id"])
            if not fact or link["filing_id"] != table["filing_id"] or fact["filing_id"] != table["filing_id"] or fact["source_document_id"] != xml_id or fact["extraction_version"] != version:
                error(table, "link_crosses_filing_source_or_version", fact_id=link["fact_id"])
        for item in parsed:
            expected_cell = item[0]
            cell = actual.get(expected_cell.cell_key)
            if not cell:
                continue
            if expected_cell.resolution_status == "source_only":
                observations = [o for o in snapshot.get("cell_source_observations", []) if o["cell_id"] == cell["id"]]
                expected_observation = {"filing_id": table["filing_id"], "table_id": tid, "source_document_id": html_id,
                        "source_sha256": structure["html_sha256"], "verification_version": "source-marker-v1", "evidence": descriptors[cell["cell_key"]]["source_only_evidence"]}
                if len(observations) != 1 or any(observations[0].get(k) != v for k, v in expected_observation.items()):
                    error(table, "source_only_observation_mismatch", cell_key=cell["cell_key"])
            if expected_cell.resolution_status == "review_required":
                descriptor = item[2]
                bindings = [b for b in snapshot.get("cell_source_fact_bindings", []) if b["cell_id"] == cell["id"]]
                ref = descriptor["source_fact_references"][0]
                occurrences = {o["id"]: o for o in snapshot.get("fact_source_occurrences", [])}
                valid = len(bindings) == 1
                if valid:
                    binding, = bindings
                    occurrence = occurrences.get(binding["occurrence_id"], {})
                    fact = facts.get(binding["fact_id"], {})
                    valid = (binding["filing_id"] == table["filing_id"] and binding["table_id"] == tid
                             and binding["reference_order"] == 0 and binding["evidence"] == ref
                             and occurrence.get("filing_id") == table["filing_id"]
                             and occurrence.get("fact_id") == binding["fact_id"] and occurrence.get("inline_id") == ref["xml_id"]
                             and occurrence.get("html_document_id") == html_id and occurrence.get("document_set_id") == binding["document_set_id"]
                             and (not document_set or binding["document_set_id"] == document_set)
                             and fact.get("source_document_id") == xml_id and fact.get("extraction_version") == version
                             and fact.get("filing_id") == table["filing_id"] and canonical(fact_evidence(fact)) == {k: ref[k] for k in fact_evidence(fact)})
                    valid = valid and (occurrence.get("evidence", {}).get("html_sha256") == structure["html_sha256"]
                              and occurrence.get("evidence", {}).get("xml_sha256") == structure["xml_sha256"]
                              and _occurrence_matches_fact_evidence(occurrence.get("evidence", {}), fact))
                    valid = valid and _valid_occurrence(occurrence, ref, fact, table, document_set, tree)
                    if xml_id in original_facts:
                        valid = valid and _same_original_fact(original_facts[xml_id].get(fact.get("occurrence_key")), fact)
                    issues = [q for q in snapshot.get("source_quality_issues", []) if q["binding_id"] == binding["id"]]
                    valid &= {q["issue_key"] for q in issues} == set(descriptor["quality_issue_keys"])
                    valid &= all(q["filing_id"] == table["filing_id"] and q["document_set_id"] == binding["document_set_id"]
                                 and {k: q[k] for k in wanted} == wanted
                                 for wanted in descriptor["quality_issues"] for q in issues if q["issue_key"] == wanted["issue_key"])
                    if tree is not None:
                        nodes = tree.xpath(executable_xpath(ref["inline_xpath"]))
                        try:
                            if len(nodes) != 1 or nodes[0].get("id") != ref["xml_id"]:
                                raise ValueError("review inline locator mismatch")
                            verify_inline(nodes[0], ref, {k[6:]: v for k, v in tree.getroot().attrib.items() if k.startswith("xmlns:")})
                        except ValueError:
                            valid = False
                if not valid:
                    error(table, "review_occurrence_or_issue_mismatch", cell_key=cell["cell_key"])
        reports = [r for r in snapshot["reports"] if r["source_document_id"] == html_id]
        for report in reports:
            if source is not None:
                mapping = map_table_section(table, report, [s for s in snapshot["sections"] if s["report_id"] == report["id"]], source,
                    accession=snapshot["accession"], processing_set=snapshot.get("selection"), document_set=document_set,
                    source_sha256=receipts.get(html_id, {}).get("sha256"))
                mappings.append(mapping)
                if mapping["mapping_status"] != "mapped":
                    error(table, "section_mapping_" + mapping["mapping_status"])
                if choice and ((not document_set and report["id"] != choice.get("report_id"))
                        or (document_set and report["extraction_version"] != choice["versions"]["reports"])):
                    error(table, "report_outside_processing_set")
        if not reports:
            unverified.append({"table_id": tid, "reason": "matching_report_missing"})
        summaries.append({"table_id": tid, "table_key": table["table_key"], "version": table["extraction_version"],
                          "anchor_cells": len(cells), "classification_counts": dict(Counter(c["resolution_status"] for c in cells)),
                          "cell_references": len(links), "unique_memberships": len(members)})
    for example in trace:
        section = next((m for m in mappings if m["table_id"] == example["table_id"] and m["mapping_status"] == "mapped"), None)
        example.update(table_chunk=None, chunk_status="no_actual_table_chunk_supplied", section=section,
                       database_scope="caller_supplied_snapshot; IDs valid only in that receiving database")
    return canonical({"accession": snapshot["accession"], "selection": snapshot.get("selection"),
        "processing_validation_status": choice.get("validation_status"),
        "independent_review": "not_asserted",
        "selection_status": "explicit_or_preferred" if snapshot.get("selection") else "unselected_candidates_only",
        "status": "failed" if errors else "unverified" if unverified or not snapshot.get("selection") else "passed",
        "errors": errors, "unverified": unverified, "tables": summaries, "section_mappings": mappings,
        "trace_examples": trace, "table_chunks": "not_inferred_from_paragraph_chunks"})
