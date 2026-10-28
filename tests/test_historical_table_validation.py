"""Synthetic batch boundaries; no shared DB access or copied production UUIDs."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import psycopg
import pytest

from sec_pipeline.database import DatabaseError
from sec_pipeline import historical_table_validation as batch
from sec_pipeline.table_relations import _occurrence_matches_fact_evidence, _valid_occurrence, fact_evidence
from sec_pipeline.table_sections import canonical
from tests.test_table_relations import relationship_snapshot
from tests.test_v4_table_cells import conflict_fixture


ACCESSION = "0000000001-26-000001"  # synthetic, not a real acceptance filing
MISSING = "0000000001-26-000002"
SET = str(UUID(int=10101))


def inventory():
    return {"filings": [{"ticker": "AAPL", "cik": batch.COMPANIES["AAPL"], "id": str(UUID(int=1)),
            "accession_number": ACCESSION, "form": "10-K", "filing_date": "2026-01-01", "reporting_period_end": "2025-12-31"}],
            "filing_processing_sets": [{"id": SET, "filing_id": str(UUID(int=1)), "validation_status": "passed",
                "table_extraction_version": "tables-v4", "fact_extraction_version": "facts-v1"}], "source_processing_sets": []}


def occurrence_fixture(nil=True):
    table, fact, _ = conflict_fixture(issue=False)
    f = canonical(asdict(fact)) | {"id": str(UUID(int=20))}
    f.update(is_nil=nil, raw_value=None if nil else "24000000", numeric_value=None if nil else "24000000")
    ref = deepcopy(table.structure["rows"][0][1]["fact_references"][0])
    ref.update(fact_evidence(f))
    document_set = str(UUID(int=30))
    evidence = fact_evidence(f) | {"raw_value": None if nil else "24000000", "xml_id": ref["xml_id"],
        "inline_is_nil": nil, "inline_mapped_value": None if nil else "24000000",
        "html_sha256": table.structure["html_sha256"], "xml_sha256": table.structure["xml_sha256"]}
    occurrence = {"filing_id": str(fact.filing_id), "document_set_id": document_set, "fact_id": f["id"],
        "html_document_id": str(table.source_document_id), "inline_id": ref["xml_id"], "source_xpath": ref["inline_xpath"],
        "evidence": evidence}
    return canonical(asdict(table)), f, ref, occurrence, document_set


@pytest.mark.parametrize("raw", [None, ""])
def test_nil_occurrence_accepts_both_existing_contract_representations(raw):
    table, fact, ref, occurrence, document_set = occurrence_fixture()
    occurrence["evidence"]["raw_value"] = raw
    assert _occurrence_matches_fact_evidence(occurrence["evidence"], fact)
    assert _valid_occurrence(occurrence, ref, fact, table, document_set, None)


@pytest.mark.parametrize("field,value", [("raw_value", "0"), ("is_nil", False), ("inline_mapped_value", "0"),
    ("inline_is_nil", False), ("concept", "{wrong}Concept"), ("unit", None), ("period", {"instant": "2000-01-01"})])
def test_nil_fix_does_not_relax_other_evidence(field, value):
    table, fact, ref, occurrence, document_set = occurrence_fixture()
    occurrence["evidence"][field] = value
    assert not _valid_occurrence(occurrence, ref, fact, table, document_set, None)


@pytest.mark.parametrize("field", ["filing_id", "document_set_id", "fact_id", "html_document_id"])
def test_nil_fix_still_rejects_wrong_parent_scope(field):
    table, fact, ref, occurrence, document_set = occurrence_fixture()
    occurrence[field] = str(UUID(int=99))
    assert not _valid_occurrence(occurrence, ref, fact, table, document_set, None)


@pytest.mark.parametrize("value", [None, "", "24000001"])
def test_non_nil_values_remain_exact(value):
    table, fact, ref, occurrence, document_set = occurrence_fixture(nil=False)
    occurrence["evidence"]["raw_value"] = value
    assert not _valid_occurrence(occurrence, ref, fact, table, document_set, None)


def test_union_keeps_not_ingested_discovery_and_never_invents_17_or_years():
    inv = inventory()
    discovery = {"discovery_complete": False, "companies": {"AAPL": {"status": "discovered", "filings": [
        inv["filings"][0], {"cik": batch.COMPANIES["AAPL"], "accession_number": MISSING, "form": "10-Q"}]}}}
    candidates, conflicts, complete = batch.merge_candidates(inv, [("synthetic discovery", discovery)], batch.COMPANIES)
    assert len(candidates) == 2 and not conflicts and not complete
    missing = next(c for c in candidates if c["accession"] == MISSING)
    assert missing["filing_id"] is None
    assert missing["filing_date"] is None
    assert all(c["scope_status"] == "pending_owner_confirmation" for c in candidates)


def test_wrong_cik_and_conflicting_dates_are_not_silently_reconciled():
    inv = inventory()
    other = inv["filings"][0] | {"filing_date": "2026-02-01"}
    wrong = other | {"cik": batch.COMPANIES["CASY"], "accession_number": MISSING}
    candidates, conflicts, _ = batch.merge_candidates(inv, [("bad manifest", [other, wrong])], batch.COMPANIES)
    assert len(candidates) == 1
    assert {c["field"] for c in conflicts} == {"CIK", "filing_date"}


def test_backfill_company_status_does_not_hide_top_level_failed_filings():
    queue = {"companies": {batch.COMPANIES["AAPL"]: {"status": "discovered"}},
        "filings": {MISSING: {"status": "acquisition_failed", "filing": {
            "cik": batch.COMPANIES["AAPL"], "accession_number": MISSING, "form": "10-Q"}}}}
    candidates, _, _ = batch.merge_candidates({}, [("synthetic queue", queue)], batch.COMPANIES)
    assert len(candidates) == 1 and candidates[0]["filing_id"] is None


def test_acquisition_inventory_company_aggregates_do_not_hide_verified_filings():
    exported = {"companies": {"AAPL": {"verified": 1}}, "verified_filings": [
        {"cik": batch.COMPANIES["AAPL"], "accession_number": MISSING, "form": "10-Q"}]}
    candidates, _, _ = batch.merge_candidates({}, [("synthetic acquisition inventory", exported)], batch.COMPANIES)
    assert len(candidates) == 1 and candidates[0]["filing_id"] is None


def test_filters_do_not_turn_other_company_into_missing_filing():
    candidates, _, _ = batch.merge_candidates(inventory(), [], {"CASY": batch.COMPANIES["CASY"]})
    assert candidates == []


def test_processing_candidates_do_not_choose_newest_or_validation_status():
    inv = inventory()
    older = inv["filing_processing_sets"][0] | {"id": str(UUID(int=10102)), "validation_status": "review_required"}
    inv["filing_processing_sets"].append(older)
    assert len(batch.processing_candidates(inv, str(UUID(int=1)))) == 2
    assert batch.processing_candidates(inv, str(UUID(int=99))) == []


@pytest.mark.parametrize("value", ["wrong", f"{ACCESSION}=newest:{SET}", f"{ACCESSION}=legacy:table_key"])
def test_selection_requires_actual_uuid_and_family(value):
    with pytest.raises(DatabaseError):
        batch.parse_selections([value])


def test_cross_filing_override_is_rejected_before_reader(tmp_path):
    with pytest.raises(DatabaseError, match="cross-filing"):
        batch.run_validation(inventory(), [], batch.COMPANIES, [], tmp_path,
            lambda *args: pytest.fail("must not read"), {ACCESSION: {"contract": "legacy", "id": str(UUID(int=99))}})


def test_duplicate_override_is_not_last_wins():
    with pytest.raises(DatabaseError, match="duplicate"):
        batch.parse_selections([f"{ACCESSION}=legacy:{SET}"] * 2)


def test_sources_require_explicit_root_and_hash_check(tmp_path):
    file = tmp_path / "public.htm"
    file.write_bytes(b"synthetic public source")
    snapshot = {"sources": [{"id": "html", "role": "main_inline_xbrl_filing", "local_path": "public.htm",
        "source_url": "https://example.invalid/source", "sha256": "b" * 64}]}
    payloads, availability = batch.available_sources(snapshot, [tmp_path])
    assert payloads["html"] and availability[0]["availability"] == "hash_mismatch"
    snapshot["sources"][0]["local_path"] = "../private.env"
    with pytest.raises(DatabaseError, match="escapes"):
        batch.available_sources(snapshot, [tmp_path])


def test_readonly_guard_refuses_implicit_and_read_write_transactions():
    conn = SimpleNamespace(info=SimpleNamespace(transaction_status=SimpleNamespace(name="IDLE")))
    with pytest.raises(DatabaseError, match="explicit"):
        batch.assert_readonly(conn)
    conn.info.transaction_status.name = "INTRANS"
    conn.execute = lambda query: SimpleNamespace(fetchone=lambda: {"transaction_read_only": "off"})
    with pytest.raises(DatabaseError, match="read-write"):
        batch.assert_readonly(conn)


@pytest.mark.parametrize("text,expected", [("could not translate host name", "DNS"), ("connection refused", "TCP"),
    ("certificate verify failed", "SSL"), ("password authentication failed SECRET", "authentication")])
def test_connection_phases_never_export_exception_text(text, expected):
    result = batch.connection_failure(psycopg.OperationalError(text))
    assert result["failure_stage"] == expected
    assert "SECRET" not in json.dumps(result) and text not in json.dumps(result)


def test_review_cell_is_not_semantic_pass_and_missing_section_is_explicit():
    table, fact, _ = conflict_fixture()
    snapshot = relationship_snapshot(table, [fact])
    for source in snapshot["sources"]:
        source.update(role="synthetic", local_path="missing", source_url="https://example.invalid/source")
    result = batch.audit_candidate(snapshot, [])
    assert result["status"] == "unverified"
    assert any(u["reason"] == "review_required_cell_requires_disposition" for u in result["unverified"])
    assert result["section_mappings"][0]["section_uuid"] is None
    assert result["section_mappings"][0]["mapping_status"].startswith("unverified")


def test_multiple_versions_do_not_double_count_same_cell_link_membership():
    table, fact, _ = conflict_fixture(issue=False)
    snap = relationship_snapshot(table, [fact])
    candidate, = batch.merge_candidates(inventory(), [], batch.COMPANIES)[0]
    check = {"coverage_check": {"status": "unverified"}, "status": "unverified", "source_availability": [{"availability": "missing_local_bytes"}]}
    result = batch.summarize(candidate, [check, check], [snap, snap], [], inventory())
    assert result["cell_count"] == 2 and result["cell_fact_link_count"] == 1
    assert result["unique_membership_count"] == 1 and result["validation_status"] == "blocked"


def test_failed_candidate_read_prevents_filing_source_semantics_pass(tmp_path, monkeypatch):
    import csv
    inv = inventory()
    other_set = str(UUID(int=10102))
    inv["filing_processing_sets"].append(inv["filing_processing_sets"][0] | {"id": other_set})
    table, fact, _ = conflict_fixture(issue=False)
    snapshot = relationship_snapshot(table, [fact])
    snapshot.update(accession=ACCESSION, selection={"contract": "legacy", "id": SET}, chunks=[])
    passed = {"coverage_check": {"status": "passed"}, "status": "passed",
        "relational_checks_status": "passed_stored_evidence_only", "source_availability": [],
        "errors": [], "unverified": [], "trace_examples": [], "section_mappings": []}
    monkeypatch.setattr(batch, "audit_candidate", lambda *args: passed)
    def reader(accession, selection):
        if selection["id"] == other_set:
            raise DatabaseError("Synthetic unavailable candidate")
        return snapshot
    assert batch.run_validation(inv, [], batch.COMPANIES, [], tmp_path, reader) == 2
    with (tmp_path / "validation_summary.csv").open(newline="") as stream:
        row, = csv.DictReader(stream)
    assert row["candidate_checks"] == "1"
    assert row["validation_status"] == "blocked"
    assert row["table_coverage"] == "unverified"
    assert row["database_relationship_status"] == "unverified"
    assert row["original_source_semantics_status"] == "unverified"


@pytest.mark.parametrize("wrong_scope", ["accession", "filing", "processing_set"])
def test_replayed_snapshot_must_match_requested_candidate(tmp_path, monkeypatch, wrong_scope):
    table, fact, _ = conflict_fixture(issue=False)
    snapshot = relationship_snapshot(table, [fact])
    snapshot.update(accession=ACCESSION, selection={"contract": "legacy", "id": SET})
    if wrong_scope == "accession":
        snapshot["accession"] = MISSING
    elif wrong_scope == "filing":
        snapshot["filing"]["id"] = str(UUID(int=999))
    else:
        snapshot["selection"]["id"] = str(UUID(int=999))
    monkeypatch.setattr(batch, "audit_candidate", lambda *args: pytest.fail("Wrong snapshot must not be audited"))
    assert batch.run_validation(inventory(), [], batch.COMPANIES, [], tmp_path, lambda *args: snapshot) == 2
    status = json.loads((tmp_path / "run_status.json").read_text())
    assert status["candidate_sets_checked"] == 0
    assert status["status_counts"] == {"blocked": 1}
    failure = json.loads((tmp_path / ACCESSION / f"legacy--{SET}" / "failure.json").read_text())
    assert failure["error_type"] == "DatabaseError"


def test_batch_keeps_missing_filing_and_unknown_company_coverage_and_stable_csv(tmp_path):
    inv = inventory()
    inv["filing_processing_sets"] = []
    extra = [("discovery fixture", [{"ticker": "AAPL", "accession_number": MISSING, "form": "10-Q"}])]
    output1, output2 = tmp_path / "one", tmp_path / "two"
    for output in (output1, output2):
        assert batch.run_validation(inv, extra, batch.COMPANIES, [], output, lambda *a: pytest.fail("no processing sets")) == 2
    assert (output1 / "validation_summary.csv").read_bytes() == (output2 / "validation_summary.csv").read_bytes()
    import csv
    rows = list(csv.DictReader((output1 / "validation_summary.csv").open()))
    assert {r["validation_status"] for r in rows} == {"unprocessed", "not_ingested"}
    assert all(r["cell_count"] == "" for r in rows)
    coverage = list(csv.DictReader((output1 / "company_coverage.csv").open()))
    assert len(coverage) == 10 and all(r["expected_final_filing_count"] == "" for r in coverage)


def test_prior_sample_is_never_reaudited(tmp_path):
    inv = inventory()
    inv["filings"][0]["accession_number"] = batch.PRIOR_SAMPLE
    assert batch.run_validation(inv, [], batch.COMPANIES, [], tmp_path, lambda *a: pytest.fail("old sample")) == 2
    status = json.loads((tmp_path / "run_status.json").read_text())
    assert status["candidate_sets_checked"] == 0
    assert status["status_counts"] == {"excluded_prior_sample_not_revalidated": 1}


@pytest.mark.parametrize("defect", ["missing_fact", "missing_cell", "wrong_cell_parent", "cross_filing", "cross_source", "wrong_fact_version", "extra_nonfact_link"])
def test_batch_checks_actual_bad_relationships_even_without_original_bytes(defect):
    from sec_pipeline.table_relations import audit_snapshot
    table, fact, _ = conflict_fixture(issue=False)
    snapshot = relationship_snapshot(table, [fact])
    # Accepted v4 fixtures need an exact source occurrence independently of raw
    # source availability. This baseline has none, so compare the added defects.
    if defect == "missing_fact":
        snapshot["facts"] = []
    elif defect == "missing_cell":
        snapshot["table_cells"] = snapshot["table_cells"][:1]
    elif defect == "wrong_cell_parent":
        snapshot["cell_fact_links"][0]["table_id"] = str(UUID(int=99))
    elif defect in {"cross_filing", "cross_source", "wrong_fact_version"}:
        field = {"cross_filing": "filing_id", "cross_source": "source_document_id", "wrong_fact_version": "extraction_version"}[defect]
        snapshot["facts"][0][field] = "facts-other" if field == "extraction_version" else str(UUID(int=99))
    else:
        snapshot["cell_fact_links"].append(snapshot["cell_fact_links"][0] | {"cell_id": snapshot["table_cells"][0]["id"]})
    reasons = {e["reason"] for e in audit_snapshot(snapshot, {})["errors"]}
    assert reasons & {"missing_or_ambiguous_scoped_fact", "cell_inventory_mismatch", "wrong_or_missing_cell_parent",
        "link_crosses_filing_source_or_version", "cell_fact_links_mismatch"}


def test_segment_coverage_allows_more_than_three_physical_tables(monkeypatch):
    source = b'<html><body><table id="a"/><table id="b"/><table id="c"/><table id="d"/></body></html>'
    kinds = ["income_statement", "balance_sheet", "cash_flow", "cash_flow"]
    snapshot = {"tables": [{"source_document_id": "html", "statement_type": kind,
        "structure": {"source_xpath": f"/html/body/table[{n+1}]", "fact_scope": {"xml_document_id": "xml"}}}
        for n, kind in enumerate(kinds)], "facts": []}
    def segments(tree, facts):
        return [SimpleNamespace(statement_type=kind, element=element) for kind, element in zip(kinds, tree.xpath('//table'))]
    monkeypatch.setattr(batch, "select_statement_segments", segments)
    assert batch.coverage_check(snapshot, {"html": source, "xml": b"synthetic-placeholder"})["status"] == "passed"
    snapshot["tables"].pop()
    assert batch.coverage_check(snapshot, {"html": source, "xml": b"synthetic-placeholder"})["status"] == "failed"


def requirement():
    return {"ticker": "AAPL", "accession": ACCESSION, "source_document_id": "synthetic-html",
        "source_url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/synthetic.htm",
        "local_path": "data/raw/sec/" + ACCESSION + "/synthetic.htm", "SHA-256": "a" * 64,
        "file_type": "main_inline_xbrl_filing", "minimum_relationship_source": True, "hash_status": "missing"}


def test_missing_contact_identity_stops_before_http_and_does_not_log_identity(tmp_path, monkeypatch):
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
    status = batch.download_required_sources([requirement()], tmp_path, identity_approved=True)
    assert status["status"] == "blocked_SEC_contact_identity_missing_or_invalid"
    assert status["HTTP_requests"] == 0 and status["files"] == []


def test_download_requires_approval_even_if_identity_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGAR_IDENTITY", "Synthetic Contact synthetic@example.org")
    status = batch.download_required_sources([requirement()], tmp_path, identity_approved=False)
    assert status["status"] == "blocked_identity_authorization_required" and not status["files"]


def test_download_preserves_existing_bytes_and_different_hash_evidence(tmp_path, monkeypatch):
    from sec_pipeline import acquisition
    class Client:
        request_log = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url):
            data = b"different synthetic public source"
            import hashlib
            return data, {"sha256": hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(acquisition, "SecClient", lambda **kw: Client())
    row = requirement()
    status = batch.download_required_sources([row], tmp_path, identity_approved=True)
    file, = status["files"]
    assert file["hash_matches"] is False
    assert not (tmp_path / row["local_path"]).exists()
    assert "hash-differences" in file["saved_path"] and Path(file["saved_path"]).is_file()
    target = tmp_path / row["local_path"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous bytes must survive")
    status = batch.download_required_sources([row], tmp_path, identity_approved=True)
    assert status["files"][0]["status"] == "existing_bytes_retained"
    assert target.read_bytes() == b"previous bytes must survive"


def test_interrupted_source_download_leaves_no_original_and_can_resume(tmp_path, monkeypatch):
    import hashlib
    from sec_pipeline import acquisition
    data = b"complete synthetic original source"
    class Client:
        request_log = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url): return data, {"sha256": hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(acquisition, "SecClient", lambda **kw: Client())
    row = requirement() | {"SHA-256": hashlib.sha256(data).hexdigest()}
    target = tmp_path / row["local_path"]
    original_open = Path.open
    original_temporary = acquisition.tempfile.NamedTemporaryFile
    class InterruptedWrite:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): return self.handle.__exit__(*args)
        def __getattr__(self, name): return getattr(self.handle, name)
        def write(self, value):
            self.handle.write(value[:3])
            raise OSError("Synthetic interrupted source write")
    def open_interrupted(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        return InterruptedWrite(handle) if args and args[0] == "xb" else handle
    with monkeypatch.context() as interrupted:
        interrupted.setattr(Path, "open", open_interrupted)
        interrupted.setattr(acquisition.tempfile, "NamedTemporaryFile",
            lambda *args, **kwargs: InterruptedWrite(original_temporary(*args, **kwargs)))
        status = batch.download_required_sources([row], tmp_path, identity_approved=True)
    assert status["files"][0]["status"] == "unavailable"
    assert not target.exists()
    assert not list(target.parent.glob("*.part"))
    resumed = batch.download_required_sources([row], tmp_path, identity_approved=True)
    assert resumed["files"][0]["status"] == "downloaded_hash_matched"
    assert target.read_bytes() == data


@pytest.mark.parametrize("raw,numeric,nil", [("10", "11", False), (None, "0", True), ("Infinity", "Infinity", False)])
def test_stored_raw_numeric_mismatch_is_not_hidden_by_missing_sources(raw, numeric, nil):
    table, fact, _ = conflict_fixture(issue=False)
    snapshot = relationship_snapshot(table, [fact])
    for source in snapshot["sources"]:
        source.update(role="synthetic", local_path="missing", source_url="https://example.invalid/source")
    snapshot["facts"][0].update(raw_value=raw, numeric_value=numeric, is_nil=nil)
    assert "stored_fact_raw_numeric_inconsistency" in {e["reason"] for e in batch.audit_candidate(snapshot, [])["errors"]}


def test_stored_multi_fact_cell_is_not_collapsed_to_one_membership():
    from dataclasses import replace
    from sec_pipeline.table_relations import audit_snapshot
    table, fact, _ = conflict_fixture(issue=False)
    table = replace(table, extraction_version="tables-v3", structure=deepcopy(table.structure))
    table.structure["schema_version"] = 2
    other = replace(fact, occurrence_key="F2", concept_name="OtherSyntheticMetric",
                    source_reference=fact.source_reference | {"element_id": "F2"})
    ref = deepcopy(table.structure["rows"][0][1]["fact_references"][0])
    ref.update(xml_id="F2", concept="{" + other.concept_namespace + "}" + other.concept_name)
    table.structure["rows"][0][1]["fact_references"].append(ref)
    snapshot = relationship_snapshot(table, [fact, other])
    assert len(snapshot["cell_fact_links"]) == len(snapshot["financial_table_facts"]) == 2
    assert audit_snapshot(snapshot, {})["errors"] == []


def test_two_stored_cells_may_reuse_one_fact_without_losing_a_link():
    from dataclasses import replace
    from sec_pipeline.table_relations import audit_snapshot
    table, fact, _ = conflict_fixture(issue=False)
    table = replace(table, extraction_version="tables-v3", structure=deepcopy(table.structure))
    table.structure["schema_version"] = 2
    repeated = deepcopy(table.structure["rows"][0][1])
    repeated.update(cell_id="r000c002", column=2, source_xpath="/html/body/table/tr/td[3]")
    ref = repeated["fact_references"][0]
    ref.update(method="same_row_semantic_repeat", repeated_from_inline_xpath=ref["inline_xpath"])
    table.structure["rows"][0].append(repeated)
    table.structure["grid"][0].append(repeated["cell_id"])
    snapshot = relationship_snapshot(table, [fact])
    assert len(snapshot["cell_fact_links"]) == 2 and len(snapshot["financial_table_facts"]) == 1
    assert audit_snapshot(snapshot, {})["errors"] == []
