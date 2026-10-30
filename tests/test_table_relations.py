"""Synthetic adversarial relation fixtures; never shared production evidence."""
from copy import deepcopy
import hashlib
from uuid import UUID

import pytest

from sec_pipeline.table_chunk_links import resolve_table_chunks
from sec_pipeline.table_sections import map_table_section


FILING, HTML, REPORT, SECTION, TABLE, SET = [str(UUID(int=n)) for n in range(101, 107)]
ACCESSION = "0000000101-26-000001"
SOURCE = b'''<html><body><nav><p id="toc"><a href="#heading">Item 1. Financial Statements</a></p></nav>
<header><p id="repeat">Item 1. Financial Statements</p></header>
<p id="heading">Item 1. Financial Statements</p><div id="container">
<table id="t1"><tr><td id="a">Revenue</td><td id="b">100</td></tr></table>
<table id="t2"><tr><td>Profit</td><td>20</td></tr></table></div>
<p id="next">Item 2. Management Discussion</p><p id="outside">Other text</p></body></html>'''


def fixture():
    digest = hashlib.sha256(SOURCE).hexdigest()
    table = {"id": TABLE, "filing_id": FILING, "source_document_id": HTML,
             "extraction_version": "tables-fixture-v1", "table_key": "income_statement",
             "accession": ACCESSION, "processing_set": {"contract": "legacy", "id": SET}, "source/document_set": None,
             "structure": {"html_sha256": digest, "source_xpath": "//*[@id='t1']"}}
    report = {"id": REPORT, "filing_id": FILING, "source_document_id": HTML, "extraction_version": "report-fixture-v1"}
    sections = [{"id": SECTION, "filing_id": FILING, "report_id": REPORT, "section_key": "part-i-item-1",
                 "original_title": "Item 1. Financial Statements", "source_locator": {"xpath": "//*[@id='heading']"},
                 "validation_status": "detected"},
                {"filing_id": FILING, "report_id": REPORT, "section_key": "part-i-item-2", "original_title": "Item 2",
                 "source_locator": {"xpath": "//*[@id='next']"}, "validation_status": "detected"}]
    chunk = {"chunk_id": "synthetic-table-chunk-1", "chunk_type": "table", "synthetic": True,
             "table_ids": [TABLE], "filing_id": FILING, "accession": ACCESSION,
             "processing_set": {"contract": "legacy", "id": SET}, "source/document_set": None,
             "source_document_id": HTML, "table_version": "tables-fixture-v1",
             "source_locator": {"xpath": "//*[@id='b']"}}
    return table, report, sections, chunk


def mapped(table=None, report=None, sections=None, source=SOURCE):
    default_table, default_report, default_sections, _ = fixture()
    return map_table_section(table or default_table, report or default_report,
                             default_sections if sections is None else sections, source,
                             accession=ACCESSION, processing_set={"contract": "legacy", "id": SET})


def test_correct_section_uses_real_UUID_and_original_locator():
    result = mapped()
    assert result["mapping_status"] == "mapped"
    assert result["section_uuid"] == SECTION
    assert result["section_key"] == "part-i-item-1"
    assert result["section_locator"] == {"xpath": "//*[@id='heading']"}
    assert result["evidence"]["section_validation_status"] == "detected"


def test_offline_mapping_does_not_manufacture_database_UUIDs():
    table, report, sections, _ = fixture()
    for record in [table, report, *sections]:
        record.pop("id", None)
        record.pop("filing_id", None)
        record.pop("report_id", None)
        record.pop("source_document_id", None)
    result = mapped(table, report, sections)
    assert result["mapping_status"] == "mapped"
    assert result["table_id"] is None and result["section_uuid"] is None


@pytest.mark.parametrize("field", ["filing_id", "source_document_id"])
def test_cross_report_scope_fails(field):
    table, report, sections, _ = fixture()
    report[field] = str(UUID(int=900))
    assert mapped(table, report, sections)["mapping_status"] == "scope_mismatch"


@pytest.mark.parametrize("field", ["filing_id", "report_id"])
def test_wrong_section_parent_fails(field):
    _, _, sections, _ = fixture()
    sections[0][field] = str(UUID(int=900))
    assert mapped(sections=sections)["mapping_status"] == "scope_mismatch"


