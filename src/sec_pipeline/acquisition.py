"""Discover and archive filing sources without changing previously saved bytes.

The date window applies to filing dates, inclusively. Original 10-K and 10-Q
filings are selected; amendments are deliberately excluded. A single client
serializes SEC requests and should be shared by the entire batch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx
from lxml import etree, html


class AcquisitionError(ValueError):
    """Discovery, transport, or immutable archive validation failed."""


class AccessBlocked(AcquisitionError):
    """This client must not issue any further requests."""


_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}\Z")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_BLOCK_MARKERS = (
    b"your request originates from an undeclared automated tool",
    b"request rate threshold exceeded",
    b"your request has been identified as part of a network of automated tools",
    b"<title>access denied",
    b"<title>sec.gov | request rate",
)
_ROLES = ("main_inline_xbrl_filing", "extracted_xbrl_instance", "xbrl_extension_schema")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise AcquisitionError(message)


def _cik(value: Any) -> str:
    text = str(value)
    _require(re.fullmatch(r"[0-9]{1,10}", text) and int(text) > 0, "Invalid SEC CIK.")
    return text.zfill(10)


def _date(value: Any, label: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value),
             f"Invalid {label}; expected YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise AcquisitionError(f"Invalid {label}; expected a calendar date.") from exc


def validate_sec_url(url: str) -> str:
    """Allow only expected public SEC endpoints, without redirects or credentials."""
    _require(isinstance(url, str) and url.isascii()
             and not any(ord(char) < 33 for char in url), "Invalid SEC URL.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise AcquisitionError("Invalid SEC URL.") from exc
    _require(parts.scheme == "https" and parts.hostname in {"www.sec.gov", "data.sec.gov"}
             and parts.username is None and parts.password is None
             and port is None and not parts.query and not parts.fragment,
             "Only direct HTTPS URLs on SEC data hosts are allowed.")
    path = parts.path
    _require(path == unquote(path) and "\\" not in path
             and not any(part in {".", ".."} for part in path.split("/")),
             "Encoded or traversing SEC paths are not allowed.")
    if parts.hostname == "data.sec.gov":
        allowed = path == "/robots.txt" or re.fullmatch(
            r"/submissions/CIK[0-9]{10}(?:-submissions-[0-9]+)?\.json", path)
    else:
        allowed = (path in {"/robots.txt", "/files/company_tickers.json",
                            "/files/company_tickers_exchange.json"}
                   or re.fullmatch(r"/Archives/edgar/data/[0-9]+/[0-9]{18}/[A-Za-z0-9][A-Za-z0-9_.-]*", path))
    _require(allowed, "SEC URL is outside the discovery and filing archive endpoints.")
    return url


class SecClient:
    """One sequential, TLS-verified SEC client with a runtime-only identity.

    Only transient HTTP 500/502/503/504 responses receive bounded retries.
    Authentication/rate blocks stop the client permanently, including responses
    disguised as successful HTML. Contact details are never copied to metadata.
    """

    def __init__(self, identity: str | None = None, *, interval_seconds: float = 1.0,
                 max_bytes: int = 64 * 1024 * 1024, max_retries: int = 2,
                 transport: httpx.BaseTransport | None = None):
        identity = identity if identity is not None else os.environ.get("EDGAR_IDENTITY", "")
        identity = identity.strip()
        _require(identity.isascii() and not any(ord(char) < 32 for char in identity)
                 and re.fullmatch(r"[^@]+\S +[^\s@]+@[^\s@]+\.[^\s@]+", identity)
                 and not identity.endswith("@example.com"),
                 "Set EDGAR_IDENTITY to a name and real contact email.")
        _require(isinstance(interval_seconds, (int, float)) and math.isfinite(interval_seconds)
                 and interval_seconds >= 1.0, "SEC requests must be spaced by at least one second.")
        _require(isinstance(max_bytes, int) and not isinstance(max_bytes, bool) and max_bytes > 0,
                 "The response size limit must be a positive integer.")
        _require(isinstance(max_retries, int) and not isinstance(max_retries, bool)
                 and 0 <= max_retries <= 3, "Transient retries must be between zero and three.")
        self.interval_seconds = float(interval_seconds)
        self.max_bytes = max_bytes
        self.max_retries = max_retries
        self.request_log: list[dict[str, Any]] = []
        self.stopped = False
        self._last_request: float | None = None
        self._lock = threading.Lock()
        self._client = httpx.Client(
            headers={"User-Agent": identity, "Accept-Encoding": "gzip, deflate"},
            follow_redirects=False, timeout=60.0, verify=True, transport=transport,
        )

    def __enter__(self) -> SecClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, url: str) -> tuple[bytes, dict[str, Any]]:
        url = validate_sec_url(url)
        with self._lock:
            if self.stopped:
                raise AccessBlocked("This SEC session was blocked; no further requests are allowed.")
            for attempt in range(self.max_retries + 1):
                if self._last_request is not None:
                    delay = max(0.0, self.interval_seconds - (time.monotonic() - self._last_request))
                    if delay:
                        time.sleep(delay)
                self._last_request = time.monotonic()
                event = {"source_url": url, "requested_at_utc": _now()}
                self.request_log.append(event)
                try:
                    with self._client.stream("GET", url) as response:
                        event["http_status"] = response.status_code
                        if response.status_code in {401, 403, 429}:
                            self.stopped = True
                            event["retry_after"] = response.headers.get("Retry-After")
                            raise AccessBlocked(f"SEC returned HTTP {response.status_code}; session stopped.")
                        if response.status_code in {500, 502, 503, 504} and attempt < self.max_retries:
                            event["result"] = "transient_retry"
                            time.sleep(min(2 ** attempt, 8))
                            continue
                        _require(response.status_code == 200,
                                 f"SEC returned HTTP {response.status_code}; no source saved.")
                        length = response.headers.get("Content-Length", "")
                        if length.isdigit():
                            _require(int(length) <= self.max_bytes, "SEC response exceeds the configured size limit.")
                        pieces: list[bytes] = []
                        size = 0
                        for piece in response.iter_bytes():
                            size += len(piece)
                            _require(size <= self.max_bytes, "SEC response exceeds the configured size limit.")
                            pieces.append(piece)
                        data = b"".join(pieces)
                        if any(marker in data.lower() for marker in _BLOCK_MARKERS):
                            self.stopped = True
                            raise AccessBlocked("SEC returned an access-block page; session stopped.")
                        _require(data, "SEC returned an empty response.")
                        metadata = {"retrieved_at_utc": _now(), "http_status": 200,
                                    "content_type": response.headers.get("Content-Type", ""),
                                    "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                        event.update(metadata)
                        return data, metadata
                except httpx.HTTPError as exc:
                    event["error"] = type(exc).__name__
                    raise AcquisitionError(f"SEC request failed ({type(exc).__name__}).") from None
        raise AcquisitionError("SEC request failed.")  # pragma: no cover

    def get_json(self, url: str) -> dict[str, Any]:
        data, _ = self.get(url)
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise AcquisitionError("SEC returned invalid JSON.") from exc
        _require(isinstance(result, dict), "SEC JSON must be an object.")
        return result


@dataclass(frozen=True)
class FilingIdentity:
    cik: str
    company: str
    accession_number: str
    form: str
    filing_date: str
    reporting_period_end: str
    primary_document: str

    def __post_init__(self) -> None:
        _require(self.cik == _cik(self.cik), "Filing CIK must have ten digits.")
        _require(isinstance(self.company, str) and self.company.strip(), "Missing company name.")
        _require(isinstance(self.accession_number, str) and _ACCESSION.fullmatch(self.accession_number),
                 "Invalid filing accession.")
        _require(self.form in {"10-K", "10-Q"}, "Only original 10-K and 10-Q filings are supported.")
        _date(self.filing_date, "filing date")
        _date(self.reporting_period_end, "reporting period end")
        _require(isinstance(self.primary_document, str) and _FILENAME.fullmatch(self.primary_document)
                 and self.primary_document.lower().endswith((".htm", ".html")),
                 "The primary filing must be a plain HTML file name.")

    @property
    def archive_url(self) -> str:
        return (f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/"
                f"{self.accession_number.replace('-', '')}/")

    @property
    def filing_index_url(self) -> str:
        return self.archive_url + self.accession_number + "-index.html"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"filing_index_url": self.filing_index_url}


def _filing_rows(columns: Any, cik: str, company: str, since: date, until: date) -> list[FilingIdentity]:
    _require(isinstance(columns, dict), "Invalid SEC submissions filing table.")
    fields = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")
    _require(all(isinstance(columns.get(field), list) for field in fields),
             "SEC submissions are missing filing columns.")
    _require(len({len(columns[field]) for field in fields}) == 1,
             "SEC submissions filing columns have inconsistent lengths.")
    result = []
    for accession, form, filed, period, document in zip(*(columns[field] for field in fields)):
        if form not in {"10-K", "10-Q"}:
            continue
        filed_date = _date(filed, "filing date")
        if since <= filed_date <= until:
            result.append(FilingIdentity(cik, company, accession, form, filed, period, document))
    return result


def discover_filings(client: SecClient, cik: str, since: date | str,
                     until: date | str) -> list[dict[str, Any]]:
    """Read recent and relevant historical submissions; deduplicate by accession."""
    cik, since, until = _cik(cik), _date(since, "start date"), _date(until, "end date")
    _require(since <= until, "The filing date window is reversed.")
    document = client.get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    _require(_cik(document.get("cik")) == cik, "SEC submissions CIK does not match the request.")
    company = document.get("name")
    _require(isinstance(company, str) and company.strip(), "SEC submissions have no company name.")
    filings = document.get("filings")
    _require(isinstance(filings, dict), "SEC submissions have no filing history.")
    records = _filing_rows(filings.get("recent"), cik, company, since, until)
    history = filings.get("files", [])
    _require(isinstance(history, list), "Invalid historical submissions list.")
    seen_names: set[str] = set()
    for descriptor in history:
        _require(isinstance(descriptor, dict), "Invalid historical submissions descriptor.")
        start = _date(descriptor.get("filingFrom"), "historical start date")
        end = _date(descriptor.get("filingTo"), "historical end date")
        _require(start <= end, "Historical submissions date range is reversed.")
        if end < since or start > until:
            continue
        name = descriptor.get("name")
        _require(isinstance(name, str) and re.fullmatch(rf"CIK{cik}-submissions-[0-9]+\.json", name),
                 "Historical submissions file does not match the selected CIK.")
        if name in seen_names:
            continue
        seen_names.add(name)
        columns = client.get_json(f"https://data.sec.gov/submissions/{name}")
        records.extend(_filing_rows(columns, cik, company, since, until))
    by_accession: dict[str, FilingIdentity] = {}
    for record in records:
        old = by_accession.setdefault(record.accession_number, record)
        _require(old == record, "SEC submissions contain conflicting metadata for one accession.")
    return [record.to_dict() for record in sorted(by_accession.values(),
                                                key=lambda record: (record.filing_date, record.accession_number))]


def _archive_link(href: str, filing: FilingIdentity) -> str:
    resolved = urljoin(filing.archive_url, href)
    parts = urlsplit(resolved)
    if parts.path in {"/ix", "/ixviewer/doc/action", "/ixviewer/doc/action.html", "/ixviewer/ix.html"}:
        _require(parts.scheme == "https" and parts.netloc == "www.sec.gov",
                 "Inline viewer is outside SEC.")
        query = parse_qs(parts.query)
        _require(len(query.get("doc", [])) == 1, "Inline viewer link has no unique document.")
        resolved = urljoin("https://www.sec.gov", query["doc"][0])
    validate_sec_url(resolved)
    _require(resolved.startswith(filing.archive_url), "Document link is outside the selected filing.")
    return resolved


def discover_documents(index_bytes: bytes, filing: FilingIdentity) -> dict[str, str]:
    """Select source roles from index table types and extracted-instance labels."""
    try:
        tree = html.fromstring(index_bytes, parser=html.HTMLParser(no_network=True))
    except (etree.Error, ValueError) as exc:
        raise AcquisitionError("The filing index is not readable HTML.") from exc
    candidates: dict[str, set[str]] = {role: set() for role in _ROLES}
    for row in tree.xpath("//table//tr"):
        cells = row.xpath("./td")
        if len(cells) < 4:
            continue
        kind = " ".join(cells[3].text_content().split()).upper()
        description = " ".join(cells[1].text_content().split()).upper()
        links = cells[2].xpath(".//a/@href")
        if kind == filing.form:
            role = "main_inline_xbrl_filing"
        elif kind == "EX-101.INS" or (kind == "XML" and "EXTRACTED" in description and "INSTANCE" in description):
            role = "extracted_xbrl_instance"
        elif kind == "EX-101.SCH":
            role = "xbrl_extension_schema"
        else:
            continue
        _require(len(links) == 1, f"The filing index has an ambiguous {role} link.")
        candidates[role].add(_archive_link(links[0], filing))
    primary = filing.archive_url + filing.primary_document
    _require(primary in candidates["main_inline_xbrl_filing"],
             "The filing index does not confirm the submissions primary document.")
    candidates["main_inline_xbrl_filing"] = {primary}
    _require(all(len(urls) == 1 for urls in candidates.values()),
             "The filing does not have a unique HTML, extracted XBRL instance, and extension schema.")
    result = {role: next(iter(urls)) for role, urls in candidates.items()}
    _require(result["extracted_xbrl_instance"].lower().endswith(".xml")
             and result["xbrl_extension_schema"].lower().endswith(".xsd"),
             "Unexpected filing XML or schema file extension.")
    _require(len(set(result.values())) == len(result), "Filing roles must refer to distinct files.")
    return result


def _registrant(xml_bytes: bytes, cik: str, *, payloads=None, manifest=None) -> str:
    from .filing_sources import registrant_name

    try:
        return registrant_name(xml_bytes, cik, payloads=payloads, manifest=manifest)
    except (ValueError, etree.Error) as exc:
        raise AcquisitionError(f"The filing XML registrant identity is invalid: {exc}") from exc


def _immutable_write(path: Path, data: bytes) -> None:
    """Publish complete bytes atomically without replacing an existing target."""
    if path.exists():
        _require(path.is_file() and path.read_bytes() == data,
                 f"An existing archive file has different bytes: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Windows rename is atomic and refuses an existing destination.
            # POSIX rename replaces destinations, so use a no-replace hard link.
            if os.name == "nt":
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        except FileExistsError:
            _require(path.is_file() and path.read_bytes() == data,
                     f"An existing archive file has different bytes: {path.name}")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download_filing(client: SecClient, filing: dict[str, Any] | FilingIdentity,
                    data_root: Path | str) -> Path:
    """Archive four independently verified sources and return their manifest path.

    An existing complete manifest is verified and reused without HTTP requests.
    Unmanifested files are compared against fetched bytes before any publication;
    changed originals are never replaced.
    """
    from .filing_sources import verify_documents, verify_filing
    from .registrants import canonical_identity, verify_discovery_binding, verify_document_binding

    if isinstance(filing, dict):
        try:
            identity = FilingIdentity(**{name: filing[name] for name in FilingIdentity.__dataclass_fields__})
        except (KeyError, TypeError) as exc:
            raise AcquisitionError("Incomplete discovered filing metadata.") from exc
        if "filing_index_url" in filing:
            _require(filing["filing_index_url"] == identity.filing_index_url, "Filing index URL disagrees with identity.")
    else:
        _require(isinstance(filing, FilingIdentity), "Expected discovered filing metadata.")
        identity = filing
    requested_identity = identity
    root = Path(data_root).resolve()
    relative_dir = Path("data") / "raw" / "sec" / identity.accession_number
    directory = (root / relative_dir).resolve()
    _require(directory.is_relative_to(root), "Filing archive path escapes the data root.")
    manifest_path = directory / "source_manifest.json"
    if manifest_path.exists():
        try:
            existing = verify_filing(manifest_path, root)
            if existing["cik"] != identity.cik:
                verify_discovery_binding(existing, identity.to_dict(), root)
            else:
                for field in ("cik", "accession_number", "form", "filing_date", "reporting_period_end", "filing_index_url"):
                    _require(existing[field] == identity.to_dict()[field], "Archived filing identity conflicts with discovery.")
                primary = next(record for record in existing["files"] if record["role"] == "main_inline_xbrl_filing")
                _require(primary["source_url"] == identity.archive_url + identity.primary_document,
                         "Archived primary document conflicts with discovery.")
        except (OSError, ValueError, KeyError, TypeError, etree.Error) as exc:
            raise AcquisitionError(f"Existing filing archive failed verification: {exc}") from exc
        return manifest_path
    request_start = len(client.request_log)
    index_bytes, index_meta = client.get(identity.filing_index_url)
    try:
        document_urls = discover_documents(index_bytes, identity)
    except AcquisitionError as original_error:
        try:
            canonical, alias_filers = canonical_identity(index_bytes, requested_identity.to_dict())
            if canonical["cik"] == requested_identity.cik:
                raise original_error
            identity = FilingIdentity(**{name: canonical[name] for name in FilingIdentity.__dataclass_fields__})
            # Obtain a real receipt for the canonical index. The alias response
            # must never be relabelled as bytes fetched from another URL.
            index_bytes, index_meta = client.get(identity.filing_index_url)
            confirmed, canonical_filers = canonical_identity(index_bytes, requested_identity.to_dict())
            _require(confirmed == canonical and canonical_filers == alias_filers,
                     "Alias and canonical filing indices disagree.")
            document_urls = discover_documents(index_bytes, identity)
        except AccessBlocked:
            raise
        except (ValueError, KeyError, TypeError, etree.Error) as exc:
            raise AcquisitionError(f"Cannot establish the canonical filing archive: {exc}") from exc
    urls = {"filing_index": identity.filing_index_url, **document_urls}
    payloads = {"filing_index": index_bytes}
    metadata = {"filing_index": index_meta}
    for role in _ROLES:
        payloads[role], metadata[role] = client.get(urls[role])
    manifest = {"schema_version": 1, "status": "verified", **identity.to_dict(),
                "company": _registrant(payloads["extracted_xbrl_instance"], identity.cik,
                                      payloads=payloads, manifest=identity.to_dict()),
                "discovery_company": identity.company, "verified_at_utc": _now(),
                "download_policy": {"minimum_request_interval_seconds": client.interval_seconds,
                                    "concurrency": 1, "max_transient_retries": client.max_retries,
                                    "follow_redirects": False, "tls_verification": True,
                                    "max_file_bytes": client.max_bytes,
                                    "identity_storage": "Runtime environment only"},
                "download_attempts": [{"result": "verified", "requests": client.request_log[request_start:]}]}
    records = {}
    paths = {}
    for role, url in urls.items():
        filename = urlsplit(url).path.rsplit("/", 1)[-1]
        relative = relative_dir / filename
        path = (root / relative).resolve()
        _require(path.parent == directory, "Document path escapes its filing archive.")
        records[role] = {"role": role, "source_url": url, "local_path": relative.as_posix(),
                         **metadata[role], "status": "verified", "verification": {"result": "passed"}}
        paths[role] = path
    manifest["index_document"] = records["filing_index"]
    manifest["files"] = [records[role] for role in _ROLES]
    try:
        if requested_identity.cik != identity.cik:
            verify_document_binding(manifest, requested_identity.to_dict(), payloads)
        else:
            verify_documents(manifest, payloads)
    except (ValueError, KeyError, TypeError, etree.Error) as exc:
        raise AcquisitionError(f"Downloaded filing source verification failed: {exc}") from exc
    for role, path in paths.items():
        if path.exists():
            _require(path.is_file() and path.read_bytes() == payloads[role],
                     f"An existing archive file has different bytes: {path.name}")
    for role, path in paths.items():
        _immutable_write(path, payloads[role])
    _immutable_write(manifest_path, (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    try:
        verify_filing(manifest_path, root)
    except (OSError, ValueError, KeyError, TypeError, etree.Error) as exc:
        raise AcquisitionError(f"Saved filing archive failed verification: {exc}") from exc
    return manifest_path
