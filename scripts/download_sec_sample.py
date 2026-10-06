"""Download and verify only the approved Microsoft sample; never extract financials."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from lxml import etree, html

ROOT = Path(__file__).resolve().parents[1]
ACCESSION = "0001193125-26-191507"
BASE_URL = "https://www.sec.gov/Archives/edgar/data/789019/000119312526191507/"
RAW_DIR = f"data/raw/sec/{ACCESSION}"
INDEX_NAME = f"{ACCESSION}-index.html"
ROBOTS_URL = "https://www.sec.gov/robots.txt"
DOCUMENTS = {
    "main_inline_xbrl_filing": "msft-20260331.htm",
    "extracted_xbrl_instance": "msft-20260331_htm.xml",
    "xbrl_extension_schema": "msft-20260331.xsd",
}
EXPECTED = {
    "accession_number": ACCESSION,
    "company": "Microsoft Corporation",
    "cik": "0000789019",
    "form": "10-Q",
    "filing_date": "2026-04-29",
    "reporting_period_end": "2026-03-31",
    "filing_index_url": BASE_URL + INDEX_NAME,
}
BLOCK_MARKERS = (
    b"your request originates from an undeclared automated tool",
    b"request rate threshold exceeded",
    b"your request has been identified as part of a network of automated tools",
    b"<title>access denied",
    b"<title>sec.gov | request rate",
)
INTERVAL_SECONDS = 2.0
MAX_BYTES = 25 * 1024 * 1024


class SampleError(RuntimeError):
    """Stop the run without retrying or replacing source documents."""


class AccessBlocked(SampleError):
    """An access restriction requires review before any further SEC requests."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise SampleError(message)


