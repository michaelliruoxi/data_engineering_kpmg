"""Corrected output uses new identities and independent original-heading evidence."""

from dataclasses import replace
from copy import deepcopy
import json
from pathlib import Path
import shutil

import httpx
import pytest

from sec_pipeline.acquisition import SecClient
from sec_pipeline.document_sets import download_document_set
from sec_pipeline.extraction_profiles import (
    CORRECTED_PROFILE, LEGACY_PROFILE, corrected_tables, supported_versions, versions_for,
)
from sec_pipeline import filing_parser_v4 as parser
from sec_pipeline import filing_ingestion_v5 as corrected
from sec_pipeline.v4_table_cells import v4_relational_cells
from tests.test_v4_table_cells import conflict_fixture

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def original_sample_set(tmp_path_factory):
    root = tmp_path_factory.mktemp("original-msft-corrected")
    manifest = json.loads((ROOT / "source_manifest.json").read_text())
    for record in [manifest["index_document"], *manifest["files"]]:
        target = root / record["local_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / record["local_path"], target)
    core = root / "source_manifest.json"
    core.write_text(json.dumps(manifest))

    def forbidden(request):
        raise AssertionError("Original-source fixture attempted network access.")

    with SecClient("Unit Test unit@example.test", transport=httpx.MockTransport(forbidden)) as client:
        path = download_document_set(client, core, root)
    return path, root


@pytest.fixture(scope="module")
def corrected_sample(original_sample_set):
    path, root = original_sample_set
    return parser.parse_filing_set(path, root, profile=CORRECTED_PROFILE)


def test_original_sample_has_versioned_source_exact_outputs(corrected_sample):
    item = corrected_sample
    assert item.versions == corrected.VERSIONS
    assert len(item.facts) == len(item.occurrences) == 1622
    assert len(item.tables) == 3
    assert item.semantic_checks == {"period_header_consistency": True, "primary_section_boundaries": True}
    assert all(check is True for check in item.checks.values())
    assert {table.extraction_version for table in item.tables} == {"tables-v5"}
    assert {document.report.extraction_version for document in item.documents} == {"report-v2"}
    assert all(chunk.text == document.report.full_text[chunk.offset_start:chunk.offset_end]
               for document in item.documents for chunk in document.chunks)
    assert all(chunk.chunking_version == "paragraph-v1" for document in item.documents for chunk in document.chunks)


def test_profile_cannot_be_swapped_after_validation(corrected_sample, original_sample_set):
    path, root = original_sample_set
    with pytest.raises(parser.FilingSetParseError, match="profile differs"):
        parser.validate_filing_set(corrected_sample, path, root)
    with pytest.raises(parser.FilingSetParseError, match="profile differs"):
        parser.validate_filing_set(replace(corrected_sample, profile=LEGACY_PROFILE),
                                   path, root, profile=CORRECTED_PROFILE)
    with pytest.raises(ValueError, match="Unsupported"):
        versions_for("invented")
    assert not supported_versions(corrected.VERSIONS | {"reports": "invented"})


def test_original_heading_inventory_is_required(original_sample_set, monkeypatch):
    from sec_pipeline import report_validation
    path, root = original_sample_set
    monkeypatch.setattr(report_validation, "source_heading_expectations",
                        lambda *args, **kwargs: {"expected_section_ids": None})
    with pytest.raises(parser.FilingSetParseError, match="independently verified"):
        parser.parse_filing_set(path, root, profile=CORRECTED_PROFILE)


def test_missing_actual_heading_cannot_pass_on_coverage_alone(original_sample_set, monkeypatch):
    original = parser.clean_report

    def incomplete(*args, **kwargs):
        report = original(*args, **kwargs)
        sections = deepcopy(report.sections)
        index = next(i for i, section in enumerate(sections) if section["section_id"] == "part-ii-item-6")
        sections[index - 1]["offset_end"] = sections[index]["offset_end"]
        sections.pop(index)
        return replace(report, sections=sections)

    monkeypatch.setattr(parser, "clean_report", incomplete)
    with pytest.raises(parser.FilingSetParseError, match="section_inventory_mismatch"):
        parser.parse_filing_set(*original_sample_set, profile=CORRECTED_PROFILE)


def test_quality_issues_get_distinct_versioned_keys_without_altering_evidence():
    table, _, _ = conflict_fixture()
    before = deepcopy(table)
    updated, = corrected_tables([table])
    old = table.structure["rows"][0][1]
    new = updated.structure["rows"][0][1]
    assert table == before
    assert new["quality_issue_keys"] == ["tables-v5:" + key for key in old["quality_issue_keys"]]
    assert new["quality_issues"][0] | {"issue_key": old["quality_issues"][0]["issue_key"]} == old["quality_issues"][0]
    assert new["source_fact_references"] == old["source_fact_references"]
    assert v4_relational_cells(table.filing_id, updated)


def test_backfill_dispatch_and_receipt_are_version_specific(original_sample_set):
    from sec_pipeline.backfill import adapter_profile
    profile, key = adapter_profile("tables-v5")
    assert profile is corrected
    assert key == "automated-v5"
    result = corrected.run_dry_run(*original_sample_set)
    assert result["versions"] == corrected.VERSIONS
    assert result["configuration"]["extraction_profile"] == CORRECTED_PROFILE
    assert result["configuration"]["section_validation_policy"] == "original-heading-v1"
    assert result["preferred"] is False
