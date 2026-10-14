"""Current SEC ticker metadata, kept separate from historical filing identity."""

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


SOURCE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
DEFAULT_REFERENCE = Path(__file__).resolve().parents[2] / "data/reference/company_tickers.json"


def build_reference(source, receipt, *, preferred=None):
    """Retain every SEC symbol; choose one display symbol for companies.ticker.

    For index members, prefer the first index share class when it also appears
    in SEC data. Otherwise preserve SEC's first-listed symbol. This is display
    metadata, not a claim that the selected security is ordinary common stock.
    """
    if receipt.get("source_url") != SOURCE_URL or receipt.get("sha256") != hashlib.sha256(source).hexdigest():
        raise ValueError("Ticker source URL or hash does not match its receipt.")
    if not receipt.get("retrieved_at_utc"):
        raise ValueError("Ticker retrieval time is required.")
    payload = json.loads(source)
    fields = payload.get("fields")
    if not isinstance(fields, list) or len(set(fields)) != len(fields) or not {"cik", "name", "ticker", "exchange"}.issubset(fields):
        raise ValueError("Unexpected SEC ticker columns.")
    records = {}
    for row in payload.get("data", []):
        if not isinstance(row, list) or len(row) != len(fields):
            raise ValueError("Invalid SEC ticker row.")
        item = dict(zip(fields, row))
        cik, symbol = str(item["cik"]), item["ticker"]
        if not re.fullmatch(r"[0-9]{1,10}", cik) or int(cik) == 0:
            raise ValueError("Invalid SEC ticker CIK.")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,31}", symbol):
            raise ValueError("Invalid SEC trading symbol.")
        cik = cik.zfill(10)
        company = records.setdefault(cik, {"symbols": [], "selection": "first_sec_symbol"})
        evidence = {"ticker": symbol, "exchange": item["exchange"]}
        if evidence not in company["symbols"]:
            company["symbols"].append(evidence)
    if not records:
        raise ValueError("SEC ticker source is empty.")
    for cik, record in records.items():
        record["ticker"] = record["symbols"][0]["ticker"]
        aliases = {entry["ticker"].replace(".", "-"): entry["ticker"] for entry in record["symbols"]}
        for symbol in (preferred or {}).get(cik, []):
            if symbol.replace(".", "-") in aliases:
                record["ticker"] = aliases[symbol.replace(".", "-")]
                record["selection"] = "index_symbol_confirmed_by_sec"
                break
    return {"schema_version": 1, "basis": "current_sec_ticker_mapping", "source": dict(receipt), "companies": records}


def write_reference(path, reference):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(reference, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@lru_cache(maxsize=4)
def _load(path, modified, size):
    reference = json.loads(Path(path).read_text(encoding="utf-8"))
    if reference.get("schema_version") != 1 or reference.get("basis") != "current_sec_ticker_mapping":
        raise ValueError("Unsupported ticker reference.")
    if reference.get("source", {}).get("source_url") != SOURCE_URL:
        raise ValueError("Ticker reference must identify the SEC source.")
    for cik, record in reference["companies"].items():
        symbol = record.get("ticker")
        if not re.fullmatch(r"[0-9]{10}", cik) or int(cik) == 0 or not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,31}", symbol):
            raise ValueError("Invalid ticker reference entry.")
        if symbol not in {entry["ticker"] for entry in record["symbols"]}:
            raise ValueError("Selected ticker has no SEC symbol evidence.")
    return reference


def load_reference(path=None):
    path = Path(path or os.environ.get("SEC_TICKER_REFERENCE", DEFAULT_REFERENCE)).resolve()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return _load(str(path), stat.st_mtime_ns, stat.st_size)


def current_ticker(cik):
    reference = load_reference()
    if reference is None:
        return None
    return reference["companies"].get(cik, {}).get("ticker")


def missing_ticker_plan(conn, reference):
    """Inspect existing companies, without changing their names or identities."""
    companies = conn.execute("SELECT cik, name, ticker FROM sec.companies ORDER BY cik").fetchall()
    proposed, unmatched = [], []
    for cik, name, ticker in companies:
        if ticker is not None:
            continue
        match = reference["companies"].get(cik)
        if match:
            proposed.append({"cik": cik, "name": name, "ticker": match["ticker"]})
        else:
            unmatched.append({"cik": cik, "name": name})
    return {"proposed": proposed, "unmatched": unmatched}


def fill_missing_tickers(conn, reference):
    """Update only NULL ticker fields inside the caller's transaction."""
    if conn.autocommit:
        raise ValueError("Ticker updates require a caller-managed transaction.")
    plan = missing_ticker_plan(conn, reference)
    proposed = plan["proposed"]
    updated = []
    if proposed:
        updated = conn.execute("""UPDATE sec.companies AS c SET ticker=m.ticker
            FROM unnest(%s::text[], %s::text[]) AS m(cik,ticker)
            WHERE c.cik=m.cik AND c.ticker IS NULL
            RETURNING c.cik,c.ticker""",
            ([r["cik"] for r in proposed], [r["ticker"] for r in proposed])).fetchall()
    return {"updated": len(updated), "updated_companies": [{"cik": cik, "ticker": ticker} for cik, ticker in updated],
            "unmatched": plan["unmatched"], "source": reference["source"]}
