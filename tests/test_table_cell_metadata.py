"""Synthetic unit fixtures, not receiving-DB producer or acceptance evidence."""
from copy import deepcopy
from uuid import UUID

import pytest

from sec_pipeline.table_cell_metadata import export_table_metadata, table_fragment_metadata


def uid(n):
    return str(UUID(int=n))


@pytest.fixture
def inputs():
    filing, source, xml, report_id, table_id, section_id = [uid(n) for n in range(1, 7)]
    full_text = "Item 1. Financial Statements\n\nRevenue | 100\n\nOther text."
    table = dict(id=table_id, filing_id=filing, source_document_id=source,
                 extraction_version="tables-test", table_key="income_statement",
                 readable_text="Revenue\t100", structure={"html_sha256": "fixture-hash",
                 "fact_scope": {"xml_document_id":xml,"facts_version":"facts-test"}, "rows":[[]]})
    fact = dict(fact_id=uid(10),reference_order=0,source_document_id=xml,
                extraction_version="facts-test",source_reference={"element_id":"F1"},
                evidence={"scope":{"filing_id":filing}},numeric_value="100.00",is_nil=False)
    cells = []
    for i, label in enumerate(["Revenue", "100"]):
        key = f"r000c00{i}"
        xpath = f"/html/body/table/tr/td[{i+1}]"
        table["structure"]["rows"][0].append(dict(cell_id=key,row=0,column=i,rowspan=1,colspan=1,
            display_text=label,source_xpath=xpath,fact_references=[{"xml_id":"F1"}] if i else []))
        cells.append(dict(cell_id=uid(20+i),table_id=table_id,cell_key=key,filing_id=filing,
            source_document_id=source,extraction_version="tables-test",source_sha256="fixture-hash",
            row_index=0,column_index=i,row_span=1,column_span=1,kind="data" if i else "label",
            display_text=label,row_heading="Revenue",column_headings=[],column_period=None,
            source_locator={"xpath":xpath},resolution_status="resolved" if i else "non_fact",
            unresolved_reason=None,is_total=False,linked_facts=[deepcopy(fact)] if i else [],
            fact_count=1 if i else 0,accession_number="0000000001-26-000001"))
    report=dict(id=report_id,filing_id=filing,source_document_id=source,
                extraction_version="report-test",full_text=full_text)
    section=dict(id=section_id,filing_id=filing,report_id=report_id,section_key="part-i-item-1",
                 offset_start=0,offset_end=len(full_text))
    return dict(cells=cells,sections=[section],tables=[table],report=report)


def test_export_retains_existing_receiving_ids_and_native_keys(inputs):
    before = deepcopy(inputs)
    table, = export_table_metadata(**inputs)
    assert table["table_id"] == uid(5) and table["section_id"] == uid(6)
    assert [c["cell_id"] for c in table["cells"]] == [uid(20),uid(21)]
    assert table["cells"][1]["fact_ids"] == [uid(10)]
    assert table["cells"][1]["has_accepted_numeric_fact"]
    assert table["row_column_basis"] == "zero_based_physical_anchor_indices_in_sec.table_cells"
    assert not table["section_mapping_evidence"]["original_html_verified"]
    assert not table["processing_selection_verified"]
    assert inputs == before
    assert [table] == export_table_metadata(**inputs)


def test_duplicate_whole_table_text_is_unresolved_not_guessed(inputs):
    inputs["report"]["full_text"] += "\nRevenue | 100"
    inputs["sections"][0]["offset_end"] = len(inputs["report"]["full_text"])
    with pytest.raises(ValueError, match="exactly one"):
        export_table_metadata(**inputs)


@pytest.mark.parametrize("fault", ["duplicate_uuid", "missing_cell", "column", "text", "locator", "source", "hash", "version", "fact_id", "fact_source", "fact_occurrence", "fact_order", "fact_count", "rounded_number"])
def test_corrupt_receiving_or_reference_evidence_is_rejected(inputs, fault):
    cell = inputs["cells"][1]
    if fault == "duplicate_uuid":cell["cell_id"]=inputs["cells"][0]["cell_id"]
    if fault == "missing_cell":inputs["cells"].pop()
    if fault == "column":cell["column_index"]=7
    if fault == "text":cell["display_text"]="999"
    if fault == "locator":cell["source_locator"]["xpath"]="/wrong"
    if fault == "source":cell["source_document_id"]=uid(90)
    if fault == "hash":cell["source_sha256"]="wrong"
    if fault == "version":cell["extraction_version"]="other"
    if fault == "fact_id":cell["linked_facts"][0]["fact_id"]="not-a-UUID"
    if fault == "fact_source":cell["linked_facts"][0]["source_document_id"]=uid(90)
    if fault == "fact_occurrence":cell["linked_facts"][0]["source_reference"]["element_id"]="wrong"
    if fault == "fact_order":cell["linked_facts"][0]["reference_order"]=2
    if fault == "fact_count":cell["fact_count"]=3
    if fault == "rounded_number":cell["linked_facts"][0]["numeric_value"]=100.0
    with pytest.raises(ValueError):export_table_metadata(**inputs)


