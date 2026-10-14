import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest

from sec_pipeline import company_tickers as tickers
from sec_pipeline import database


def fixture_source():
    raw = json.dumps({"fields": ["cik", "name", "ticker", "exchange"], "data": [
        [1, "First", "ONE", "NYSE"], [1, "First", "ONE-PA", "NYSE"],
        [2, "Classes", "TWO-B", "NYSE"], [2, "Classes", "TWO-A", "NYSE"],
    ]}).encode()
    return raw, {"source_url": tickers.SOURCE_URL, "sha256": hashlib.sha256(raw).hexdigest(), "retrieved_at_utc": "2026-10-13T19:16:13Z"}


def test_sec_symbols_are_grouped_by_cik_without_losing_share_classes():
    raw, receipt = fixture_source()
    reference = tickers.build_reference(raw, receipt, preferred={"0000000002": ["TWO.A"]})
    assert reference["companies"]["0000000001"]["ticker"] == "ONE"
    assert reference["companies"]["0000000002"]["ticker"] == "TWO-A"
    assert len(reference["companies"]["0000000002"]["symbols"]) == 2


def test_index_cannot_invent_a_symbol_not_in_sec_mapping():
    raw, receipt = fixture_source()
    assert tickers.build_reference(raw, receipt, preferred={"0000000001": ["FAKE"]})["companies"]["0000000001"]["ticker"] == "ONE"


@pytest.mark.parametrize("changed", [{"source_url": "https://example.invalid/tickers"}, {"sha256": "0" * 64}, {"retrieved_at_utc": None}])
def test_invalid_source_receipt_blocks_reference(changed):
    raw, receipt = fixture_source()
    with pytest.raises(ValueError):
        tickers.build_reference(raw, receipt | changed)


def test_optional_cache_missing_preserves_existing_import_support(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_TICKER_REFERENCE", str(tmp_path / "missing.json"))
    assert tickers.current_ticker("0000000001") is None


def test_cache_refresh_is_visible_without_network_calls(tmp_path, monkeypatch):
    path = tmp_path / "tickers.json"
    monkeypatch.setenv("SEC_TICKER_REFERENCE", str(path))
    raw, receipt = fixture_source()
    reference = tickers.build_reference(raw, receipt)
    tickers.write_reference(path, reference)
    assert tickers.current_ticker("0000000001") == "ONE"
    reference["companies"]["0000000001"]["ticker"] = "ONE-PA"
    tickers.write_reference(path, reference)
    assert tickers.current_ticker("0000000001") == "ONE-PA"
    assert tickers.current_ticker("0000000999") is None


@pytest.mark.parametrize("supplied, expected", [(None, "ONE"), ("EXPLICIT", "EXPLICIT")])
def test_company_registration_enriches_only_unsupplied_symbol(supplied, expected):
    calls = []
    conn = SimpleNamespace(autocommit=False, execute=lambda sql, params: (calls.append((sql, params)) or SimpleNamespace(fetchone=lambda: (UUID(int=1),))))
    with patch.object(tickers, "current_ticker", return_value="ONE") as lookup:
        database.register_company(conn, database.CompanyInput(cik="0000000001", name="Original name", ticker=supplied))
    assert calls[0][1] == ("0000000001", "Original name", expected)
    assert "ON CONFLICT (cik) DO NOTHING" in calls[0][0]
    assert lookup.call_count == (1 if supplied is None else 0)


def test_existing_company_backfill_preserves_populated_tickers_and_other_fields():
    raw, receipt = fixture_source()
    reference = tickers.build_reference(raw, receipt)
    calls = []
    def execute(sql, params=None):
        calls.append((sql, params))
        if sql.startswith("SELECT"):
            return SimpleNamespace(fetchall=lambda: [("0000000001", "Original name", None), ("0000000002", "Preserved", "CUSTOM"), ("0000000003", "Unlisted subsidiary", None)])
        assert "SET ticker=m.ticker" in sql and "c.ticker IS NULL" in sql
        assert params == (["0000000001"], ["ONE"])
        return SimpleNamespace(fetchall=lambda: [("0000000001", "ONE")])
    result = tickers.fill_missing_tickers(SimpleNamespace(autocommit=False, execute=execute), reference)
    assert result["updated"] == 1
    assert result["unmatched"] == [{"cik": "0000000003", "name": "Unlisted subsidiary"}]
    assert len(calls) == 2


def test_ticker_update_rejects_autocommit():
    with pytest.raises(ValueError):
        tickers.fill_missing_tickers(SimpleNamespace(autocommit=True), {})


def test_unsubstantiated_cached_symbol_is_rejected(tmp_path):
    raw, receipt = fixture_source()
    reference = tickers.build_reference(raw, receipt)
    reference["companies"]["0000000001"]["ticker"] = "INVENTED"
    path = tmp_path / "tickers.json"
    tickers.write_reference(path, reference)
    with pytest.raises(ValueError):
        tickers.load_reference(path)