def test_wrong_section_UUID_and_changed_source_fail():
    _, _, sections, _ = fixture()
    sections[0]["id"] = "part-i-item-1"
    assert mapped(sections=sections)["mapping_status"] == "invalid_section_id"
    assert mapped(source=SOURCE+b" ")["mapping_status"] == "source_hash_mismatch"


def test_toc_and_repeated_page_heading_are_excluded():
    _, _, sections, _ = fixture()
    toc, repeated = deepcopy(sections[0]), deepcopy(sections[0])
    toc["source_locator"] = {"xpath": "//*[@id='toc']"}
    repeated["source_locator"] = {"xpath": "//*[@id='repeat']"}
    result = mapped(sections=[toc, repeated, *sections])
    assert result["mapping_status"] == "mapped"
    assert len(result["evidence"]["rejected_boundaries"]) == 2


def test_duplicate_real_boundary_is_ambiguous():
    _, _, sections, _ = fixture()
    assert mapped(sections=[sections[0], deepcopy(sections[0]), sections[1]])["mapping_status"] == "ambiguous"


def test_unmarked_repeated_title_is_not_assigned_by_last_title_match():
    _, _, sections, _ = fixture()
    repeated = deepcopy(sections[0])
    repeated.update(section_key="unconfirmed-repeat", source_locator={"xpath": "//*[@id='outside']"})
    result = mapped(sections=[*sections, repeated])
    assert result["mapping_status"] == "ambiguous"
    assert result["evidence"]["duplicate_titles"] == ["ITEM 1. FINANCIAL STATEMENTS"]


def test_missing_table_and_section_have_explicit_status():
    table, _, sections, _ = fixture()
    table["structure"]["source_xpath"] = "//*[@id='absent']"
    assert mapped(table=table)["mapping_status"] == "unresolved_table"
    assert mapped(sections=[])["mapping_status"] == "unresolved_section"
    sections[1]["source_locator"] = {"xpath": "//*[@id='absent']"}
    assert mapped(sections=sections)["mapping_status"] == "ambiguous"


def test_section_start_inside_table_is_cross_boundary():
    table, report, sections, _ = fixture()
    sections[1]["source_locator"] = {"xpath": "//*[@id='b']"}
    assert mapped(table, report, sections)["mapping_status"] == "crosses_boundary"


def test_container_mapping_and_overlapping_containers():
    _, _, sections, _ = fixture()
    sections[0]["source_locator"] = {"container_xpath": "//*[@id='container']"}
    assert mapped(sections=[sections[0]])["mapping_status"] == "mapped"
    assert mapped(sections=[sections[0], deepcopy(sections[0])])["mapping_status"] == "ambiguous"


def test_mapping_is_deterministic_and_does_not_change_inputs():
    table, report, sections, _ = fixture()
    original = deepcopy((table, report, sections))
    assert mapped(table, report, sections) == mapped(table, report, sections)
    assert (table, report, sections) == original


def resolve(chunks=None, tables=None, **options):
    table, _, _, chunk = fixture()
    return resolve_table_chunks(chunks or [chunk], tables or [table], {HTML: SOURCE}, allow_synthetic=True, **options)


def test_same_table_multiple_chunks_preserve_table_ID():
    table, _, _, chunk = fixture()
    second = deepcopy(chunk)
    second.update(chunk_id="synthetic-table-chunk-2", source_locator={"xpath": "//*[@id='a']"})
    rows = resolve([chunk, second], [table])
    assert [row["table_id"] for row in rows] == [TABLE, TABLE]
    assert all(row["mapping_status"] == "synthetic_fixture_mapped" and row["synthetic"] for row in rows)
    assert all(row["contract_status"] == "pending_Michael_Ruby_confirmation" for row in rows)


def test_multi_table_chunk_is_explicit_not_arbitrarily_reduced():
    table, _, _, chunk = fixture()
    other = deepcopy(table)
    other.update(id=str(UUID(int=202)), table_key="other_statement")
    other["structure"]["source_xpath"] = "//*[@id='t2']"
    chunk.update(table_ids=[TABLE, other["id"]], source_locator={"xpath": "//*[@id='container']"})
    assert len(resolve([chunk], [table, other])) == 2
    chunk["table_ids"].append(str(UUID(int=999)))
    rows = resolve([chunk], [table, other])
    assert len(rows) == 1 and rows[0]["table_id"] is None