def test_review_required_cell_is_retained_but_not_routed_as_verified_numeric(inputs):
    cell=inputs["cells"][1]
    cell.update(resolution_status="review_required",linked_facts=[],fact_count=0)
    inputs["tables"][0]["structure"]["rows"][0][1]["fact_references"]=[]
    table, = export_table_metadata(**inputs)
    assert table["cells"][1]["fact_ids"] == []
    assert not table["cells"][1]["has_accepted_numeric_fact"]
    assert table["cells"][1]["resolution_status"] == "review_required"


def test_multi_fact_cell_keeps_all_refs_and_their_order(inputs):
    cell=inputs["cells"][1]
    second=deepcopy(cell["linked_facts"][0])
    second.update(fact_id=uid(11),reference_order=1,source_reference={"element_id":"F2"})
    cell["linked_facts"].append(second);cell["fact_count"]=2
    inputs["tables"][0]["structure"]["rows"][0][1]["fact_references"].append({"xml_id":"F2"})
    table, = export_table_metadata(**inputs)
    assert table["cells"][1]["fact_ids"] == [uid(10),uid(11)]


def test_section_must_be_same_filing_and_report(inputs):
    inputs["sections"][0]["report_id"]=uid(90)
    with pytest.raises(ValueError,match="one exact report section"):
        export_table_metadata(**inputs)


def test_existing_chunk_identity_is_supplied_not_generated(inputs):
    tables=export_table_metadata(**inputs)
    before=deepcopy(tables)
    a=table_fragment_metadata(chunk_id="unit-fragment-1", selections={uid(5):["r000c001"]}, tables=tables)
    b=table_fragment_metadata(chunk_id="unit-fragment-2", selections={uid(5):["r000c001"]}, tables=tables)
    assert a["chunk_id"]=="unit-fragment-1" and b["chunk_id"]=="unit-fragment-2"
    assert a["table_ids"]==b["table_ids"]==[uid(5)]
    assert a["cells"][0]["cell_id"]==uid(21) and a["fact_ids"]==[uid(10)]
    assert tables==before
    with pytest.raises(ValueError):table_fragment_metadata(chunk_id=None,selections={uid(5):["r000c001"]},tables=tables)


def test_wrong_or_duplicate_cell_keys_reject_all_requested_links(inputs):
    tables=export_table_metadata(**inputs)
    for keys in (["r000c001","missing"],["r000c001","r000c001"],[]):
        with pytest.raises(ValueError):
            table_fragment_metadata(chunk_id="unit",selections={uid(5):keys},tables=tables)
    with pytest.raises(ValueError):
        table_fragment_metadata(chunk_id="unit",selections={uid(99):["r000c001"]},tables=tables)


def test_multi_table_chunk_keeps_scoped_keys_and_rejects_cross_scope(inputs):
    tables=export_table_metadata(**inputs)
    other=deepcopy(tables[0]);other["table_id"]=uid(50)
    for c in other["cells"]:c["cell_id"]=uid(100+c["column_index"])
    tables.append(other)
    result=table_fragment_metadata(chunk_id="unit-multiple-tables",selections={uid(5):["r000c001"],uid(50):["r000c001"]},tables=tables)
    assert len(result["cells"])==2
    assert result["cells"][0]["cell_key"]==result["cells"][1]["cell_key"]
    assert result["cells"][0]["table_id"]!=result["cells"][1]["table_id"]
    other["source_document_id"]=uid(200)
    with pytest.raises(ValueError,match="cannot mix"):
        table_fragment_metadata(chunk_id="unit",selections={uid(5):["r000c001"],uid(50):["r000c001"]},tables=tables)
