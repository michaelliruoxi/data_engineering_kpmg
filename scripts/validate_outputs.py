"""Microsoft 10-Q local validation (reviewed against main 2b8ed25).

Run from the latest team repository:
    uv sync --locked
    uv run --locked python sally_check.py

Reports are saved to data/processed/sally-check-<timestamp>.json.
Use --repo PATH when running from outside the repository.

Checks local team outputs against original XML/HTML without SEC downloads or
database access. A local PASS does not establish full project acceptance.
Database validation, independent peer review, and release checks remain pending.
The reference scope must be reviewed before extending to other filings.

Exit codes: 0 = local checks passed; 1 = failed checks; 2 = execution error.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

SAMPLE_HASHES = {
    "main_inline_xbrl_filing": "a615ae0de8fe27dd8d1032978b1c810305d3cf7a864b386807cd25a73fc70d59",
    "extracted_xbrl_instance": "d9c4e6b92fccf521277f96ddc8fceb95f9708495f8fa245137e5054a3947cef1",
    "xbrl_extension_schema": "11bfc25e715493f9750956eee727384f1d0db44b0298dbbf9055b80a2363dcb3",
    "filing_index": "c03cc6c008535d590fe6be2033f476eac451747db283a5ebccba81759d12849b",
}


def normalize(value):
    return " ".join(value.split())


def validate(manifest, xml_path, html_path, facts, tables, report, chunks):
    """Check supplied team outputs; does not regenerate or repair bad outputs."""
    from lxml import etree, html

    results = []

    def check(name, expected, actual, evidence=""):
        results.append(dict(check=name, status="PASS" if expected == actual else "FAIL",
                            expected=expected, actual=actual, evidence=evidence))

    def problems(name, errors):
        check(name, 0, len(errors), errors[:10])

    root = etree.parse(str(xml_path), etree.XMLParser(resolve_entities=False, no_network=True))
    original = {e.get("id"): e for e in root.getroot() if e.get("contextRef")}
    html_tree = html.document_fromstring(html_path.read_bytes(),
                                        parser=html.HTMLParser(encoding="utf-8", no_network=True))
    ns = {"x": "http://www.xbrl.org/2003/instance"}
    contexts = {e.get("id"): e for e in root.findall("x:context", ns)}
    check("filing_scope", ("0000789019", "10-Q", "0001193125-26-191507"),
          (manifest["cik"], manifest["form"], manifest["accession_number"]))
    check("source_fact_inventory", 1622, len(original))
    check("extracted_fact_inventory", len(original), len(facts))
    ids = [f.source_reference.get("element_id") for f in facts]
    check("fact_ids_unique", len(ids), len(set(ids)))
    problems("all_source_occurrences_present", sorted(set(original).symmetric_difference(ids), key=str))
    errors = []
    for f in facts:
        fid = f.source_reference.get("element_id")
        e = original.get(fid)
        if e is None:
            errors.append(f"Unknown fact {fid}")
            continue
        raw = "".join(e.itertext())
        nil = e.get("{http://www.w3.org/2001/XMLSchema-instance}nil") in ("true", "1")
        expected_numeric = Decimal(raw.strip()) if e.get("unitRef") and not nil else None
        period = contexts[e.get("contextRef")].find("x:period", ns)
        values = {etree.QName(p).localname: p.text for p in period}
        expected = (e.tag, nil, None if nil else raw, expected_numeric,
                    e.get("contextRef"), e.get("unitRef"),
                    values.get("startDate"), values.get("endDate"), values.get("instant"))
        actual = (f"{{{f.concept_namespace}}}{f.concept_name}", f.is_nil,
                  f.raw_value, f.numeric_value, f.source_reference.get("context_ref"),
                  f.source_reference.get("unit_ref"),
                  str(f.period_start) if f.period_start else None,
                  str(f.period_end) if f.period_end else None,
                  str(f.instant_date) if f.instant_date else None)
        if expected != actual:
            errors.append(dict(id=fid, expected=expected, actual=actual))
    problems("fact_values_nil_context_and_period_against_xml", errors)
    check("nil_occurrences", 2, sum(f.is_nil for f in facts))

    check("three_primary_statements", Counter(income_statement=1, balance_sheet=1, cash_flow=1),
          Counter(t.statement_type for t in tables))
    errors = []
    counts = {}
    available = set(ids)
    for table in tables:
        cells = [c for row in table.structure["rows"] for c in row]
        counts[table.statement_type] = sum(c["classification"] == "resolved" for c in cells)
        for c in cells:
            label = f"{table.statement_type}/{c['cell_id']}"
            nodes = html_tree.xpath(c["source_xpath"])
            if len(nodes) != 1 or normalize("".join(nodes[0].itertext())) != c["display_text"]:
                errors.append(label + ": displayed text differs from original HTML")
            refs = c.get("fact_references", [])
            if c["classification"] == "unresolved":
                errors.append(label + ": unresolved financial cell")
            if c["classification"] in ("resolved", "resolved_label") and not refs:
                errors.append(label + ": missing fact link")
            for ref in refs:
                e = original.get(ref["xml_id"])
                if e is None or ref["xml_id"] not in available:
                    errors.append(label + ": linked fact missing")
                elif (ref["concept"] != e.tag or ref["context_ref"] != e.get("contextRef")
                      or ref.get("unit_ref") != e.get("unitRef")
                      or normalize(ref["raw_value"]) != normalize("".join(e.itertext()))):
                    errors.append(label + ": linked evidence differs from XML")
        for name, path in (("html_sha256", html_path), ("xml_sha256", xml_path)):
            if table.structure.get(name) != hashlib.sha256(path.read_bytes()).hexdigest():
                errors.append(table.statement_type + ": source checksum mismatch")
    check("financial_cell_counts", dict(income_statement=76, balance_sheet=66, cash_flow=132), counts)
    problems("statement_cell_text_and_fact_links", errors)

    text = report.full_text
    check("report_nonempty", True, bool(text.strip()))
    errors = []
    sections = {s["section_id"]: s for s in report.sections}
    coverage = bytearray(len(text))
    previous_end = 0
    for s in report.sections:
        start, end = s["offset_start"], s["offset_end"]
        if not 0 <= previous_end <= start < end <= len(text):
            errors.append(f"Invalid section range: {s['section_id']}")
            continue
        if text[previous_end:start].strip():
            errors.append("Uncovered text between sections")
        previous_end = end
        locator = s.get("source_locator", {}).get("xpath")
        if not locator or len(html_tree.xpath(locator)) != 1:
            errors.append(f"Section has no unique original locator: {s['section_id']}")
    if text[previous_end:].strip():
        errors.append("Uncovered report tail")
    check("chunk_indices", list(range(len(chunks))), [c.chunk_index for c in chunks])
    for c in chunks:
        start, end = c.offset_start, c.offset_end
        section = sections.get(c.section)
        if not 0 <= start < end <= len(text) or c.text != text[start:end]:
            errors.append(f"Chunk {c.chunk_index}: not an exact report slice")
            continue
        coverage[start:end] = b"\x01" * (end - start)
        if not section or not section["offset_start"] <= start < end <= section["offset_end"]:
            errors.append(f"Chunk {c.chunk_index}: outside its section")
        if len(c.text) > 1500:
            errors.append(f"Chunk {c.chunk_index}: exceeds configured length")
    check("uncovered_nonwhitespace_characters", 0,
          sum(not covered and not char.isspace() for covered, char in zip(coverage, text)))
    problems("section_locations_and_chunk_slices", errors)

    # Source locators selected independently in Sally's earlier reference preparation.
    # Compare actual original text, not expected text copied from the cleaner output.
    passages = [("income tax", '/html/body/div[63]/div[1]/*[local-name()="ix:nonnumeric"]/p[3]'),
                ("quarter revenue", "/html/body/div[105]/p[3]"),
                ("nine-month revenue", "/html/body/div[105]/p[11]"),
                ("liquidity", "/html/body/div[132]/p[3]"),
                ("controls", "/html/body/div[144]/p[17]"),
                ("legal", "/html/body/div[147]/p[3]")]
    for title, xpath in passages:
        nodes = html_tree.xpath(xpath)
        source = normalize("".join(nodes[0].itertext())) if len(nodes) == 1 else ""
        check("source_passage: " + title, True, bool(source) and source in normalize(text), xpath)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Latest team repository folder")
    args = parser.parse_args()
    repo = args.repo.resolve()
    sys.path.insert(0, str(repo / "src"))
    evidence = dict(scope="Microsoft 10-Q / LOCAL OUTPUTS ONLY", reviewed_commit="2b8ed25",
                    acceptance="UNVERIFIED", checks=[],
                    pending=["Stored database results and exact processing versions",
                             "Teammate review of independent reference samples and semantic links",
                             "Complete units/dimensions validation and source-tracing presentation",
                             "Michael's replay/rollback acceptance evidence"])
    try:
        manifest = json.loads((repo / "source_manifest.json").read_text())
        evidence["accession"] = manifest["accession_number"]
        sources = manifest["files"] + [manifest["index_document"]]
        if {s["role"]: s["sha256"] for s in sources} != SAMPLE_HASHES:
            raise ValueError("Source manifest differs from the reviewed Microsoft sample; update references before checking.")
        for source in sources:
            actual = hashlib.sha256((repo / source["local_path"]).read_bytes()).hexdigest()
            if actual != source["sha256"]:
                raise ValueError("Source checksum mismatch: " + source["local_path"])
        paths = {s["role"]: repo / s["local_path"] for s in manifest["files"]}
        from sec_pipeline.fact_extraction import extract_facts
        from sec_pipeline.tables import extract_tables
        from sec_pipeline.report_cleaning import clean_report
        from sec_pipeline.chunking import chunk_report
        xml, html = paths["extracted_xbrl_instance"], paths["main_inline_xbrl_filing"]
        print("Running the team's facts, tables, cleaner and chunker on local SEC files...")
        facts = extract_facts(xml, filing_id=UUID(int=1), source_document_id=UUID(int=3)).require_complete().facts
        tables = extract_tables(html, xml, filing_id=UUID(int=1), html_document_id=UUID(int=2), xml_document_id=UUID(int=3))
        report = clean_report(html, form=manifest["form"])
        chunks = chunk_report(filing_id=UUID(int=1), report_id=UUID(int=4), full_text=report.full_text, sections=report.sections)
        evidence["source_hashes"] = {s["local_path"]: s["sha256"] for s in sources}
        evidence["code_hashes"] = {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in sorted((repo / "src" / "sec_pipeline").glob("*.py"))}
        evidence["checker_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        evidence["versions"] = dict(facts="facts-v1", tables="tables-v1", chunks="paragraph-v1",
                                    report="current checked-out cleaner; no release version asserted")
        evidence["counts"] = dict(facts=len(facts), tables=len(tables), sections=len(report.sections), chunks=len(chunks))
        evidence["checks"] = validate(manifest, xml, html, facts, tables, report, chunks)
        passed = bool(evidence["checks"]) and all(c["status"] == "PASS" for c in evidence["checks"])
        evidence["local_automated_status"] = "PASS" if passed else "FAIL"
        code = 0 if passed else 1
    except Exception as exc:
        evidence["local_automated_status"] = "ERROR"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        code = 2
    output = repo / "data" / "processed" / ("sally-check-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n")
    for row in evidence["checks"]:
        print(f"{row['status']:4}  {row['check']}")
    print("Local automated result:", evidence["local_automated_status"])
    if "error" in evidence:
        print(evidence["error"])
    print("Full project acceptance: UNVERIFIED — see pending items in the report.")
    print("Report:", output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