@pytest.mark.parametrize("field,value", [("filing_id", str(UUID(int=999))), ("accession", "wrong-filing"),
    ("processing_set", {"contract": "legacy", "id": str(UUID(int=999))}), ("source/document_set", str(UUID(int=999))),
    ("source_document_id", str(UUID(int=999))), ("table_version", "tables-other")])
def test_cross_scope_chunk_is_rejected(field, value):
    _, _, _, chunk = fixture()
    chunk[field] = value
    assert resolve([chunk])[0]["mapping_status"] == "rejected"
    assert "scope_mismatch" in resolve([chunk])[0]["evidence"]["reason"]


@pytest.mark.parametrize("identifier", ["income_statement", "synthetic-table-chunk-1", str(UUID(int=999))])
def test_wrong_or_missing_table_identity_is_not_replaced_by_table_key(identifier):
    _, _, _, chunk = fixture()
    chunk["table_ids"] = [identifier]
    assert resolve([chunk])[0]["table_id"] is None


def test_missing_scope_and_bad_locator_are_rejected():
    _, _, _, chunk = fixture()
    del chunk["processing_set"]
    assert "missing_explicit_scope" in resolve([chunk])[0]["evidence"]["reason"]
    _, _, _, chunk = fixture()
    chunk["source_locator"] = {"xpath": "//*[@id='outside']"}
    assert resolve([chunk])[0]["evidence"]["reason"] == "chunk_locator_outside_declared_table"


def test_synthetic_opt_in_and_configurable_fields_and_repeat_stability():
    table, _, _, chunk = fixture()
    assert resolve_table_chunks([chunk], [table], {HTML: SOURCE})[0]["mapping_status"] == "rejected"
    external = {"payload": chunk}
    config = {key: "payload."+key for key in chunk}
    rows = resolve([external], input_fields=config, output_fields={"table_id": "financial_table_id"})
    assert rows[0]["financial_table_id"] == TABLE
    assert rows == resolve([external], input_fields=config, output_fields={"table_id": "financial_table_id"})
    with pytest.raises(ValueError, match="collide"):
        resolve(output_fields={"table_id": "chunk_id"})


@pytest.mark.parametrize("marker", [None, "true", "false", 0, 1, [], {}])
@pytest.mark.parametrize("allow_synthetic", [False, True])
def test_malformed_synthetic_marker_is_never_treated_as_real(marker, allow_synthetic):
    table, _, _, chunk = fixture()
    chunk["synthetic"] = marker
    result, = resolve_table_chunks([chunk], [table], {HTML: SOURCE}, allow_synthetic=allow_synthetic)
    assert result["mapping_status"] == "rejected"
    assert result["synthetic"] is None
    assert result["evidence"]["reason"] == "synthetic_must_be_explicit_boolean"


def test_missing_synthetic_marker_rejected_and_explicit_false_is_preserved():
    table, _, _, chunk = fixture()
    del chunk["synthetic"]
    result, = resolve_table_chunks([chunk], [table], {HTML: SOURCE})
    assert result["mapping_status"] == "rejected" and result["synthetic"] is None
    chunk["synthetic"] = False
    result, = resolve_table_chunks([chunk], [table], {HTML: SOURCE})
    assert result["mapping_status"] == "draft_mapped" and result["synthetic"] is False