def write_json(path, value):
    """Replace only workflow metadata atomically; preserve unrelated manifest fields."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     suffix=".tmp", delete=False) as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_manifest(path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for key, value in EXPECTED.items():
        require(manifest.get(key) == value, f"Unexpected sample metadata: {key}")
    require(len(manifest["files"]) == 3, "Only the three approved documents are allowed.")
    require({f["role"] for f in manifest["files"]} == set(DOCUMENTS), "Unexpected document roles.")
    for record in manifest["files"]:
        name = DOCUMENTS[record["role"]]
        require(record["source_url"] == BASE_URL + name, "Unexpected SEC source URL.")
        require(record["local_path"] == f"{RAW_DIR}/{name}", "Unexpected local path.")
    index = manifest.setdefault("index_document", {
        "role": "filing_index", "source_url": BASE_URL + INDEX_NAME,
        "local_path": f"{RAW_DIR}/{INDEX_NAME}", "status": "pending",
        "size_bytes": None, "sha256": None, "retrieved_at_utc": None,
    })
    require(index["source_url"] == BASE_URL + INDEX_NAME, "Unexpected index URL.")
    require(index["local_path"] == f"{RAW_DIR}/{INDEX_NAME}", "Unexpected index path.")
    require(index["role"] == "filing_index", "Unexpected index role.")
    return manifest


def check_identity(identity, approved):
    require(approved, "SEC access requires explicit --identity-approved authorization.")
    require(identity and identity.isascii() and not any(ord(c) < 32 for c in identity),
            "Set EDGAR_IDENTITY to the approved name and contact email.")
    require(re.fullmatch(r"[^@]+\S +[^\s@]+@[^\s@]+\.[^\s@]+", identity),
            "EDGAR_IDENTITY must contain a name followed by a contact email.")
    require(not identity.endswith("@example.com"), "Use the approved real contact email.")
    return identity


@contextmanager
def sec_client(identity):
    # Apply settings before importing edgartools; offline verification never imports it.
    os.environ["EDGAR_LOCAL_DATA_DIR"] = str(ROOT / ".cache" / "edgar")
    os.environ["EDGAR_RATE_LIMIT_PER_SEC"] = "1"
    os.environ["EDGAR_VERIFY_SSL"] = "true"
    from edgar import set_identity
    from edgar.core import log
    from edgar.httpclient import get_http_mgr

    log.setLevel(logging.WARNING)  # Do not log the approved contact details.
    set_identity(identity)
    manager = get_http_mgr(cache_enabled=False, request_per_sec_limit=1)
    try:
        # This edgartools client has no request retry wrapper or automatic redirects.
        with manager.http_client(follow_redirects=False, timeout=30.0) as client:
            require(client.headers.get("User-Agent") == identity, "Identity header mismatch.")
            yield client
    finally:
        manager.close()


class SecReader:
    def __init__(self, client, request_log):
        self.client = client
        self.request_log = request_log
        self.last_request = None
        self.stopped = False

    def get(self, url):
        require(not self.stopped, "This SEC session has stopped; no further requests allowed.")
        allowed = {ROBOTS_URL, BASE_URL + INDEX_NAME, *(BASE_URL + n for n in DOCUMENTS.values())}
        require(url in allowed, "URL is outside this single-filing workflow.")
        if self.last_request is not None:
            time.sleep(max(0, INTERVAL_SECONDS - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        event = {"source_url": url, "requested_at_utc": utc_now()}
        self.request_log.append(event)
        try:
            with self.client.stream("GET", url) as response:
                event["http_status"] = response.status_code
                if response.status_code in (401, 403, 429):
                    event["retry_after"] = response.headers.get("Retry-After")
                    raise AccessBlocked(f"SEC returned HTTP {response.status_code}; stopped without retry.")
                require(response.status_code == 200, f"SEC returned HTTP {response.status_code}; stopped.")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    require(size <= MAX_BYTES, "Response exceeds the 25 MiB sample-file limit.")
                    chunks.append(chunk)
                data = b"".join(chunks)
                if any(marker in data.lower() for marker in BLOCK_MARKERS):
                    raise AccessBlocked("SEC returned an access-block page; stopped without retry.")
                require(bool(data), "Empty SEC response.")
                metadata = {
                    "retrieved_at_utc": utc_now(), "http_status": 200,
                    "content_type": response.headers.get("Content-Type", ""),
                    "size_bytes": len(data), "sha256": sha256(data),
                }
                event.update(metadata)
                return data, metadata
        except BaseException:
            self.stopped = True
            raise


def verify_dei(facts):
    # Inline XBRL can display a transformed date while the extracted instance
    # contains the canonical date. Handle the two forms used by this sample.
    periods = facts.get("DocumentPeriodEndDate", set())
    facts["DocumentPeriodEndDate"] = {
        "2026-03-31" if re.sub(r"[\s,]+", " ", value).strip() == "March 31 2026" else value
        for value in periods
    }
    facts["EntityCentralIndexKey"] = {
        value.zfill(10) if value.isdigit() else value
        for value in facts.get("EntityCentralIndexKey", set())
    }
    for key, expected in (("DocumentType", "10-Q"), ("DocumentPeriodEndDate", "2026-03-31")):
        require(facts.get(key) == {expected}, f"Unexpected or missing {key}.")
    require(facts.get("EntityCentralIndexKey") == {"0000789019"}, "Unexpected or missing filing CIK.")
    names = facts.get("EntityRegistrantName", set())
    require(len(names) == 1 and "microsoft" in next(iter(names)).lower(), "Unexpected registrant.")


def validate_document(data, role):
    require(len(data) > 500, "Document is too small to be the requested filing artifact.")
    require(not any(marker in data.lower() for marker in BLOCK_MARKERS), "Access-block page, not a document.")
    if role in ("filing_index", "main_inline_xbrl_filing"):
        tree = html.fromstring(data, parser=html.HTMLParser(no_network=True))
        text = " ".join(tree.text_content().split())
        require("microsoft" in text.lower(), "HTML is not a Microsoft filing document.")
        if role == "filing_index":
            require(ACCESSION in text and "10-Q" in text, "Filing index accession/form mismatch.")
            require(re.search(r"\b0*789019\b", text), "Filing index CIK mismatch.")
            for label, expected in (("Filing Date", "2026-04-29"), ("Period of Report", "2026-03-31")):
                require(re.search(re.escape(label) + r"\s+" + expected, text), f"Index {label} mismatch.")
            links = set()
            for href in tree.xpath("//a/@href"):
                url = urljoin(BASE_URL, href)
                if "doc" in parse_qs(urlsplit(url).query):
                    url = urljoin("https://www.sec.gov", parse_qs(urlsplit(url).query)["doc"][0])
                links.add(url)
            require(all(BASE_URL + name in links for name in DOCUMENTS.values()),
                    "Index does not link all three approved documents.")
            return {"result": "passed", "accession_and_dates_match": True, "document_links_match": True}
        facts = {}
        for node in tree.xpath("//*[@name]"):
            if str(node.tag).lower().endswith("nonnumeric"):
                key = node.get("name", "")
                if key.lower().startswith("dei:"):
                    facts.setdefault(key.split(":")[-1], set()).add(" ".join(node.text_content().split()))
        verify_dei(facts)
        return {"result": "passed", "inline_xbrl_identity_and_period_match": True}

    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    tree = etree.fromstring(data, parser=parser)
    require(not tree.getroottree().docinfo.doctype, "Unexpected XML DTD.")
    if role == "extracted_xbrl_instance":
        require(tree.tag == "{http://www.xbrl.org/2003/instance}xbrl", "Not an XBRL instance.")
        facts = {}
        for node in tree:
            if isinstance(node.tag, str):
                name = etree.QName(node)
                if (name.namespace or "").startswith("http://xbrl.sec.gov/dei/"):
                    facts.setdefault(name.localname, set()).add(" ".join("".join(node.itertext()).split()))
        verify_dei(facts)
        refs = tree.xpath("//*[local-name()='schemaRef']/@*[local-name()='href']")
        require(refs == [DOCUMENTS["xbrl_extension_schema"]], "XBRL schema reference mismatch.")
        contexts = tree.findall("{http://www.xbrl.org/2003/instance}context")
        require(bool(contexts), "XBRL instance has no contexts.")
        return {"result": "passed", "identity_and_period_match": True,
                "schema_reference_matches": True, "context_count": len(contexts)}
    require(role == "xbrl_extension_schema", "Unexpected document role.")
    require(tree.tag == "{http://www.w3.org/2001/XMLSchema}schema", "Not an XML schema.")
    namespace = tree.get("targetNamespace", "")
    require("20260331" in namespace and any(n in namespace.lower() for n in ("microsoft", "msft")),
            "Schema namespace does not match Microsoft and the reporting period.")
    elements = tree.findall("{http://www.w3.org/2001/XMLSchema}element")
    require(bool(elements), "Schema has no element declarations.")
    return {"result": "passed", "target_namespace": namespace, "element_count": len(elements)}


def verify_record(root, record):
    path = root / record["local_path"]
    require(path.is_file(), f"Missing source document: {record['local_path']}")
    data = path.read_bytes()
    require(record.get("size_bytes") == len(data), f"Size mismatch: {path.name}")
    require(record.get("sha256") == sha256(data), f"SHA-256 mismatch: {path.name}")
    require(record.get("retrieved_at_utc"), f"Missing download timestamp: {path.name}")
    return validate_document(data, record["role"])


def verify_sample(root, manifest):
    for record in [manifest["index_document"], *manifest["files"]]:
        verify_record(root, record)
        print(f"Verified {record['local_path']} ({record['size_bytes']:,} bytes)")
    schema_record = next(f for f in manifest["files"] if f["role"] == "xbrl_extension_schema")
    instance_record = next(f for f in manifest["files"] if f["role"] == "extracted_xbrl_instance")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    schema = etree.fromstring((root / schema_record["local_path"]).read_bytes(), parser)
    instance = etree.fromstring((root / instance_record["local_path"]).read_bytes(), parser)
    require(schema.get("targetNamespace") in instance.nsmap.values(), "Instance/schema namespace mismatch.")


def download_sample(root, manifest_path, manifest, identity):
    require(manifest.get("status") != "blocked_sec_access",
            "Previous SEC access was blocked. Review the manifest blocker before authorizing another run.")
    records = [manifest["index_document"], *manifest["files"]]
    # Inspect every existing file before any request; never overwrite an original.
    for record in records:
        if (root / record["local_path"]).exists():
            verify_record(root, record)
    if all((root / r["local_path"]).exists() for r in records):
        verify_sample(root, manifest)
        return
    attempt = {"started_at_utc": utc_now(), "requests": [], "identity_approved": True}
    manifest.setdefault("download_attempts", []).append(attempt)
    manifest["status"] = "downloading"
    manifest["download_policy"] = {
        "library": "edgartools", "library_version": version("edgartools"),
        "minimum_request_interval_seconds": INTERVAL_SECONDS, "concurrency": 1,
        "automatic_retries": 0, "follow_redirects": False, "tls_verification": True,
        "identity_storage": "Runtime environment only; omitted from tracked files",
        "max_file_bytes": MAX_BYTES,
        "sec_access_rules_url": "https://www.sec.gov/about/developer-resources",
    }
    write_json(manifest_path, manifest)
    try:
        with sec_client(identity) as client:
            reader = SecReader(client, attempt["requests"])
            robots_bytes, robots_meta = reader.get(ROBOTS_URL)
            require("text/plain" in robots_meta["content_type"].lower(), "Unexpected robots.txt content type.")
            robots_text = robots_bytes.decode("utf-8")
            require("user-agent:" in robots_text.lower(), "Invalid robots.txt response.")
            robots = RobotFileParser()
            robots.parse(robots_text.splitlines())
            for record in records:
                if not robots.can_fetch(identity, record["source_url"]):
                    raise AccessBlocked("SEC robots.txt disallows a requested source; stopped.")
            manifest["robots_check"] = {"source_url": ROBOTS_URL, **robots_meta, "all_sample_urls_allowed": True}
            for record in records:
                path = root / record["local_path"]
                if path.exists():
                    continue
                data, metadata = reader.get(record["source_url"])
                try:
                    validation = validate_document(data, record["role"])
                except (SampleError, etree.Error):
                    # Keep a rejected complete response for local diagnosis, outside
                    # tracked raw data. Never treat it as a verified source document.
                    rejected = root / ".cache" / "sec-rejected" / path.name
                    rejected.parent.mkdir(parents=True, exist_ok=True)
                    if not rejected.exists():
                        rejected.write_bytes(data)
                    raise
                path.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation preserves any original that appeared during the request.
                with path.open("xb") as handle:
                    handle.write(data)
                record.update(metadata, status="verified", verification=validation)
                manifest["updated_at_utc"] = utc_now()
                write_json(manifest_path, manifest)
        verify_sample(root, manifest)
        manifest["status"] = "verified"
        manifest["verified_at_utc"] = utc_now()
        manifest["verification_scope"] = (
            "Raw-byte hashes and sizes; index accession/dates/links; HTML and XBRL identity/period; "
            "XML structure and matching schema reference/namespace. No financial extraction or full taxonomy validation."
        )
        attempt["result"] = "verified"
    except (SampleError, httpx.HTTPError, etree.Error, OSError, UnicodeError) as exc:
        manifest["status"] = "blocked_sec_access" if isinstance(exc, AccessBlocked) else "download_failed"
        # HTTP exception text can contain configuration details; record only its type.
        message = str(exc) if isinstance(exc, SampleError) else type(exc).__name__
        attempt.update(result=manifest["status"], error=message)
        raise SampleError(message) from None
    finally:
        attempt["finished_at_utc"] = utc_now()
        manifest["updated_at_utc"] = utc_now()
        write_json(manifest_path, manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-approved", action="store_true", help="Confirm approval of EDGAR_IDENTITY.")
    parser.add_argument("--verify-only", action="store_true", help="Verify existing files without any network access.")
    args = parser.parse_args()
    try:
        manifest_path = ROOT / "source_manifest.json"
        manifest = load_manifest(manifest_path)
        if args.verify_only:
            verify_sample(ROOT, manifest)
        else:
            identity = check_identity(os.environ.get("EDGAR_IDENTITY", "").strip(), args.identity_approved)
            download_sample(ROOT, manifest_path, manifest, identity)
    except (SampleError, OSError, ValueError, etree.Error) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
