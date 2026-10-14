"""Verify archived 10-K/10-Q sources against their own filing identity."""

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from lxml import etree, html


class SourceVerificationError(ValueError):
    """Source bytes or provenance do not establish the requested filing."""


ROLES = {"filing_index", "main_inline_xbrl_filing", "extracted_xbrl_instance", "xbrl_extension_schema"}
BLOCK_MARKERS = (b"undeclared automated tool", b"request rate threshold exceeded", b"<title>access denied")


def require(condition, message):
    if not condition:
        raise SourceVerificationError(message)


def archive_base(manifest):
    cik, accession = manifest.get("cik", ""), manifest.get("accession_number", "")
    require(isinstance(cik, str) and re.fullmatch(r"\d{10}", cik) and int(cik) > 0, "Invalid filing CIK.")
    require(isinstance(accession, str) and re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession), "Invalid accession number.")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"


def normalized_date(value):
    value = re.sub(r"[\s,]+", " ", value).strip()
    for pattern in ("%Y-%m-%d", "%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%m-%d-%Y", "%d %B %Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return value


def _xml(data):
    document = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    require(not document.getroottree().docinfo.doctype, "XML declarations must not include a DTD.")
    return document


def _inline_namespace(node, prefix):
    ancestor, namespace = node, None
    while ancestor is not None and namespace is None:
        namespace = ancestor.get("xmlns:" + prefix)
        ancestor = ancestor.getparent()
    return namespace


def _is_context_element(node, local_name, inline):
    if not isinstance(node.tag, str):
        return False
    if inline:
        if ":" not in node.tag:
            return False
        prefix, name = node.tag.split(":", 1)
        return name.lower() == local_name.lower() and _inline_namespace(node, prefix) == "http://www.xbrl.org/2003/instance"
    return node.tag == "{http://www.xbrl.org/2003/instance}" + local_name


def _inline_text(node, document):
    """Follow declared continuation chains and omit inline excluded text."""
    namespaces = {"http://www.xbrl.org/2013/inlineXBRL", "http://www.xbrl.org/2008/inlineXBRL"}

    def is_inline(element, local_name):
        if not isinstance(element.tag, str) or ":" not in element.tag:
            return False
        prefix, name = element.tag.split(":", 1)
        return name.lower() == local_name and _inline_namespace(element, prefix) in namespaces

    def visible_text(element):
        pieces = [element.text or ""]
        for child in element:
            if isinstance(child.tag, str) and not is_inline(child, "exclude"):
                pieces.append(visible_text(child))
            pieces.append(child.tail or "")
        return "".join(pieces)

    pieces, seen = [], set()
    current = node
    while True:
        pieces.append(visible_text(current))
        reference = current.get("continuedat")
        if not reference:
            break
        require(reference not in seen and len(seen) < 1000, "Invalid cyclic or excessive inline continuation chain.")
        seen.add(reference)
        matches = document.xpath("//*[@id=$reference]", reference=reference)
        require(len(matches) == 1 and is_inline(matches[0], "continuation"), "Inline continuation target is missing or ambiguous.")
        current = matches[0]
    return " ".join("".join(pieces).split())


def _dei(document, *, expected_cik, inline=False):
    """Select DEI identity through matching CIK facts and declared contexts.

    Co-registrants can share a context entity identifier while a LegalEntityAxis
    distinguishes their DEI facts. Matching the context identifier alone would
    incorrectly combine those registrants' names and CIKs.
    """
    candidates = []
    for node in document.iter():
        if not isinstance(node.tag, str):
            continue
        if inline:
            if ":" not in node.tag or node.tag.split(":", 1)[1].lower() != "nonnumeric":
                continue
            tag_prefix = node.tag.split(":", 1)[0]
            inline_namespace = _inline_namespace(node, tag_prefix)
            if inline_namespace not in {"http://www.xbrl.org/2013/inlineXBRL", "http://www.xbrl.org/2008/inlineXBRL"}:
                continue
            name = node.get("name", "")
            if ":" not in name:
                continue
            prefix, name = name.split(":", 1)
            # HTML parsing retains xmlns attributes rather than XML nsmap.
            namespace = _inline_namespace(node, prefix)
            if not namespace or not re.match(r"https?://xbrl\.sec\.gov/dei/", namespace):
                continue
        else:
            qname = etree.QName(node)
            if not re.match(r"https?://xbrl\.sec\.gov/dei/", qname.namespace or ""):
                continue
            name = qname.localname
        value = _inline_text(node, document) if inline else " ".join("".join(node.itertext()).split())
        reference = node.get("contextref" if inline else "contextRef")
        candidates.append((name, value, reference))
    selected = {reference for name, value, reference in candidates
                if name == "EntityCentralIndexKey" and re.fullmatch(r"[0-9]{1,10}", value)
                and value.zfill(10) == expected_cik}
    require(bool(selected) and None not in selected and "" not in selected,
            "Document has no context-scoped DEI CIK matching filing metadata.")
    contexts = {}
    for node in document.iter():
        if _is_context_element(node, "context", inline) and node.get("id") in selected:
            reference = node.get("id")
            require(reference not in contexts, "Selected DEI context is declared more than once.")
            contexts[reference] = node
    require(set(contexts) == selected, "A selected DEI context is not declared in the document.")
    for context in contexts.values():
        entities = [node for node in context if _is_context_element(node, "entity", inline)]
        identifiers = [node for entity in entities for node in entity
                       if _is_context_element(node, "identifier", inline)]
        values = ["".join(node.itertext()).strip() for node in identifiers]
        require(len(entities) == 1 and len(values) == 1
                and re.fullmatch(r"[0-9]{1,10}", values[0])
                and values[0].zfill(10) == expected_cik,
                "Selected DEI context entity does not match filing CIK.")
    result = {}
    for name, value, reference in candidates:
        if reference in selected:
            result.setdefault(name, set()).add(value)
    return result


def registrant_name(xml_bytes, cik, *, payloads=None, manifest=None):
    """Return the filed registrant name belonging to the requested DEI CIK."""
    document = _xml(xml_bytes)
    require(document.tag == "{http://www.xbrl.org/2003/instance}xbrl", "Source is not an XBRL instance.")
    try:
        facts = _dei(document, expected_cik=cik)
    except SourceVerificationError:
        if payloads is None or manifest is None:
            raise
        from .registrants import combined_registrant_facts
        try:
            require(payloads.get("extracted_xbrl_instance") == xml_bytes, "Registrant XML bytes differ from supplied sources.")
            facts = combined_registrant_facts(payloads, manifest, cik)
        except (ValueError, KeyError, TypeError, etree.Error) as exc:
            raise SourceVerificationError(f"Combined registrant identity is invalid: {exc}") from exc
    require({value.zfill(10) for value in facts.get("EntityCentralIndexKey", set())} == {cik},
            "Selected DEI context contains conflicting CIK facts.")
    names = facts.get("EntityRegistrantName", set())
    require(len(names) == 1 and bool(next(iter(names))), "The filing has no unique XML registrant name for its CIK.")
    return next(iter(names))


def _identity(facts, manifest, *, inline=False):
    ciks = {v.zfill(10) if v.isdigit() else v for v in facts.get("EntityCentralIndexKey", set())}
    require(ciks == {manifest["cik"]}, "Document CIK does not match filing metadata.")
    require(facts.get("DocumentType") == {manifest["form"]}, "Document form does not match filing metadata.")
    periods = {normalized_date(v) for v in facts.get("DocumentPeriodEndDate", set())}
    require(periods == {manifest["reporting_period_end"]}, "Document report period does not match filing metadata.")
    names = facts.get("EntityRegistrantName", set())
    require(len(names) == 1 and bool(next(iter(names))), "Document registrant name is missing or ambiguous.")
    if not inline:
        require({v.casefold() for v in names} == {manifest["company"].casefold()}, "Manifest company must match the name as filed in XML.")


def verify_documents(manifest, payloads):
    """Validate four document payloads keyed by role, without writing files."""
    base = archive_base(manifest)
    require(manifest.get("schema_version") == 1, "Expected manifest schema version 1.")
    require(manifest.get("form") in {"10-K", "10-Q"}, "Only original 10-K and 10-Q filings are supported.")
    for key in ("filing_date", "reporting_period_end"):
        require(date.fromisoformat(manifest[key]).isoformat() == manifest[key], "Invalid filing date.")
    require(manifest["reporting_period_end"] <= manifest["filing_date"], "Reporting period ends after filing date.")
    require(set(payloads) == ROLES, "Expected index, inline HTML, instance XML, and extension schema.")
    records = [manifest["index_document"], *manifest["files"]]
    require(len(records) == 4 and {r["role"] for r in records} == ROLES, "Source roles must be distinct.")
    by_role = {record["role"]: record for record in records}
    require(by_role["filing_index"]["source_url"] == manifest["filing_index_url"] == base + manifest["accession_number"] + "-index.html", "Filing index URL mismatch.")
    for record in records:
        url = record["source_url"]
        name = url.removeprefix(base)
        require(url.startswith(base) and re.fullmatch(r"[A-Za-z0-9_.-]+", name) and name not in {".", ".."}, "Source URL is outside the filing archive.")
        data = payloads[record["role"]]
        require(bool(data) and not any(marker in data.lower() for marker in BLOCK_MARKERS), "Empty or blocked filing source.")

    index = html.fromstring(payloads["filing_index"], parser=html.HTMLParser(no_network=True))
    index_text = " ".join(index.text_content().split())
    require(manifest["accession_number"] in index_text and re.search(r"\b" + re.escape(manifest["form"]) + r"\b(?!/A)", index_text), "Filing index accession/form mismatch.")
    require(re.search(r"\bCIK\s*:?\s*0*" + str(int(manifest["cik"])) + r"\b", index_text, re.I), "Filing index CIK mismatch.")
    for label, key in (("Filing Date", "filing_date"), ("Period of Report", "reporting_period_end")):
        require(re.search(re.escape(label) + r"\s+" + re.escape(manifest[key]), index_text), "Filing index date mismatch.")
    links = set()
    for href in index.xpath("//a/@href"):
        url = urljoin(base, href)
        parts = urlsplit(url)
        doc = parse_qs(parts.query).get("doc") if parts.scheme == "https" and parts.netloc == "www.sec.gov" else None
        links.add(urljoin("https://www.sec.gov", doc[0]) if doc else url)
    require(all(r["source_url"] in links for r in manifest["files"]), "Filing index does not link every source.")

    instance = _xml(payloads["extracted_xbrl_instance"])
    require(instance.tag == "{http://www.xbrl.org/2003/instance}xbrl", "Source is not an XBRL instance.")
    require(bool(instance.findall("{http://www.xbrl.org/2003/instance}context")), "XBRL instance has no contexts.")
    schema = _xml(payloads["xbrl_extension_schema"])
    require(schema.tag == "{http://www.w3.org/2001/XMLSchema}schema", "Source is not an extension schema.")
    refs = instance.xpath("./link:schemaRef/@xlink:href", namespaces={
        "link": "http://www.xbrl.org/2003/linkbase", "xlink": "http://www.w3.org/1999/xlink"})
    require(len(refs) == 1 and urljoin(base, refs[0]) == by_role["xbrl_extension_schema"]["source_url"], "Instance schema reference mismatch.")
    namespace = schema.get("targetNamespace")
    require(bool(namespace) and namespace in instance.nsmap.values(), "Instance/schema namespace mismatch.")
    inline = html.document_fromstring(payloads["main_inline_xbrl_filing"], parser=html.HTMLParser(encoding="utf-8", no_network=True))
    try:
        _identity(_dei(instance, expected_cik=manifest["cik"]), manifest)
        _identity(_dei(inline, expected_cik=manifest["cik"], inline=True), manifest, inline=True)
    except SourceVerificationError:
        # Keep ordinary CIK/entity matching strict. A combined filing requires
        # an independent index + XML + inline proof before any inheritance.
        from .registrants import combined_registrant_facts
        try:
            _identity(combined_registrant_facts(payloads, manifest, manifest["cik"]), manifest)
        except (ValueError, KeyError, TypeError, etree.Error) as exc:
            raise SourceVerificationError(f"Combined filing identity is invalid: {exc}") from exc
    return manifest


def verify_filing(manifest_path, data_root):
    """Re-read and verify local hashes, paths, identity, and source relationships."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    require(isinstance(manifest, dict) and manifest.get("status") == "verified", "The source manifest must have verified status.")
    root, seen, payloads = Path(data_root).resolve(), set(), {}
    for record in [manifest["index_document"], *manifest["files"]]:
        require(record.get("status") == "verified", f"Source is not marked verified: {record.get('local_path', '')}")
    for record in [manifest["index_document"], *manifest["files"]]:
        relative = Path(record["local_path"])
        path = (root / relative).resolve()
        require(not relative.is_absolute() and path.is_relative_to(root) and path not in seen, "Source paths must be distinct and contained in the data root.")
        seen.add(path)
        require(record.get("status") == "verified" and record.get("verification", {}).get("result") == "passed", "Source verification record is missing.")
        require(bool(record.get("retrieved_at_utc")), "Source retrieval time is missing.")
        require(path.is_file(), f"Missing source document: {relative.name}")
        data = path.read_bytes()
        require(record.get("size_bytes") == len(data), f"Size mismatch: {relative.name}")
        require(record.get("sha256") == hashlib.sha256(data).hexdigest(), f"SHA-256 mismatch: {relative.name}")
        require(record["role"] not in payloads, "Duplicate source role.")
        payloads[record["role"]] = data
    return verify_documents(manifest, payloads)