def relationship_snapshot(table, facts):
    """Synthetic stored-row shape, separate from opt-in real PostgreSQL tests."""
    from dataclasses import asdict
    from sec_pipeline.table_cells import relational_cells
    from sec_pipeline.table_relations import fact_evidence
    from sec_pipeline.table_sections import canonical
    from sec_pipeline.v4_table_cells import v4_relational_cells
    table_id = str(UUID(int=500))
    table_row = canonical(asdict(table)) | {"id": table_id, "relational_cells_version": table.structure["schema_version"]}
    fact_rows = [canonical(asdict(fact)) | {"id": str(UUID(int=600+n))} for n, fact in enumerate(facts)]
    fact_ids = {f["occurrence_key"]: f["id"] for f in fact_rows}
    snapshot = {"accession": ACCESSION, "selection": None, "filing": {"id": str(table.filing_id)},
        "tables": [table_row], "facts": fact_rows, "table_cells": [], "cell_fact_links": [],
        "financial_table_facts": [], "reports": [], "sections": [], "sources": [],
        "cell_source_observations": [], "cell_source_fact_bindings": [], "fact_source_occurrences": [], "source_quality_issues": []}
    snapshot["sources"] = [{"id": str(table.source_document_id), "sha256": table.structure["html_sha256"]},
                           {"id": table.structure["fact_scope"]["xml_document_id"], "sha256": table.structure["xml_sha256"]}]
    parsed = v4_relational_cells(UUID(table_id), table) if table.extraction_version == "tables-v4" else relational_cells(UUID(table_id), table)
    members = set()
    for n, item in enumerate(parsed):
        cell, refs, descriptor = (*item, None) if len(item) == 2 else item
        if descriptor is None:
            descriptor = next(c for row in table.structure["rows"] for c in row if c["cell_id"] == cell.cell_key)
        cell_id = str(UUID(int=700+n))
        snapshot["table_cells"].append(canonical(asdict(cell)) | {"id": cell_id})
        parent = {"cell_id": cell_id, "table_id": table_id, "filing_id": str(table.filing_id)}
        for ordinal, ref in enumerate(refs):
            fact_id = fact_ids[ref["xml_id"]]
            members.add(fact_id)
            snapshot["cell_fact_links"].append(parent | {"fact_id": fact_id, "reference_order": ordinal,
                                                       "link_method": ref["method"], "evidence": ref})
        if cell.resolution_status == "source_only":
            snapshot["cell_source_observations"].append(parent | {"source_document_id": str(table.source_document_id),
                "source_sha256": table.structure["html_sha256"], "verification_version": "source-marker-v1", "evidence": descriptor["source_only_evidence"]})
        if cell.resolution_status == "review_required":
            ref = descriptor["source_fact_references"][0]
            occurrence_id, binding_id, document_set_id = [str(UUID(int=x)) for x in (800, 801, 802)]
            binding = parent | {"id": binding_id, "fact_id": fact_ids[ref["xml_id"]], "occurrence_id": occurrence_id,
                "document_set_id": document_set_id, "reference_order": 0, "evidence": ref}
            snapshot["cell_source_fact_bindings"].append(binding)
            snapshot["fact_source_occurrences"].append({"id": occurrence_id, "filing_id": str(table.filing_id),
                "fact_id": binding["fact_id"], "inline_id": ref["xml_id"], "html_document_id": str(table.source_document_id),
                "source_xpath": ref["inline_xpath"],
                "document_set_id": document_set_id, "evidence": fact_evidence(fact_rows[0]) | {
                    "xml_id": ref["xml_id"], "inline_is_nil": fact_rows[0]["is_nil"], "inline_mapped_value": fact_rows[0]["raw_value"],
                    "html_sha256": table.structure["html_sha256"], "xml_sha256": table.structure["xml_sha256"]}})
            snapshot["source_quality_issues"] = [issue | {"binding_id": binding_id, "document_set_id": document_set_id,
                "filing_id": str(table.filing_id)} for issue in descriptor["quality_issues"]]
    snapshot["financial_table_facts"] = [{"table_id": table_id, "filing_id": str(table.filing_id), "fact_id": fid} for fid in members]
    return canonical(snapshot)


def test_source_only_observation_rechecks_original_evidence_without_inventing_fact():
    from tests.test_source_cells_v3 import fixture as marker_fixture
    from sec_pipeline.source_cells_v3 import transform_tables
    from sec_pipeline.table_relations import audit_snapshot
    original, facts, source = marker_fixture()
    [table] = transform_tables([original], source, facts)
    snapshot = relationship_snapshot(table, facts)
    result = audit_snapshot(snapshot, {str(table.source_document_id): source})
    assert result["errors"] == [] and result["status"] == "unverified"  # No XML/report/selected set.
    assert snapshot["cell_fact_links"] == snapshot["financial_table_facts"] == []
    snapshot["cell_source_observations"][0]["evidence"]["literal"] = "0"
    assert "source_only_observation_mismatch" in {e["reason"] for e in audit_snapshot(snapshot, {str(table.source_document_id): source})["errors"]}


