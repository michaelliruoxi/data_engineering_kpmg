"""Offline S&P 500 security-to-issuer mapping and dated snapshot integrity."""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from sec_pipeline.universe import SOURCE_URL, fetch_snapshot, load_snapshot, parse_constituents, snapshot


def table(rows):
    return ("<html><table id='constituents'><tr><th>Symbol</th><th>Security</th><th>Sector</th><th>CIK</th></tr>"
            + "".join(f"<tr><td>{ticker}</td><td>{name}</td><td>Example</td><td>{cik}</td></tr>"
                      for ticker, name, cik in rows)
            + "</table></html>").encode("utf-8")


def complete_source():
    rows = [(f"T{number:04d}", f"Company {number}", str(number)) for number in range(1, 501)]
    rows += [(f"T{number:04d}.B", f"Company {number} class B", str(number)) for number in range(1, 4)]
    return table(rows)


def save_snapshot(root, *, as_of="2026-10-13"):
    data = complete_source()
    result = snapshot(data, as_of=as_of)
    path = root / "membership.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    path.with_suffix(".html").write_bytes(data)
    return path, result, data


class ConstituentParsingTests(unittest.TestCase):
    def test_503_security_rows_deduplicate_to_500_cik_issuers(self):
        data = complete_source()
        companies = parse_constituents(data)
        self.assertEqual(len(companies), 500)
        self.assertEqual(sum(len(company["tickers"]) for company in companies), 503)
        self.assertEqual(companies[0], {"cik": "0000000001", "name": "Company 1", "tickers": ["T0001", "T0001.B"]})
        self.assertEqual(companies[-1]["cik"], "0000000500")
        self.assertEqual(len({company["cik"] for company in companies}), 500)

    def test_ciks_normalize_and_companies_sort_independently_of_table_order(self):
        companies = parse_constituents(table([("B", "Second", "2"), ("A", "First", "0000000001"),
                                              ("B.B", "Second class B", "0000000002")]))
        self.assertEqual([company["cik"] for company in companies], ["0000000001", "0000000002"])
        self.assertEqual(companies[1]["tickers"], ["B", "B.B"])

    def test_invalid_ciks_and_duplicate_symbols_are_rejected(self):
        for cik in ("", "0", "-1", "abc", "12345678901"):
            with self.subTest(cik=cik), self.assertRaises(ValueError):
                parse_constituents(table([("A", "First", cik)]))
        for rows in ([('', 'First', '1')], [('A', 'First', '1'), ('A', 'Second', '2')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_constituents(table(rows))

    def test_missing_ambiguous_and_changed_table_shapes_are_rejected(self):
        valid = table([("A", "First", "1")])
        variants = (b"<html>No table</html>", valid + valid,
                    valid.replace(b"<th>CIK</th>", b"<th>Identifier</th>"),
                    valid.replace(b"<td>Example</td>", b""))
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_constituents(data)


class MembershipSnapshotTests(unittest.TestCase):
    def test_snapshot_records_source_counts_hash_and_explicit_date(self):
        data = complete_source()
        result = snapshot(data, as_of="2026-10-13")
        self.assertEqual(result["source_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(result["company_count"], 500)
        self.assertEqual(result["security_count"], 503)
        self.assertEqual(result["as_of"], "2026-10-13")
        self.assertEqual(result["source_url"], SOURCE_URL)
        self.assertIn("community", result["source_kind"])
        self.assertIsNotNone(datetime.fromisoformat(result["retrieved_at_utc"]).tzinfo)

    def test_partial_source_cannot_be_published_as_full_universe(self):
        with self.assertRaisesRegex(ValueError, "issuer count"):
            snapshot(table([("A", "First", "1")]), as_of="2026-10-13")

    def test_invalid_snapshot_date_is_rejected(self):
        for as_of in ("2026-02-30", "not-a-date", "2026/10/13"):
            with self.subTest(as_of=as_of), self.assertRaises(ValueError):
                snapshot(complete_source(), as_of=as_of)

    def test_saved_snapshot_load_is_independent_of_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, result, data = save_snapshot(Path(temporary))
            with patch("sec_pipeline.universe.httpx.get", side_effect=AssertionError("Unexpected HTTP request")):
                self.assertEqual(load_snapshot(path), result)
                self.assertEqual(fetch_snapshot(path, as_of="2026-10-13"), result)
            self.assertEqual(path.with_suffix(".html").read_bytes(), data)

    def test_source_byte_tamper_is_rejected_even_when_html_still_parses(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _, data = save_snapshot(Path(temporary))
            path.with_suffix(".html").write_bytes(data + b"<!-- changed source -->")
            with self.assertRaisesRegex(ValueError, "hash changed"):
                load_snapshot(path)

    def test_derived_company_and_count_tamper_are_rejected(self):
        for change in ("company", "company_count", "security_count", "as_of", "schema", "universe"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                path, result, _ = save_snapshot(Path(temporary))
                changed = deepcopy(result)
                if change == "company":
                    changed["companies"][0]["tickers"] = ["FAKE"]
                elif change in {"company_count", "security_count"}:
                    changed[change] += 1
                elif change == "as_of":
                    changed["as_of"] = "2026-02-30"
                elif change == "schema":
                    changed["schema_version"] = 2
                else:
                    changed["universe"] = "different-index"
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_snapshot(path)

    def test_cached_old_snapshot_preserves_date_and_cannot_claim_a_new_date(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, result, _ = save_snapshot(Path(temporary), as_of="2021-10-13")
            with patch("sec_pipeline.universe.httpx.get", side_effect=AssertionError("Unexpected HTTP request")):
                self.assertEqual(load_snapshot(path)["as_of"], "2021-10-13")
                with self.assertRaisesRegex(ValueError, "date|as.of|stale"):
                    fetch_snapshot(path, as_of="2026-10-13")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), result)

    def test_fetch_archives_the_exact_response_and_loads_it_back(self):
        data = complete_source()
        response = SimpleNamespace(content=data, raise_for_status=lambda: None)
        with tempfile.TemporaryDirectory() as temporary, patch("sec_pipeline.universe.httpx.get", return_value=response) as get:
            path = Path(temporary) / "membership.json"
            result = fetch_snapshot(path, as_of="2026-10-13")
            self.assertEqual(get.call_count, 1)
            self.assertEqual(path.with_suffix(".html").read_bytes(), data)
            self.assertEqual(load_snapshot(path), result)


if __name__ == "__main__":
    unittest.main()
