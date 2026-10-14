"""Dated S&P 500 membership snapshots, deduplicated by SEC issuer CIK."""

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import httpx
from lxml import html


SOURCE_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def parse_constituents(data):
    tree = html.fromstring(data)
    tables = tree.xpath("//table[@id='constituents']")
    if len(tables) != 1:
        raise ValueError("Expected one S&P 500 constituents table.")
    headers = [" ".join(cell.text_content().split()) for cell in tables[0].xpath(".//tr[1]/th")]
    required = {"Symbol", "Security", "CIK"}
    if not required.issubset(headers):
        raise ValueError("Constituent table is missing symbol, name, or CIK.")
    companies, symbols = {}, set()
    for row in tables[0].xpath(".//tr[td]"):
        values = [" ".join(cell.text_content().split()) for cell in row.xpath("./td")]
        if len(values) != len(headers):
            raise ValueError("Constituent row has an unexpected width.")
        fields = dict(zip(headers, values))
        cik, symbol = fields["CIK"], fields["Symbol"]
        if not re.fullmatch(r"\d{1,10}", cik) or int(cik) == 0 or not symbol or symbol in symbols:
            raise ValueError("Constituent CIK or ticker is invalid or duplicated.")
        cik = cik.zfill(10)
        symbols.add(symbol)
        company = companies.setdefault(cik, {"cik": cik, "name": fields["Security"], "tickers": []})
        company["tickers"].append(symbol)
    return sorted(companies.values(), key=lambda company: company["cik"])


def snapshot(data, *, source_url=SOURCE_URL, as_of=None):
    if as_of is not None and date.fromisoformat(as_of).isoformat() != as_of:
        raise ValueError("Invalid membership date.")
    companies = parse_constituents(data)
    # Share classes produce more securities than unique issuers. Unexpected
    # issuer counts require investigation rather than a silently partial run.
    if not 490 <= len(companies) <= 510:
        raise ValueError("Unexpected S&P 500 issuer count; verify the membership source.")
    return {
        "schema_version": 1, "universe": "current-sp500", "source_url": source_url,
        "source_kind": "community-maintained constituent list",
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of or datetime.now(timezone.utc).date().isoformat(),
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "companies": companies,
        "company_count": len(companies),
        "security_count": sum(len(c["tickers"]) for c in companies),
    }


def fetch_snapshot(path, *, as_of=None):
    path = Path(path)
    if path.exists():
        saved = load_snapshot(path)
        if as_of is not None and saved["as_of"] != as_of:
            raise ValueError("Existing membership snapshot has a different date; use a new snapshot path.")
        return saved
    response = httpx.get(SOURCE_URL, headers={"User-Agent": "sec-pipeline/0.1 (constituent research)"}, timeout=30, follow_redirects=True)
    response.raise_for_status()
    result = snapshot(response.content, as_of=as_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".html").write_bytes(response.content)
    with path.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    return result


def load_snapshot(path):
    path = Path(path)
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("schema_version") != 1 or result.get("universe") != "current-sp500":
        raise ValueError("Unsupported membership snapshot.")
    source = path.with_suffix(".html").read_bytes()
    if hashlib.sha256(source).hexdigest() != result.get("source_sha256"):
        raise ValueError("Membership source hash changed.")
    if parse_constituents(source) != result.get("companies"):
        raise ValueError("Membership snapshot differs from its source.")
    if not 490 <= len(result["companies"]) <= 510 or result.get("company_count") != len(result["companies"]):
        raise ValueError("Unexpected membership count.")
    if result.get("security_count") != sum(len(c["tickers"]) for c in result["companies"]):
        raise ValueError("Unexpected security count.")
    if date.fromisoformat(result["as_of"]).isoformat() != result["as_of"]:
        raise ValueError("Invalid membership date.")
    return result