@pytest.mark.parametrize("defect", [None, "occurrence", "document_set", "binding_evidence", "issue", "hash"])
def test_v4_review_binding_is_not_an_accepted_fact_link(defect):
    from tests.test_v4_table_cells import conflict_fixture
    from sec_pipeline.table_relations import audit_snapshot
    table, fact, _ = conflict_fixture()
    snapshot = relationship_snapshot(table, [fact])
    binding = snapshot["cell_source_fact_bindings"][0]
    if defect == "occurrence":
        binding["occurrence_id"] = str(UUID(int=999))
    elif defect == "document_set":
        binding["document_set_id"] = str(UUID(int=999))
    elif defect == "binding_evidence":
        binding["evidence"]["raw_value"] = "999"
    elif defect == "issue":
        snapshot["source_quality_issues"] = []
    elif defect == "hash":
        snapshot["fact_source_occurrences"][0]["evidence"]["html_sha256"] = "f"*64
    result = audit_snapshot(snapshot, {})
    assert snapshot["cell_fact_links"] == snapshot["financial_table_facts"] == []
    assert bool(result["errors"]) == bool(defect)
    if defect:
        assert "review_occurrence_or_issue_mismatch" in {e["reason"] for e in result["errors"]}


def accepted_relationship_snapshot():
    from sec_pipeline.table_relations import fact_evidence
    from tests.test_v4_table_cells import conflict_fixture
    table, fact, _ = conflict_fixture(issue=False)
    snapshot = relationship_snapshot(table, [fact])
    document_set = str(UUID(int=802))
    snapshot["selection"] = {"contract": "source-v4", "id": SET}
    snapshot["selected_processing_set"] = {"id": SET, "filing_id": str(table.filing_id), "document_set_id": document_set,
        "versions": {"facts": "facts-v1", "tables": "tables-v4", "reports": "report-v1"}}
    snapshot["source_document_sets"] = [{"id": document_set, "xml_document_id": str(fact.source_document_id)}]
    snapshot["source_document_set_members"] = [{"document_set_id": document_set, "source_document_id": str(table.source_document_id)}]
    ref = table.structure["rows"][0][1]["fact_references"][0]
    fact_row = snapshot["facts"][0]
    occurrence = {"id": str(UUID(int=800)), "filing_id": str(table.filing_id), "fact_id": fact_row["id"],
        "document_set_id": document_set, "html_document_id": str(table.source_document_id),
        "inline_id": ref["xml_id"], "source_xpath": ref["inline_xpath"],
        "evidence": fact_evidence(fact_row) | {"xml_id": ref["xml_id"], "inline_is_nil": False,
            "inline_mapped_value": fact_row["raw_value"], "html_sha256": table.structure["html_sha256"],
            "xml_sha256": table.structure["xml_sha256"]}}
    snapshot["fact_source_occurrences"] = [occurrence]
    return snapshot


@pytest.mark.parametrize("defect", [None, "missing", "duplicate", "document_set", "filing", "source", "raw_value"])
def test_v4_accepted_links_also_require_exact_selected_source_occurrence(defect):
    from sec_pipeline.table_relations import audit_snapshot
    snapshot = accepted_relationship_snapshot()
    occurrence = snapshot["fact_source_occurrences"][0]
    if defect == "missing":
        snapshot["fact_source_occurrences"] = []
    elif defect == "duplicate":
        snapshot["fact_source_occurrences"].append(deepcopy(occurrence) | {"id": str(UUID(int=999))})
    elif defect in {"document_set", "filing", "source"}:
        occurrence[{"document_set": "document_set_id", "filing": "filing_id", "source": "html_document_id"}[defect]] = str(UUID(int=999))
    elif defect == "raw_value":
        occurrence["evidence"]["inline_mapped_value"] = "999"
    result = audit_snapshot(snapshot, {})
    assert bool(result["errors"]) == bool(defect)
    if defect:
        assert "accepted_source_occurrence_mismatch" in {e["reason"] for e in result["errors"]}


