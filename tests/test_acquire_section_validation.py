"""Offline integration checks for the original-source corpus acquisition CLI."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sec_pipeline.acquisition import FilingIdentity


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("acquire_section_validation", ROOT / "scripts/acquire_section_validation.py")
acquisition = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acquisition)


def filing(number, *, cik="0000320193", form="10-Q", date="2025-05-01"):
    return FilingIdentity(cik, "Example issuer", f"{cik}-25-{number:06d}", form,
                          date, "2025-03-31", f"filing{number}.htm")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    config = tmp_path / "companies.json"
    config.write_text(json.dumps({"companies": [{"ticker": "AAPL", "cik": "0000320193"}]}))
    run = SimpleNamespace(args=SimpleNamespace(config=config, output=tmp_path / "corpus",
                                              since="2021-01-01", until="2026-12-31"),
                          rows=[filing(1), filing(2, form="10-K")], requests=[], fail_url=None)

    class OfflineClient:
        def __init__(self, output):
            self.request_log = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def record(self, url):
            run.requests.append(url)
            if url == run.fail_url:
                self.request_log.append({"source_url": url, "error": "RuntimeError"})
                raise RuntimeError("private runtime identity must not enter artifacts")

        def get_json(self, url):
            self.record(url)
            cik = url.rsplit("CIK", 1)[-1].removesuffix(".json")
            rows = [row for row in run.rows if row.cik == cik]
            columns = {key: [getattr(row, attribute) for row in rows] for key, attribute in (
                ("accessionNumber", "accession_number"), ("form", "form"),
                ("filingDate", "filing_date"), ("reportDate", "reporting_period_end"),
                ("primaryDocument", "primary_document"))}
            self.request_log.append({"source_url": url, "http_status": 200})
            return {"cik": int(cik), "name": "Example issuer", "filings": {"recent": columns, "files": []}}

        def get(self, url):
            self.record(url)
            data = f"<html><body>Original filing {url.rsplit('/', 1)[-1]}</body></html>".encode()
            metadata = {"http_status": 200, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
            self.request_log.append({"source_url": url, **metadata})
            return data, metadata

    monkeypatch.setattr(acquisition, "RecordedClient", OfflineClient)
    return run


def saved(run):
    return json.loads((run.args.output / "corpus_manifest.json").read_text())


def test_acquires_inclusive_2021_2026_scope_and_excludes_amendments(harness):
    harness.rows = [filing(1, date="2020-12-31"), filing(2, date="2021-01-01"),
                    filing(3, date="2026-12-31", form="10-K"), filing(4, date="2027-01-01"),
                    SimpleNamespace(**(vars(filing(5)) | {"form": "10-K/A"}))]
    result = acquisition.acquire(harness.args)
    assert [row["accession_number"] for row in result["filings"]] == [harness.rows[1].accession_number, harness.rows[2].accession_number]
    assert result["filing_date_from"] == "2021-01-01"
    assert result["filing_date_to"] == "2026-12-31"
    assert result["discovery_complete"] is True
    assert result["coverage_status"] == "acquired"
    assert result["companies"]["AAPL"]["discovered_count"] == 2
    for row in result["filings"]:
        data = (harness.args.output / row["html_path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["sha256"]


def test_cached_resume_uses_verified_bytes_and_preserves_request_receipts(harness):
    first = acquisition.acquire(harness.args)
    requests = list(harness.requests)
    log_path = harness.args.output / "request_log.json"
    original_log = log_path.read_bytes()
    second = acquisition.acquire(harness.args)
    assert harness.requests == requests
    assert second["filings"] == first["filings"]
    assert log_path.read_bytes() == original_log


def test_corrupted_cached_source_is_not_overwritten_or_marked_complete(harness):
    first = acquisition.acquire(harness.args)
    source = harness.args.output / first["filings"][0]["html_path"]
    source.write_bytes(b"corrupted original")
    requests = list(harness.requests)
    with pytest.raises(ValueError, match="hash mismatch"):
        acquisition.acquire(harness.args)
    assert source.read_bytes() == b"corrupted original"
    assert harness.requests == requests
    assert saved(harness)["coverage_status"] == "incomplete"


def test_download_failure_retains_complete_inventory_for_resumption(harness):
    harness.fail_url = harness.rows[1].archive_url + harness.rows[1].primary_document
    with pytest.raises(RuntimeError):
        acquisition.acquire(harness.args)
    failed = saved(harness)
    assert len(failed["filings"]) == 2
    assert failed["filings"][0]["acquisition_status"] == "available"
    assert "html_path" not in failed["filings"][1]
    assert failed["coverage_status"] == "incomplete"
    assert failed["discovery_complete"] is False
    assert failed["acquisition_blocker"] == "RuntimeError"
    assert all(b"private runtime identity" not in path.read_bytes() for path in harness.args.output.rglob("*") if path.is_file())
    attempted = len(harness.requests)
    harness.fail_url = None
    resumed = acquisition.acquire(harness.args)
    assert harness.requests[attempted:] == [harness.rows[1].archive_url + harness.rows[1].primary_document]
    assert resumed["coverage_status"] == "acquired"
    assert resumed["discovery_complete"] is True
    assert "acquisition_blocker" not in resumed


def test_discovery_failure_does_not_claim_company_coverage_and_can_resume(harness):
    harness.args.config.write_text(json.dumps({"companies": [
        {"ticker": "AAPL", "cik": "0000320193"}, {"ticker": "MSFT", "cik": "0000789019"}]}))
    harness.rows.append(filing(3, cik="0000789019"))
    harness.fail_url = "https://data.sec.gov/submissions/CIK0000789019.json"
    with pytest.raises(RuntimeError):
        acquisition.acquire(harness.args)
    failed = saved(harness)
    assert failed["discovery_complete"] is False
    assert failed["companies"]["AAPL"]["discovery_status"] == "complete"
    assert failed["companies"]["MSFT"].get("discovery_status") != "complete"
    attempted = len(harness.requests)
    harness.fail_url = None
    resumed = acquisition.acquire(harness.args)
    assert resumed["discovery_complete"] is True
    assert resumed["companies"]["MSFT"]["available_count"] == 1
    assert all("320193" not in url for url in harness.requests[attempted:])


@pytest.mark.parametrize("change", ["date", "ticker", "cik"])
def test_changed_scope_cannot_reuse_saved_discovery(harness, change):
    acquisition.acquire(harness.args)
    manifest_path = harness.args.output / "corpus_manifest.json"
    original = manifest_path.read_bytes()
    requests = list(harness.requests)
    if change == "date":
        harness.args.since = "2022-01-01"
    else:
        company = {"ticker": "MSFT" if change == "ticker" else "AAPL",
                   "cik": "0000789019" if change == "cik" else "0000320193"}
        harness.args.config.write_text(json.dumps({"companies": [company]}))
    with pytest.raises(ValueError, match="changed|differs"):
        acquisition.acquire(harness.args)
    assert manifest_path.read_bytes() == original
    assert harness.requests == requests


def test_saved_complete_discovery_with_missing_inventory_rows_is_rejected(harness):
    acquisition.acquire(harness.args)
    state = saved(harness)
    state["filings"].pop()
    acquisition.save(harness.args.output / "corpus_manifest.json", state)
    requests = list(harness.requests)
    with pytest.raises(ValueError, match="inventory differs"):
        acquisition.acquire(harness.args)
    assert saved(harness)["coverage_status"] == "incomplete"
    assert saved(harness)["discovery_complete"] is False
    assert harness.requests == requests
