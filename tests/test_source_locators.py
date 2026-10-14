"""Original-HTML locators resolve prefixed ancestors without changing old rows."""

import json
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from lxml import etree, html
import pytest

from sec_pipeline import report_cleaning, tables
from sec_pipeline.fact_extraction import IX
from sec_pipeline.source_locators import (
    executable_xpath, inline_elements, inline_source_xpath, source_xpath,
)


ROOT = Path(__file__).resolve().parents[1]


def parse(source):
    return html.document_fromstring(source, parser=html.HTMLParser(encoding="utf-8", no_network=True)).getroottree()


def test_regular_paths_are_identical_to_existing_getpath_results():
    tree = parse(b"<html><body><div><p>first</p><p><span>second</span></p></div><div>last</div></body></html>")
    for element in tree.iter():
        path = source_xpath(tree, element)
        assert path == tree.getpath(element)
        assert tree.xpath(path) == [element]


def test_prefixed_elements_and_ancestors_resolve_exact_original_nodes():
    tree = parse(f"""<html xmlns:ix="{IX}"><body>
      <ix:nonNumeric id="first"><div><p>Earlier content</p></div></ix:nonNumeric>
      <ix:nonNumeric id="second"><ix:continuation><div><table><tr><td>
        <ix:nonFraction id="value">12</ix:nonFraction><ix:nonFraction id="other">34</ix:nonFraction>
      </td></tr></table></div></ix:continuation></ix:nonNumeric>
    </body></html>""".encode())
    unchanged = etree.tostring(tree)
    for element in tree.iter():
        path = source_xpath(tree, element)
        assert tree.xpath(path) == [element]
        assert executable_xpath(path) == path
    fact = tree.xpath("//*[@id='other']")[0]
    path = source_xpath(tree, fact)
    assert "*[name()='ix:nonnumeric'][2]" in path
    assert "*[name()='ix:continuation']" in path
    assert path.endswith("*[name()='ix:nonfraction'][2]")
    assert etree.tostring(tree) == unchanged


def test_inline_reference_identity_stays_raw_and_resolves_through_original_dom_map():
    tree = parse(f"""<html xmlns:fact="{IX}"><body><fact:nonNumeric><p>
      <fact:nonFraction id="first">1</fact:nonFraction>
      <fact:nonFraction id="second">2</fact:nonFraction>
    </p></fact:nonNumeric></body></html>""".encode())
    inline_map = {inline_source_xpath(tree, element): element
                  for element in inline_elements(tree, IX, "nonfraction")}
    assert len(inline_map) == 2
    for raw_path, element in inline_map.items():
        assert raw_path == tree.getpath(element)
        assert inline_map[raw_path] is element
        assert tree.xpath(executable_xpath(raw_path)) == [element]


def test_inline_prefix_matching_checks_nearest_namespace_declaration():
    tree = parse(f"""<html xmlns:ix="{IX}" xmlns:other="{IX}" xmlns:fake="urn:unrelated"><body>
      <ix:nonFraction id="first">1</ix:nonFraction>
      <other:nonFraction id="second">2</other:nonFraction>
      <fake:nonFraction id="wrong">3</fake:nonFraction>
      <div xmlns:ix="urn:shadowed"><ix:nonFraction id="shadowed">4</ix:nonFraction></div>
      <div><new:nonFraction xmlns:new="{IX}" id="local">5</new:nonFraction></div>
      <undeclared:nonFraction id="missing">6</undeclared:nonFraction>
      <ix:nonNumeric id="text">text</ix:nonNumeric>
    </body></html>""".encode())
    assert [node.get("id") for node in inline_elements(tree, IX, "nonfraction")] == ["first", "second", "local"]


def test_table_cell_locators_under_inline_ancestors_are_directly_executable():
    tree = parse(f"""<html xmlns:ix="{IX}"><body><ix:nonNumeric><ix:continuation>
      <table><tr><td colspan="2">Heading</td></tr><tr><td>Label</td><td>5</td></tr></table>
    </ix:continuation></ix:nonNumeric></body></html>""".encode())
    table = tree.xpath("//table")[0]
    rows, _ = tables.layout_grid(table, tree)
    for row in rows:
        for cell in row:
            assert tree.xpath(cell["source_xpath"]) == [cell["_element"]]
    assert tree.xpath(source_xpath(tree, table)) == [table]


def test_clean_report_locators_under_inline_ancestors_resolve_unchanged_html(tmp_path):
    source = f"""<html xmlns:ix="{IX}"><body><ix:nonNumeric id="block"><ix:continuation>
      <div><p>FORM 10-Q</p><p>PART I</p><p>Item 1. Financial Statements</p>
      <p>Statement text.</p><p>Item 2. Management's Discussion</p><p>Discussion text.</p></div>
    </ix:continuation></ix:nonNumeric></body></html>""".encode()
    path = tmp_path / "filing.htm"
    path.write_bytes(source)
    report = report_cleaning.clean_report(path, form="10-Q")
    original = parse(source)
    assert report.sections
    for section in report.sections:
        locator = section["source_locator"]
        assert len(original.xpath(locator["xpath"])) == 1
        assert "*[name()='ix:continuation']" in locator["xpath"]
    assert path.read_bytes() == source


@pytest.mark.parametrize("manifest_path", [
    ROOT / "source_manifest.json",
    ROOT / "data/raw/sec/0000320193-25-000079/source_manifest.json",
    ROOT / "data/raw/sec/0000320193-26-000020/source_manifest.json",
])
def test_existing_report_and_table_records_preserve_locator_identity(manifest_path):
    if not manifest_path.is_file():
        pytest.skip("Optional locally archived cross-company fixture is unavailable.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = {record["role"]: ROOT / record["local_path"] for record in manifest["files"]}
    arguments = dict(filing_id=UUID(int=1), html_document_id=UUID(int=2), xml_document_id=UUID(int=3))
    current_tables = tables.extract_tables(paths["main_inline_xbrl_filing"], paths["extracted_xbrl_instance"], **arguments)
    current_report = report_cleaning.clean_report(paths["main_inline_xbrl_filing"], form=manifest["form"])
    raw = lambda tree, element: tree.getpath(element)
    with patch.object(tables, "source_xpath", raw), patch.object(tables, "inline_source_xpath", raw):
        previous_tables = tables.extract_tables(paths["main_inline_xbrl_filing"], paths["extracted_xbrl_instance"], **arguments)
    with patch.object(report_cleaning, "source_xpath", raw):
        previous_report = report_cleaning.clean_report(paths["main_inline_xbrl_filing"], form=manifest["form"])
    assert current_tables == previous_tables
    assert current_report == previous_report