@pytest.mark.parametrize("selected", [False, True])
def test_fact_in_multiple_document_sets_does_not_fail_candidate_audit(selected):
    from sec_pipeline.table_relations import audit_snapshot
    snapshot = accepted_relationship_snapshot()
    occurrence = snapshot["fact_source_occurrences"][0]
    snapshot["fact_source_occurrences"].append(deepcopy(occurrence) | {
        "id": str(UUID(int=901)), "document_set_id": str(UUID(int=902))})
    if not selected:
        snapshot["selection"] = None
        snapshot.pop("selected_processing_set")
    result = audit_snapshot(snapshot, {})
    assert result["errors"] == []
    assert result["status"] == "unverified"  # No original source files/report.
    reasons = {item["reason"] for item in result["unverified"]}
    assert ("source_document_set_unselected" in reasons) is (not selected)


def inline_relationship_fixture(*, scale=None, repeat=False, defect=None, negative=False):
    from dataclasses import replace
    from datetime import date
    from decimal import Decimal
    from lxml import html
    from sec_pipeline.database import FactInput, FinancialTableInput
    from sec_pipeline.table_relations import fact_evidence
    from sec_pipeline.table_sections import canonical
    from sec_pipeline.tables import layout_grid
    from sec_pipeline.source_locators import inline_source_xpath

    fact = FactInput(filing_id=UUID(FILING), source_document_id=UUID(int=900),
        extraction_version="facts-v1", occurrence_key="F1",
        concept_namespace="http://fasb.org/us-gaap/2026", concept_name="CashAndCashEquivalentsAtCarryingValue",
        numeric_value=Decimal(-24 if negative else 24) * Decimal(10) ** int(scale or "0"),
        raw_value=str((-24 if negative else 24) * 10 ** int(scale or "0")), period_kind="instant", instant_date=date(2026, 6, 30),
        unit={"numerator": ["{http://www.xbrl.org/2003/iso4217}USD"], "denominator": []},
        source_reference={"element_id": "F1", "context_ref": "c1", "unit_ref": "usd"})
    facts = [fact]
    if defect in {"ambiguous_occurrences", "ambiguous_semantics"}:
        facts.append(replace(fact, occurrence_key="F2",
            concept_name="RestrictedCashCurrent" if defect == "ambiguous_semantics" else fact.concept_name,
            instant_date=date(2025, 6, 30) if defect == "ambiguous_semantics" else fact.instant_date,
            source_reference={"element_id": "F2", "context_ref": "c2", "unit_ref": "usd"}))
    scale_attribute = "" if scale is None else f' scale="{scale}"'
    sign_attribute = ' sign="-"' if negative else ""
    inline_cells = "".join(f'<td><ix:nonFraction id="{item.occurrence_key}" name="g:{item.concept_name}" '
        f'contextRef="{item.source_reference["context_ref"]}" unitRef="usd"{scale_attribute}{sign_attribute}>24</ix:nonFraction></td>'
        for item in facts)
    displayed = "25" if defect == "wrong_value" else "24"
    if negative:
        displayed = f"({displayed})"
    if defect == "tagged_target":
        displayed = (f'<ix:nonFraction id="Fextra" name="g:{fact.concept_name}" '
                     f'contextRef="c1" unitRef="usd"{scale_attribute}>24</ix:nonFraction>')
    target = f'<td>{displayed}</td>' if repeat else ""
    source = ('<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:g="http://fasb.org/us-gaap/2026">'
              f'<body><table><tr><td>Closing cash</td>{inline_cells}{target}</tr></table></body></html>').encode()
    tree = html.document_fromstring(source).getroottree()
    rows, grid = layout_grid(tree.xpath('//table')[0], tree)
    scope = {"filing_id": FILING, "xml_document_id": str(fact.source_document_id), "facts_version": "facts-v1"}
    refs = []
    for item in facts:
        node = tree.xpath(f'//*[@id="{item.occurrence_key}"]')[0]
        refs.append(fact_evidence(canonical(item.__dict__)) | {"xml_id": item.occurrence_key, "scope": scope,
            "method": "inline_id", "inline_xpath": inline_source_xpath(tree, node),
            "scale": scale or "0", "inline_sign": "-" if negative else None, "format": None})
    for index, cell in enumerate(rows[0]):
        cell.pop("_element")
        reference = refs[index - 1] if 0 < index <= len(refs) else None
        if repeat and index == len(rows[0]) - 1:
            reference = refs[0] | {"method": "same_row_semantic_repeat",
                                  "repeated_from_inline_xpath": refs[0]["inline_xpath"]}
        cell.update(row_heading="Closing cash", column_headings=[], column_period=reference["period"] if reference else None,
                    classification="resolved" if reference else "label", fact_references=[reference] if reference else [],
                    unresolved_reason=None, is_total=False)
    if repeat:
        rows[0][-1]["column_period"] = {"startDate": "2026-01-01", "endDate": "2026-06-30"}
    if defect == "wrong_period":
        rows[0][-1]["column_period"] = {"instant": "2025-06-30"}
    if defect == "missing_origin":
        rows[0][1].update(classification="label", fact_references=[])
    table = FinancialTableInput(filing_id=UUID(FILING), source_document_id=UUID(HTML), extraction_version="tables-v1",
        table_key="cash_flow", statement_type="cash_flow", readable_text="Closing cash",
        structure={"schema_version": 1, "rows": rows, "grid": grid, "fact_scope": scope,
            "source_xpath": tree.getpath(tree.xpath('//table')[0]), "html_sha256": hashlib.sha256(source).hexdigest(),
            "xml_sha256": "a" * 64})
    return table, facts, source


