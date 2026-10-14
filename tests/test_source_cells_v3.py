"""Static source markers are never converted into invented amounts or nil facts."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
import hashlib
from uuid import UUID

from lxml import html
import pytest

from sec_pipeline.database import FactInput, FinancialTableInput
from sec_pipeline.source_cells_v3 import (
    POLICY, SourceBindings, SourceCellError, source_cell_counts, transform_tables, validate_representations,
)
from sec_pipeline.source_locators import source_xpath
from sec_pipeline.table_cells import TableCellError, relational_cells
from sec_pipeline.tables import layout_grid


IX = "http://www.xbrl.org/2013/inlineXBRL"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
UNIT = {"numerator": ["{http://www.xbrl.org/2003/iso4217}USD"], "denominator": []}


def fixture(*, binding=None, literal="—", hidden=True, hidden_extra="", body_extra="", wrapper="",
            style_extra="", namespace=XSI, filing=UUID(int=1), html_id=UUID(int=2), xml_id=UUID(int=3)):
    hidden_markup = (f'<div style="display:none"><ix:header><ix:hidden><ix:nonFraction id="F1" '
                     f'name="g:PreferredStockValue" contextRef="c" unitRef="usd" xs:nil="true"/>'
                     f'{hidden_extra}</ix:hidden></ix:header></div>') if hidden else ""
    value = f'<span style="{binding}">{literal}</span>' if binding is not None else literal
    table_markup = f'<table><tr><td>Preferred stock</td><td>{value}</td></tr></table>'
    if wrapper:
        table_markup = f'<{wrapper}>{table_markup}</{wrapper}>'
    source = (f'<html xmlns:ix="{IX}" xmlns:xs="{namespace}" xmlns:g="urn:gaap" xmlns:foreign="urn:foreign">'
              f'<body>{hidden_markup}{table_markup}{body_extra}{style_extra}</body></html>').encode()
    tree = html.document_fromstring(source, parser=html.HTMLParser(encoding="utf-8", no_network=True)).getroottree()
    table = tree.xpath("//table")[0]
    rows, grid = layout_grid(table, tree)
    for row in rows:
        for cell in row:
            cell.pop("_element")
            cell.update(row_heading="Preferred stock", column_headings=["July 31, 2026"] if cell["column"] else [],
                        column_period={"instant": "2026-07-31"} if cell["column"] else None,
                        classification="unresolved" if cell["column"] else "label", fact_references=[],
                        unresolved_reason="untagged_financial_cell" if cell["column"] else None, is_total=False)
    fact = FactInput(filing_id=filing, source_document_id=xml_id, extraction_version="facts-v1",
        occurrence_key="F1", concept_namespace="urn:gaap", concept_name="PreferredStockValue",
        period_kind="instant", instant_date=date(2026, 7, 31), unit=UNIT, is_nil=True,
        source_reference={"element_id": "F1", "context_ref": "c", "unit_ref": "usd"})
    table = FinancialTableInput(filing_id=filing, source_document_id=html_id, extraction_version="tables-v2",
        table_key="balance_sheet", statement_type="balance_sheet", readable_text="Preferred stock\t" + literal,
        structure={"schema_version": 1, "rows": rows, "grid": grid, "source_xpath": source_xpath(tree, table),
                   "html_sha256": hashlib.sha256(source).hexdigest(), "xml_sha256": "b" * 64,
                   "inventory": {"label": 1, "unresolved": 1},
                   "fact_scope": {"filing_id": str(filing), "xml_document_id": str(xml_id), "facts_version": "facts-v1"}})
    return table, [fact], source


def marker(table):
    return table.structure["rows"][0][1]


def test_unbound_marker_retains_literal_without_creating_a_fact_or_link():
    original, facts, source = fixture()
    before, facts_before = deepcopy(original), deepcopy(facts)
    [table] = transform_tables([original], source, facts)
    assert original == before and facts == facts_before
    cell = marker(table)
    assert table.extraction_version == "tables-v3" and table.structure["schema_version"] == 2
    assert cell["classification"] == "source_only_marker" and cell["display_text"] == "—"
    assert cell["fact_references"] == [] and cell["unresolved_reason"] is None
    assert cell["source_only_evidence"]["verification_version"] == POLICY
    assert "numeric_value" not in cell and "is_nil" not in cell
    [(label, _), (data, refs)] = relational_cells(UUID(int=4), table)
    assert (data.kind, data.resolution_status, refs) == ("data", "source_only", [])
    assert validate_representations([table], source, facts) == {
        "financial_cells": 1, "linked_financial_cells": 0, "source_only_marker_cells": 1,
        "hidden_nil_cells": 0, "unresolved_cells": 0,
    }


def test_explicit_hidden_nil_binding_keeps_exact_source_fact_and_namespace_alias():
    original, facts, source = fixture(binding="color:black; -sec-ix-hidden:F1;")
    [table] = transform_tables([original], source, facts)
    cell = marker(table)
    assert cell["classification"] == "resolved"
    [ref] = cell["fact_references"]
    assert ref["method"] == "hidden_inline_id" and ref["xml_id"] == "F1" and ref["is_nil"] is True
    assert ref["binding_style"] == "color:black; -sec-ix-hidden:F1;"
    assert ref["raw_value"] == "" and facts[0].numeric_value is None
    assert validate_representations([table], source, facts)["hidden_nil_cells"] == 1
    assert relational_cells(UUID(int=4), table)[1][0].resolution_status == "resolved"


def test_external_script_is_recorded_without_execution_or_runtime_completeness_claim():
    original, facts, source = fixture(body_extra='<script src="/security/bootstrap.js"></script>')
    [table] = transform_tables([original], source, facts)
    proof = marker(table)["source_only_evidence"]
    assert proof["runtime_scope"] == "scripts_not_executed"
    assert proof["binding_scope"] == "archived_dom_numeric_and_sec_hidden_bindings"
    assert proof["external_scripts"] == [{"source_xpath": "/html/body/script", "src": "/security/bootstrap.js"}]
    assert validate_representations([table], source, facts)["source_only_marker_cells"] == 1


@pytest.mark.parametrize("direct", [False, True])
def test_actual_binding_still_prevents_source_only_even_with_external_script(direct):
    literal = ('<ix:nonFraction id="F2" name="g:PreferredStockValue" contextRef="c" unitRef="usd">—</ix:nonFraction>'
               if direct else "—")
    original, facts, source = fixture(binding=None if direct else "-sec-ix-hidden:F1",
                                      literal=literal, body_extra='<script src="/security.js"></script>')
    [table] = transform_tables([original], source, facts)
    assert marker(table)["classification"] != "source_only_marker"
    assert source_cell_counts([table])["source_only_marker_cells"] == 0


@pytest.mark.parametrize("kwargs", [
    {"style_extra": "<style>td { color:black }</style>"},
    {"style_extra": '<link rel="stylesheet" href="layout.css"/>'},
    {"body_extra": "<script>unknownActiveContent()</script>"},
    {"wrapper": "foreign:nonFraction"}, {"wrapper": "foreign:fraction"},
    {"wrapper": "ix:nonFraction"},
    {"binding": "-sec-ix-hidden:missing"},
    {"binding": "-sec-ix-hidden:F1;-sec-ix-hidden:F1"},
    {"binding": "-sec-ix-hidden:var(--fact)"},
    {"binding": "display:none"},
    {"binding": "visibility:hidden;-sec-ix-hidden:F1"},
    {"binding": "-sec\\2d ix-hidden:F1"},
    {"binding": "-sec-ix-hidden:F1", "namespace": "urn:foreign"},
    {"binding": "-sec-ix-hidden:F1", "body_extra": '<span style="-sec-ix-hidden:F1">—</span>'},
    {"binding": "-sec-ix-hidden:F1", "body_extra": '<span style="-sec\\2d ix-hidden:F1">—</span>'},
    {"binding": "-sec-ix-hidden:F1", "hidden_extra": '<ix:nonFraction id="F1" name="g:Other"/>'},
])
def test_ambiguous_or_unsupported_bindings_remain_unresolved(kwargs):
    original, facts, source = fixture(**kwargs)
    [table] = transform_tables([original], source, facts)
    assert marker(table)["classification"] == "unresolved"
    assert marker(table)["fact_references"] == []
    assert source_cell_counts([table])["unresolved_cells"] == 1


@pytest.mark.parametrize("alteration", [
    {"is_nil": False, "raw_value": "0", "numeric_value": Decimal("0")},
    {"instant_date": date(2025, 7, 31)}, {"filing_id": UUID(int=99)},
    {"source_document_id": UUID(int=99)}, {"extraction_version": "facts-v2"},
])
def test_hidden_reference_requires_nil_and_exact_current_source_scope(alteration):
    original, facts, source = fixture(binding="-sec-ix-hidden:F1")
    facts = [replace(facts[0], **alteration)]
    [table] = transform_tables([original], source, facts)
    assert marker(table)["classification"] == "unresolved"


@pytest.mark.parametrize("tamper", ["formatting", "literal", "fake_fact", "invented_nil", "inferred_value", "evidence_hash"])
def test_source_only_claims_are_independently_rechecked_against_original_dom(tamper):
    original, facts, source = fixture()
    [table] = transform_tables([original], source, facts)
    cell = marker(table)
    if tamper == "formatting":
        cell["classification"] = "formatting"
        cell.pop("source_only_evidence")
    elif tamper == "literal":
        cell["display_text"] = "0"
    elif tamper == "fake_fact":
        cell["fact_references"] = [{"method": "inline_id", "xml_id": "F1"}]
    elif tamper == "invented_nil":
        cell["classification"] = "resolved"
        cell["fact_references"] = [{"method": "hidden_inline_id", "xml_id": "F1"}]
    elif tamper == "inferred_value":
        cell["numeric_value"] = 0
        cell["is_nil"] = True
    else:
        cell["source_only_evidence"]["html_sha256"] = "0" * 64
    table.structure["inventory"] = dict(Counter(c["classification"] for row in table.structure["rows"] for c in row))
    with pytest.raises(SourceCellError):
        validate_representations([table], source, facts)


def test_new_hidden_binding_in_source_invalidates_old_absence_evidence():
    original, facts, source = fixture()
    [table] = transform_tables([original], source, facts)
    changed = source.replace("<td>—</td>".encode(), '<td style="-sec-ix-hidden:F1">—</td>'.encode())
    with pytest.raises(SourceCellError):
        validate_representations([table], changed, facts)


@pytest.mark.parametrize("schema,version", [(1, "tables-v3"), (2, "tables-v2")])
def test_source_aware_cells_cannot_use_legacy_schema_or_extraction_version(schema, version):
    original, facts, source = fixture()
    [table] = transform_tables([original], source, facts)
    table.structure["schema_version"] = schema
    with pytest.raises(TableCellError):
        relational_cells(UUID(int=4), replace(table, extraction_version=version))


def test_plain_numbers_are_not_admitted_as_source_only_markers():
    original, facts, source = fixture(literal="0")
    [table] = transform_tables([original], source, facts)
    assert marker(table)["classification"] == "unresolved"
    assert marker(table)["display_text"] == "0"
