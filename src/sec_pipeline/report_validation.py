"""Offline checks of freshly parsed reports against explicitly scoped originals.

This is a parser regression audit, not a database check or a claim that a sample
represents every historical filing. Expected Item/Part identifiers must come
from independent source review; the detector is never its own coverage oracle.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote
from uuid import UUID

from lxml import etree, html

from .chunking import chunk_report
from .report_cleaning import clean_report


TARGET_TICKERS = ("AAPL", "CASY", "MCK", "MDT", "MSFT", "MU", "NKE", "NTAP", "PH", "TTWO")
FORMS = ("10-K", "10-Q")
_REPEATED_PIPES = re.compile(r"\|(?:[^\S\r\n]*\|)+")
_TOC_ITEM = re.compile(r"^ITEM\s*(\d{1,2}(?:\s*[A-Z](?=[\s.:]|$))?)(?:[\s.:\u2013\u2014-]|$)", re.I)
_TOC_PART = re.compile(r"^PART\s+(IV|III|II|I)(?:[\s.:\u2013\u2014-]|$)", re.I)


def _original_body_outline(tree, *, form, toc_tables=()):
    """Independent original DOM outline for omitted TOC entries/no-TOC forms."""
    from .source_locators import source_xpath

    blocks = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
    result, seen, active_part = [], set(), None
    for node in tree.iter():
        if node.tag not in blocks or any(child.tag in blocks for child in node.iterdescendants()):
            continue
        text = " ".join(" ".join(node.itertext()).split())
        if not text or len(text) > 300:
            continue
        part = re.fullmatch(r"PART\s+(IV|III|II|I)(?:[\s.\u2013\u2014-]*(?:FINANCIAL (?:INFORMATION|STATEMENTS)|OTHER INFORMATION))?[\s.]*", text, re.I)
        item = _TOC_ITEM.match(text)
        if not part and not item:
            continue
        if any(ancestor in toc_tables for ancestor in (node, *node.iterancestors())):
            continue
        if item and re.search(r"\s\d+\s*$", text):
            continue
        if node.xpath(".//a[starts-with(@href, '#')]") or any(a.get("href") for a in node.iterancestors("a")):
            continue
        lineage = [node, *node.iterancestors()]
        if any("hidden" in ancestor.attrib or re.search(r"display\s*:\s*none", ancestor.get("style", ""), re.I) for ancestor in lineage):
            continue
        emphasized = text == text.upper() or any(
            element.tag in {"b", "strong", "h1", "h2", "h3", "h4", "h5", "h6"}
            or re.search(r"font-weight\s*:\s*(?:bold|[6-9]00)|text-decoration(?:-line)?\s*:[^;]*underline",
                         element.get("style", ""), re.I) for element in node.iter())
        if not emphasized:
            continue
        if part:
            active_part = part[1].upper()
            key = f"part-{active_part.lower()}"
        else:
            number = re.sub(r"\s+", "", item[1]).upper()
            title = text[item.end():].strip()
            if not title or re.match(r"(?:of|and|is|was|above|below|herein)\b", title, re.I):
                continue
            if form == "10-K":
                integer = int(re.match(r"\d+", number)[0])
                if not 1 <= integer <= 16:
                    continue
                item_part = "I" if integer <= 4 else "II" if integer <= 9 else "III" if integer <= 14 else "IV"
            else:
                item_part = active_part
                if item_part not in {"I", "II"} or int(re.match(r"\d+", number)[0]) > 6:
                    continue
            key = f"part-{item_part.lower()}-item-{number.lower()}"
        if key not in seen:
            seen.add(key)
            result.append({"section_id": key, "source_xpath": source_xpath(tree, node), "title": text})
    return result


def source_heading_expectations(html_path, *, form):
    """Read heading identities from original linked TOC rows, independently.

    No cleaned text, detector vocabulary, or detected sections are consulted.
    Links must resolve to original IDs/name anchors. Unlinked/ambiguous TOCs
    remain explicitly unverified and require original-source review.
    """
    from .source_locators import source_xpath

    tree = html.document_fromstring(Path(html_path).read_bytes(),
        parser=html.HTMLParser(encoding="utf-8", no_network=True)).getroottree()
    targets = set(tree.xpath("//@id | //a/@name"))
    candidates = []
    for table in tree.iter("table"):
        rows = []
        original_rows = [row for row in table.iter("tr") if next(row.iterancestors("table"), None) is table]
        item_column = any(re.fullmatch(r"ITEM(?:\s+NUMBER)?(?:\s+DESCRIPTION)?\s+PAGE", " ".join(" ".join(row.itertext()).split()), re.I)
                          for row in original_rows[:4])
        for row_index, row in enumerate(original_rows):
            value = " ".join(" ".join(row.itertext()).split())
            item, part = _TOC_ITEM.match(value), _TOC_PART.match(value)
            if part:
                # Some TOCs put the Part and its first Item in the same row.
                remainder = value[part.end():].strip()
                item = _TOC_ITEM.match(remainder)
            if not item and item_column:
                cells = [" ".join(" ".join(cell.itertext()).split()) for cell in row if cell.tag in {"td", "th"}]
                nonempty = [cell for cell in cells if cell]
                if nonempty and re.fullmatch(r"\d{1,2}(?:\s*[A-Z])?[\s.]*", nonempty[0], re.I):
                    item = re.match(r"(\d{1,2}(?:\s*[A-Z])?)", nonempty[0], re.I)
            if not item and not part:
                continue
            hrefs = [a.get("href")[1:] for a in row.iter("a")
                     if a.get("href", "").startswith("#") and a.get("href")[1:] in targets]
            evidence = {"text": value, "source_xpath": source_xpath(tree, row), "targets": hrefs}
            if item and not hrefs and not re.search(r"\d+[\s.]*$", value):
                # A financial-statements group label can be unlinked, with
                # the statement names and page links in the following rows.
                group_targets = []
                for following in original_rows[row_index + 1:]:
                    following_text = " ".join(" ".join(following.itertext()).split())
                    if _TOC_ITEM.match(following_text) or _TOC_PART.match(following_text):
                        break
                    if re.search(r"\d+[\s.]*$", following_text):
                        group_targets.extend(a.get("href")[1:] for a in following.iter("a")
                            if a.get("href", "").startswith("#") and a.get("href")[1:] in targets)
                if group_targets:
                    evidence.update(targets=sorted(set(group_targets)), navigation_on_child_rows=True)
            if part:
                rows.append({"item": None, "part": part[1].upper(), **evidence})
            if item:
                rows.append({"item": re.sub(r"\s+", "", item[1]).upper(), "part": None, **evidence})
        items = [row for row in rows if row["item"]]
        # A linked row with a page column distinguishes TOC entries from body
        # headings or prose cross-references in layout tables.
        linked = [row for row in items if row["targets"] and re.search(r"\d+[\s.]*$", row["text"])]
        if len(items) >= 2 and len(linked) >= max(2, len(items) // 2) and all(row["targets"] for row in items):
            candidates.append({"source_xpath": source_xpath(tree, table), "rows": rows})
    toc_tables = {tree.xpath(candidate["source_xpath"])[0] for candidate in candidates}
    body_outline = _original_body_outline(tree, form=form, toc_tables=toc_tables)
    body_ids = {heading["section_id"] for heading in body_outline}
    required = ({"part-i-item-1", "part-ii-item-7", "part-ii-item-8", "part-iv-item-15"}
                if form == "10-K" else {"part-i-item-1", "part-i-item-2", "part-ii-item-6"})
    if not candidates:
        if required.issubset(body_ids):
            return {"status": "derived_from_original_body", "method": "Original emphasized Part/Item DOM blocks without navigation links; independent of report parser",
                    "expected_section_ids": [heading["section_id"] for heading in body_outline], "body_headings": body_outline}
        return {"status": "unverified", "reason": "no_unambiguous_linked_original_toc", "expected_section_ids": None,
                "body_headings": body_outline}

    expected, evidence = [], []
    active_part = None
    for table in candidates:
        for row in table["rows"]:
            if row["part"]:
                active_part = row["part"]
                key = f"part-{active_part.lower()}"
            else:
                item = row["item"]
                if form == "10-K":
                    number = int(re.match(r"\d+", item)[0])
                    part = "I" if number <= 4 else "II" if number <= 9 else "III" if number <= 14 else "IV"
                    if not 1 <= number <= 16:
                        return {"status": "unverified", "reason": "unexpected_annual_item", "expected_section_ids": None}
                else:
                    part = active_part
                    if part not in {"I", "II"}:
                        return {"status": "unverified", "reason": "quarterly_toc_part_not_explicit", "expected_section_ids": None}
                key = f"part-{part.lower()}-item-{item.lower()}"
            if key in expected:
                return {"status": "unverified", "reason": "duplicate_original_toc_heading", "expected_section_ids": None}
            expected.append(key)
            evidence.append({"section_id": key, "source_xpath": row["source_xpath"],
                             "title": row["text"], "navigation_targets": row["targets"],
                             "navigation_on_child_rows": row.get("navigation_on_child_rows", False)})
    # Partial TOCs must not establish complete section coverage.
    if not required.issubset(expected):
        return {"status": "unverified", "reason": "original_toc_does_not_cover_core_parts", "expected_section_ids": None,
                "observed_section_ids": expected, "rows": evidence}
    additions = [heading for heading in body_outline if heading["section_id"] not in expected]
    return {"status": "derived_from_original_toc", "method": "Original DOM table rows with resolving navigation targets and page columns, supplemented by explicit original body headings absent from the TOC; independent of report parser",
            "expected_section_ids": expected + [heading["section_id"] for heading in additions], "rows": evidence,
            "body_headings_absent_from_toc": additions}


def _compact(value):
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def _heading_starts_in_navigation(node):
    """Inspect the first original text node, not later prose links in its row."""
    for value in node.xpath(".//text()"):
        if not _compact(value):
            continue
        parent = value.getparent()
        if value.is_tail:
            parent = parent.getparent()
        if parent is None:
            return False
        link = next((element for element in (parent, *parent.iterancestors())
                     if element.tag == "a" and element.get("href", "").startswith("#")), None)
        if link is None:
            return False
        local_targets = {target for element in (*node.iter(), *node.iterancestors())
                         for target in (element.get("id"), element.get("name")) if target}
        return unquote(link.get("href")[1:]) not in local_targets
    return False


def audit_report(html_path, *, form, expected_section_ids=None, report=None, chunks=None):
    """Audit supplied outputs, or run the cleaner/chunker when outputs are absent.

    Source XPath checks use a separate parse of the unchanged original bytes.
    Expected identifiers include Part headings if those occur in the source.
    A passing structural audit without expected headings remains unverified.
    """
    html_path = Path(html_path)
    source = html_path.read_bytes()
    tree = html.document_fromstring(source, parser=html.HTMLParser(encoding="utf-8", no_network=True)).getroottree()
    report = clean_report(html_path, form=form) if report is None else report
    text = report.full_text
    issues = []

    def issue(check, **evidence):
        issues.append({"check": check, **evidence})

    if not text.strip():
        issue("empty_report")
    pipe_matches = list(_REPEATED_PIPES.finditer(text))
    if pipe_matches:
        issue("repeated_pipes", count=len(pipe_matches), examples=[
            {"offset": m.start(), "text": text[max(0, m.start() - 40):m.end() + 40]}
            for m in pipe_matches[:5]])

    ids = [section.get("section_id") for section in report.sections]
    duplicates = [key for key, count in Counter(ids).items() if count > 1]
    if duplicates:
        issue("duplicate_section_ids", section_ids=duplicates)
    previous_end = 0
    valid_sections = {}
    unknown_chars = 0
    for section in report.sections:
        key = section.get("section_id")
        start, end = section.get("offset_start"), section.get("offset_end")
        if (type(start) is not int or type(end) is not int
                or not 0 <= previous_end <= start < end <= len(text)):
            issue("invalid_section_range", section_id=key, start=start, end=end)
            continue
        if text[previous_end:start].strip():
            issue("uncovered_report_text", start=previous_end, end=start)
        previous_end = end
        valid_sections[key] = section
        # Some recognized annual Items deliberately have an unknown taxonomy.
        # They must not be confused with unclassified document content.
        if key in {"full-report", "unclassified-prefix"} or (
                section.get("section_type") == "unknown" and not section.get("item")):
            unknown_chars += end - start
            issue("unclassified_section", section_id=key, start=start, end=end)
        locator = section.get("source_locator", {})
        xpath = locator.get("xpath")
        try:
            nodes = tree.xpath(xpath) if isinstance(xpath, str) else []
        except etree.XPathError:
            nodes = []
        if len(nodes) != 1 or not isinstance(nodes[0], etree._Element):
            issue("nonunique_original_locator", section_id=key, xpath=xpath)
            continue
        node = nodes[0]
        if locator.get("element_id") and node.get("id") != locator["element_id"]:
            issue("source_element_id_mismatch", section_id=key, xpath=xpath)
        if locator.get("source_file") != html_path.name:
            issue("source_file_mismatch", section_id=key)
        if section.get("item") or section.get("section_type") == "part_heading":
            original_title = section.get("original_title") or section.get("title", "")
            heading = _compact(original_title)
            actual = _compact(text[start:end])
            if not heading or not actual.startswith(heading):
                issue("heading_offset_mismatch", section_id=key, title=original_title)
            original = _compact("".join(node.itertext()))
            # Split headings may continue in the following original block.
            if not original or not (original.startswith(heading) or heading.startswith(original)):
                issue("heading_source_mismatch", section_id=key, xpath=xpath, title=original_title)
            if _heading_starts_in_navigation(node):
                issue("section_starts_in_toc_link", section_id=key, xpath=xpath)
    if text[previous_end:].strip():
        issue("uncovered_report_text", start=previous_end, end=len(text))

    heading_status = "unverified"
    if expected_section_ids is not None:
        if (not isinstance(expected_section_ids, list) or not expected_section_ids
                or any(not isinstance(key, str) or not key.startswith("part-") for key in expected_section_ids)
                or len(expected_section_ids) != len(set(expected_section_ids))):
            issue("invalid_heading_expectations")
            heading_status = "failed"
        else:
            actual_ids = {key for key in ids if isinstance(key, str) and key.startswith("part-")}
            missing = sorted(set(expected_section_ids) - actual_ids)
            unexpected = sorted(actual_ids - set(expected_section_ids))
            heading_status = "failed" if missing or unexpected else "passed"
            if missing or unexpected:
                issue("section_inventory_mismatch", missing=missing, unexpected=unexpected)

    if chunks is None:
        try:
            chunks = chunk_report(filing_id=UUID(int=1), report_id=UUID(int=2),
                                  full_text=text, sections=report.sections)
        except ValueError as error:
            issue("chunk_generation_failed", error=str(error))
            chunks = []
    coverage = bytearray(len(text))
    if [chunk.chunk_index for chunk in chunks] != list(range(len(chunks))):
        issue("chunk_indices_not_contiguous")
    for chunk in chunks:
        start, end = chunk.offset_start, chunk.offset_end
        section = valid_sections.get(chunk.section)
        if not 0 <= start < end <= len(text) or chunk.text != text[start:end]:
            issue("chunk_not_exact_slice", chunk_index=chunk.chunk_index)
            continue
        coverage[start:end] = b"\x01" * (end - start)
        if not section or not section["offset_start"] <= start < end <= section["offset_end"]:
            issue("chunk_crosses_section", chunk_index=chunk.chunk_index)
        if section and (chunk.metadata.get("title") != section.get("title")
                        or chunk.metadata.get("source_locator") != section.get("source_locator")):
            issue("chunk_section_metadata_mismatch", chunk_index=chunk.chunk_index)
        max_chars = chunk.metadata.get("max_chars")
        if type(max_chars) is int and len(chunk.text) > max_chars:
            issue("chunk_exceeds_configured_length", chunk_index=chunk.chunk_index)
        if _REPEATED_PIPES.search(chunk.text):
            issue("chunk_repeated_pipes", chunk_index=chunk.chunk_index)
    uncovered = sum(not covered and not char.isspace() for covered, char in zip(coverage, text))
    if uncovered:
        issue("uncovered_chunk_text", count=uncovered)
    return {
        "status": "failed" if issues else "passed" if heading_status == "passed" else "unverified",
        "heading_inventory_status": heading_status,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "text_characters": len(text), "section_count": len(report.sections), "chunk_count": len(chunks),
        "unclassified_characters": unknown_chars, "repeated_pipe_count": len(pipe_matches),
        "sections": report.sections, "issues": issues,
    }


def audit_manifest(manifest_path, *, tickers=TARGET_TICKERS, forms=FORMS):
    """Check every selected manifest entry and report absent ticker/form pairs."""
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text())
    entries = manifest.get("filings") if isinstance(manifest, dict) else manifest
    if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
        raise ValueError("Manifest must contain a filings array of objects.")
    scope = manifest if isinstance(manifest, dict) else {}
    tickers = tuple(dict.fromkeys(tickers))
    forms = tuple(dict.fromkeys(forms))
    if not tickers or not forms or set(forms) - set(FORMS):
        raise ValueError("Select at least one ticker and one 10-K/10-Q form.")
    selected = [row for row in entries if row.get("ticker") in tickers and row.get("form") in forms]
    present, results = set(), []
    seen = set()
    for row in selected:
        identity = {key: row.get(key) for key in ("ticker", "form", "accession_number", "filing_date")}
        key = (row["ticker"], row["form"], row.get("accession_number"))
        result = identity | {"html_path": row.get("html_path")}
        if not isinstance(key[2], str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", key[2]):
            results.append(result | {"status": "failed", "issues": [{"check": "invalid_accession_number"}]})
            continue
        if key in seen:
            results.append(result | {"status": "failed", "issues": [{"check": "duplicate_filing"}]})
            continue
        seen.add(key)
        path = row.get("html_path")
        if not isinstance(path, str) or not path:
            results.append(result | {"status": "unavailable", "issues": [{"check": "missing_html_path"}]})
            continue
        html_path = manifest_path.parent / path
        if not html_path.is_file():
            results.append(result | {"status": "unavailable", "issues": [{"check": "missing_source_bytes"}]})
            continue
        digest = hashlib.sha256(html_path.read_bytes()).hexdigest()
        if row.get("sha256") and row["sha256"] != digest:
            results.append(result | {"status": "failed", "issues": [{"check": "source_hash_mismatch"}]})
            continue
        present.add((row["ticker"], row["form"]))
        try:
            expected = row.get("expected_section_ids")
            if expected is None:
                evidence = source_heading_expectations(html_path, form=row["form"])
                expected = evidence["expected_section_ids"]
            else:
                evidence = {"status": "explicit_source_review", **row.get("expectation_evidence", {})}
            result["expectation_evidence"] = evidence
            result.update(audit_report(html_path, form=row["form"], expected_section_ids=expected))
            toc_paths = [item["source_xpath"] for item in evidence.get("rows", []) + evidence.get("tables", [])
                         if isinstance(item.get("source_xpath"), str)]
            for section in result["sections"]:
                if not section.get("item") and section.get("section_type") != "part_heading":
                    continue
                xpath = section.get("source_locator", {}).get("xpath", "")
                if any(xpath == path or xpath.startswith(path + "/") for path in toc_paths):
                    result["issues"].append({"check": "section_starts_in_original_toc", "section_id": section["section_id"], "xpath": xpath})
                    result["status"] = "failed"
        except (ValueError, OSError, etree.Error) as error:
            result.update(status="failed", issues=[{"check": "report_audit_error", "error_type": type(error).__name__, "error": str(error)}])
        results.append(result)
    missing = [{"ticker": ticker, "form": form} for ticker in tickers for form in forms if (ticker, form) not in present]
    discovery_issues = []
    historical_scope = bool(scope.get("filing_date_from") or scope.get("filing_date_to"))
    if historical_scope:
        for ticker in tickers:
            company = scope.get("companies", {}).get(ticker, {})
            rows = [row for row in selected if row["ticker"] == ticker]
            if company.get("discovery_status") != "complete":
                discovery_issues.append({"ticker": ticker, "check": "discovery_incomplete"})
            # Counts cover the discovery form set, so only compare a full-form
            # run with that count; a deliberate --form subset is narrower.
            if set(forms) == set(scope.get("forms", FORMS)) and company.get("discovered_count") != len(rows):
                discovery_issues.append({"ticker": ticker, "check": "discovery_inventory_count_mismatch",
                                         "expected": company.get("discovered_count"), "actual": len(rows)})
            for row in rows:
                date = row.get("filing_date")
                if not date or not scope.get("filing_date_from", "") <= date <= scope.get("filing_date_to", "9999-12-31"):
                    discovery_issues.append({"ticker": ticker, "accession_number": row.get("accession_number"),
                                             "check": "filing_outside_declared_date_scope", "filing_date": date})
    status = ("failed" if any(row["status"] == "failed" for row in results) else
              "incomplete" if missing or discovery_issues or any(row["status"] == "unavailable" for row in results) else
              "unverified" if any(row["status"] != "passed" for row in results) else "passed")
    return {"schema_version": 1, "status": status, "scope": "offline_original_source_parser_audit",
            "scope_note": "Original filings in the declared inventory only; historical coverage is limited to its discovery snapshot. Stored database outputs and later filings are not checked.",
            "requested_tickers": list(tickers), "forms": list(forms), "filing_count": len(results),
            "filing_date_from": scope.get("filing_date_from"), "filing_date_to": scope.get("filing_date_to"),
            "discovery_status": "incomplete" if discovery_issues else "complete_for_declared_snapshot" if historical_scope else "not_asserted",
            "discovery_issues": discovery_issues,
            "coverage_status": "incomplete" if missing else "complete_for_requested_ticker_form_pairs",
            "missing_ticker_forms": missing, "filings": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ticker", nargs="+", default=list(TARGET_TICKERS))
    parser.add_argument("--form", nargs="+", choices=FORMS, default=list(FORMS))
    args = parser.parse_args(argv)
    result = audit_manifest(args.manifest, tickers=args.ticker, forms=args.form)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"{result['status']}: {result['filing_count']} filings; {len(result['missing_ticker_forms'])} missing ticker/form pairs. {args.output}")
    return 0 if result["status"] == "passed" else 1 if result["status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