@pytest.mark.parametrize("scale", [None, "0", "6"])
@pytest.mark.parametrize("wrong_scale", [False, True])
def test_original_inline_scale_uses_extractor_default_and_rejects_mismatch(scale, wrong_scale):
    from sec_pipeline.table_relations import audit_snapshot
    table, facts, source = inline_relationship_fixture(scale=scale)
    if wrong_scale:
        table.structure["rows"][0][1]["fact_references"][0]["scale"] = "2"
    result = audit_snapshot(relationship_snapshot(table, facts), {HTML: source})
    assert bool(result["errors"]) is wrong_scale
    if wrong_scale:
        assert "original_inline_evidence_mismatch" in {error["reason"] for error in result["errors"]}


@pytest.mark.parametrize("defect", [None, "wrong_value", "wrong_period", "ambiguous_occurrences", "ambiguous_semantics", "missing_origin", "tagged_target"])
def test_original_semantic_repeat_requires_value_period_and_unique_direct_origin(defect):
    from sec_pipeline.table_relations import audit_snapshot
    table, facts, source = inline_relationship_fixture(scale="6", repeat=True, defect=defect)
    result = audit_snapshot(relationship_snapshot(table, facts), {HTML: source})
    assert bool(result["errors"]) is bool(defect)
    if defect:
        assert "original_inline_evidence_mismatch" in {error["reason"] for error in result["errors"]}


def test_original_negative_repeat_uses_parentheses_without_applying_inline_sign_twice():
    from sec_pipeline.table_relations import audit_snapshot
    table, facts, source = inline_relationship_fixture(scale="6", repeat=True, negative=True)
    result = audit_snapshot(relationship_snapshot(table, facts), {HTML: source})
    assert result["errors"] == []


def test_trace_limit_is_per_table_not_global():
    """Later physical/continuation tables must not lose all source examples."""
    from sec_pipeline.table_relations import audit_snapshot
    base=accepted_relationship_snapshot()
    snapshot=deepcopy(base)
    snapshot['tables']=[];snapshot['table_cells']=[]
    snapshot['cell_fact_links']=[];snapshot['financial_table_facts']=[]
    for number in range(4):
        table_id=str(UUID(int=2000+number))
        snapshot['tables'].append(deepcopy(base['tables'][0]) | {'id':table_id,'table_key':f'fixture.segment_{number}'})
        cell_ids={row['id']:str(UUID(int=3000+number*100+index)) for index,row in enumerate(base['table_cells'])}
        snapshot['table_cells'].extend(deepcopy(row) | {'id':cell_ids[row['id']],'table_id':table_id} for row in base['table_cells'])
        snapshot['cell_fact_links'].extend(deepcopy(row) | {'cell_id':cell_ids[row['cell_id']],'table_id':table_id} for row in base['cell_fact_links'])
        snapshot['financial_table_facts'].extend(deepcopy(row) | {'table_id':table_id} for row in base['financial_table_facts'])
    result=audit_snapshot(snapshot,{})
    assert {row['table_id'] for row in result['trace_examples']}=={row['id'] for row in snapshot['tables']}
