"""Bind a discovered registrant to one canonical, immutable SEC accession.

These helpers perform no network requests or writes. A submissions record can
associate a co-registrant with a filing whose source documents belong to another
explicit index filer. That association does not create a second filing identity.
"""

from datetime import date
import hashlib
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from lxml import etree, html

from .filing_sources import (
    ROLES, _inline_namespace, _inline_text, _is_context_element,
    normalized_date, verify_documents,
)


class RegistrantBindingError(ValueError):
    """The source does not prove the requested filing/registrant association."""


_IX = {"http://www.xbrl.org/2013/inlineXBRL", "http://www.xbrl.org/2008/inlineXBRL"}
_XBRLDI = "http://xbrl.org/2006/xbrldi"
_IDENTITY_FIELDS = ("accession_number", "form", "filing_date", "reporting_period_end")


def _require(condition, message):
    if not condition:
        raise RegistrantBindingError(message)


def _cik(value):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value)
             and int(value) > 0, "Invalid registrant CIK.")
    return value.zfill(10)


def _base(cik, accession):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"


def _discovery(discovered):
    _require(isinstance(discovered, dict), "Expected discovered filing metadata.")
    cik = _cik(discovered.get("cik"))
    _require(cik == discovered["cik"], "Discovered CIK must contain ten digits.")
    accession = discovered.get("accession_number")
    _require(isinstance(accession, str) and re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession),
             "Invalid discovered accession.")
    _require(discovered.get("form") in {"10-K", "10-Q"}, "Only original 10-K/10-Q filings are supported.")
    for key in ("filing_date", "reporting_period_end"):
        value = discovered.get(key)
        _require(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value),
                 "Invalid discovered filing date.")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise RegistrantBindingError("Invalid discovered calendar date.") from exc
    _require(discovered["reporting_period_end"] <= discovered["filing_date"], "Report period ends after filing date.")
    name = discovered.get("primary_document")
    _require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:htm|html)", name, re.I),
             "Invalid discovered primary file name.")
    expected = _base(cik, accession) + accession + "-index.html"
    _require(discovered.get("filing_index_url", expected) == expected, "Discovered index URL disagrees with identity.")
    return cik, accession


def _index_filers(tree):
    filers = {}
    blocks = tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' companyInfo ')]")
    for block in blocks:
        names = block.xpath(".//*[contains(concat(' ', normalize-space(@class), ' '), ' companyName ')]")
        _require(len(names) == 1, "Index company block has no unique company name.")
        node = names[0]
        text = " ".join(node.text_content().split())
        match = re.fullmatch(r"(.+?)\s*\(Filer\)\s*CIK\s*:\s*([0-9]{1,10})(?:\s+.*)?", text, re.I)
        if not match:
            # Issuer/subject-company labels do not establish a filing registrant.
            continue
        name, displayed = match.group(1).strip(), _cik(match.group(2))
        linked = []
        for href in node.xpath(".//a/@href"):
            parts = urlsplit(urljoin("https://www.sec.gov", href))
            query = parse_qs(parts.query)
            if "CIK" not in query:
                continue
            _require(parts.scheme == "https" and parts.netloc == "www.sec.gov"
                     and parts.path == "/cgi-bin/browse-edgar" and not parts.fragment
                     and len(query["CIK"]) == 1,
                     "Index filer CIK link is not an unambiguous SEC company link.")
            linked.append(_cik(query["CIK"][0]))
        _require(linked == [displayed], "Index filer label and CIK link disagree.")
        _require(displayed not in filers or filers[displayed] == name, "Index filer names are ambiguous.")
        filers[displayed] = name
    _require(filers, "The index has no explicitly labelled filers.")
    return filers


def _primary_link(href, requested_base, accession, filename):
    url = urljoin(requested_base, href)
    parts = urlsplit(url)
    _require(parts.scheme == "https" and parts.netloc == "www.sec.gov" and not parts.fragment,
             "The primary document link is outside SEC.")
    if parts.path in {"/ix", "/ixviewer/ix.html", "/ixviewer/doc/action", "/ixviewer/doc/action.html"}:
        docs = parse_qs(parts.query).get("doc", [])
        _require(len(docs) == 1, "Inline viewer has no unique primary document.")
        url = urljoin("https://www.sec.gov", docs[0])
        parts = urlsplit(url)
    _require(parts.scheme == "https" and parts.netloc == "www.sec.gov"
             and not parts.query and not parts.fragment and parts.path == unquote(parts.path),
             "The primary document must use a direct SEC archive URL.")
    match = re.fullmatch(r"/Archives/edgar/data/([0-9]{1,10})/([0-9]{18})/([A-Za-z0-9][A-Za-z0-9_.-]*)", parts.path)
    _require(match is not None and match.group(2) == accession.replace("-", "")
             and match.group(3) == filename, "Primary document accession or file name mismatch.")
    return url, _cik(match.group(1))


def canonical_identity(index_bytes, discovered_dict):
    """Return ``(canonical_filing, explicit_index_associations)`` without writes.

    Association names come from SEC index labels. DEI verification is performed
    separately by verify_discovery_binding after the complete archive exists.
    The input discovery dictionary is never modified.
    """
    requested, accession = _discovery(discovered_dict)
    try:
        tree = html.fromstring(index_bytes, parser=html.HTMLParser(no_network=True))
        filers = _index_filers(tree)
        _require(requested in filers, "Requested CIK is not an explicit index filer.")
        text = " ".join(tree.text_content().split())
        _require(accession in text, "Index accession differs from discovery.")
        for label, key in (("Filing Date", "filing_date"), ("Period of Report", "reporting_period_end")):
            _require(re.search(re.escape(label) + r"\s+" + re.escape(discovered_dict[key]) + r"\b", text),
                     "Index dates differ from discovery.")
        candidates = []
        for row in tree.xpath("//table//tr"):
            cells = row.xpath("./td")
            if len(cells) < 4 or " ".join(cells[3].text_content().split()) != discovered_dict["form"]:
                continue
            links = cells[2].xpath(".//a/@href")
            # The document list may also label an unofficial PDF with the same
            # form. Select the API's exact primary filename before validating
            # its archive path, rather than treating every form row as primary.
            matching = []
            for href in links:
                parts = urlsplit(urljoin(_base(requested, accession), href))
                paths = [parts.path]
                if parts.path in {"/ix", "/ixviewer/ix.html", "/ixviewer/doc/action", "/ixviewer/doc/action.html"}:
                    paths = [urlsplit(value).path for value in parse_qs(parts.query).get("doc", [])]
                if any(unquote(path).rsplit("/", 1)[-1] == discovered_dict["primary_document"] for path in paths):
                    matching.append(href)
            if not matching:
                continue
            _require(len(links) == 1, "Primary document row has ambiguous links.")
            candidates.append(_primary_link(matching[0], _base(requested, accession), accession,
                                            discovered_dict["primary_document"]))
        _require(len(candidates) == 1, "Index has no unique primary document row for the discovered form.")
        _, canonical = candidates[0]
        _require(canonical in filers, "Canonical source CIK is not an explicit index filer.")
        result = {key: discovered_dict[key] for key in (*_IDENTITY_FIELDS, "primary_document")}
        result.update(cik=canonical, company=filers[canonical],
                      filing_index_url=_base(canonical, accession) + accession + "-index.html")
        associations = [{"cik": cik, "name": name, "is_canonical": cik == canonical,
                         "is_requested": cik == requested} for cik, name in sorted(filers.items())]
        return result, associations
    except RegistrantBindingError:
        raise
    except (etree.Error, TypeError, ValueError, KeyError) as exc:
        raise RegistrantBindingError("Cannot establish canonical identity from the SEC index.") from exc


def _expanded(value, node, inline):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*:[A-Za-z_][A-Za-z0-9_.-]*", value),
             "Legal entity member has no qualified name.")
    prefix, name = value.split(":", 1)
    namespace = _inline_namespace(node, prefix) if inline else node.nsmap.get(prefix)
    _require(bool(namespace) and bool(name), "Legal entity member namespace is not declared.")
    return namespace, name


def _dei_namespace(namespace):
    return bool(namespace and re.fullmatch(r"https?://xbrl\.sec\.gov/dei/[0-9]{4}", namespace))


def _registrant_identities(document, filers, manifest, *, inline):
    """Prove each DEI registrant against one independently identified base entity."""
    candidates = []
    for node in document.iter():
        if not isinstance(node.tag, str):
            continue
        if inline:
            if ":" not in node.tag:
                continue
            prefix, tag = node.tag.split(":", 1)
            if tag.lower() != "nonnumeric" or _inline_namespace(node, prefix) not in _IX:
                continue
            name = node.get("name", "")
            if name.count(":") != 1:
                continue
            prefix, name = name.split(":", 1)
            namespace = _inline_namespace(node, prefix)
        else:
            qname = etree.QName(node)
            namespace, name = qname.namespace, qname.localname
        if _dei_namespace(namespace):
            value = _inline_text(node, document) if inline else " ".join("".join(node.itertext()).split())
            candidates.append((name, value, node.get("contextref" if inline else "contextRef")))
    selected = {}
    for name, value, reference in candidates:
        if name == "EntityCentralIndexKey":
            cik = _cik(value)
            _require(cik in filers, "A DEI registrant is not an explicit index filer.")
            _require(bool(reference), "A DEI CIK has no context reference.")
            _require(reference not in selected or selected[reference] == cik,
                     "A DEI context has conflicting CIK facts.")
            selected[reference] = cik
    _require(bool(selected), "Combined filing has no context-scoped DEI CIKs.")
    contexts = {}
    for node in document.iter():
        if _is_context_element(node, "context", inline) and node.get("id") in selected:
            _require(node.get("id") not in contexts, "Co-registrant context is declared more than once.")
            contexts[node.get("id")] = node
    _require(set(contexts) == set(selected), "A co-registrant context is not declared.")
    identities = {}
    for reference, context in contexts.items():
        entities = [node for node in context if _is_context_element(node, "entity", inline)]
        identifiers = [node for entity in entities for node in entity if _is_context_element(node, "identifier", inline)]
        _require(len(entities) == 1 and len(identifiers) == 1, "Co-registrant context has no unique entity identifier.")
        identifier = identifiers[0]
        entity = _cik("".join(identifier.itertext()).strip())
        _require(entity in filers and identifier.get("scheme") in {"http://www.sec.gov/CIK", "https://www.sec.gov/CIK"},
                 "Co-registrant context does not identify an associated SEC issuer.")
        explicit = []
        for node in context.iter():
            if not isinstance(node.tag, str):
                continue
            if inline:
                prefix, _, local = node.tag.partition(":")
                namespace = _inline_namespace(node, prefix)
            else:
                qname = etree.QName(node)
                namespace, local = qname.namespace, qname.localname
            if namespace == _XBRLDI:
                _require(local.lower() == "explicitmember", "Unsupported co-registrant dimension content.")
                explicit.append(node)
        _require(len(explicit) <= 1, "Co-registrant context requires one explicit LegalEntityAxis member.")
        containers = [node for node in context.iter() if _is_context_element(node, "segment", inline)
                      or _is_context_element(node, "scenario", inline)]
        member_name = None
        if explicit:
            member = explicit[0]
            namespace, axis = _expanded(member.get("dimension"), member, inline)
            _require(_dei_namespace(namespace) and axis == "LegalEntityAxis", "Co-registrant context lacks the DEI LegalEntityAxis.")
            parent = member.getparent()
            _require((_is_context_element(parent, "segment", inline) and parent.getparent() is entities[0])
                     or (_is_context_element(parent, "scenario", inline) and parent.getparent() is context),
                     "Legal entity member is outside context segment/scenario.")
            _require(all(list(node) == ([member] if node is parent else [])
                         and not (node.text or "").strip()
                         and not any((child.tail or "").strip() for child in node)
                         for node in containers),
                     "Co-registrant dimension containers contain unsupported content.")
            _require(len(member) == 0, "Legal entity member cannot contain nested content.")
            member_name = _expanded("".join(member.itertext()).strip(), member, inline)
        else:
            _require(entity == selected[reference], "Undimensioned DEI CIK differs from its context entity.")
            # Unknown or spoofed dimensional content is not undimensioned.
            _require(not any(len(node) or "".join(node.itertext()).strip() for node in containers),
                     "Undimensioned DEI context contains unsupported entity content.")
        record = identities.setdefault(selected[reference], {"scope": (entity, member_name), "facts": {}})
        _require(record["scope"] == (entity, member_name), "DEI registrant has ambiguous entity or legal members.")
        for name, value, fact_reference in candidates:
            if fact_reference == reference:
                record["facts"].setdefault(name, set()).add(value)
    primaries = [cik for cik, record in identities.items() if record["scope"] == (cik, None)]
    _require(len(primaries) == 1, "Combined filing needs one unique undimensioned primary DEI registrant.")
    primary = primaries[0]
    seen_members = set()
    for cik, record in identities.items():
        entity, member = record["scope"]
        _require(entity == primary, "Co-registrant context entity differs from the proven primary.")
        if cik != primary:
            _require(member is not None and member not in seen_members, "Co-registrant legal entity member is missing or shared.")
            seen_members.add(member)
        facts = record["facts"]
        _require({_cik(value) for value in facts.get("EntityCentralIndexKey", set())} == {cik},
                 "Co-registrant context has conflicting DEI CIKs.")
        names = facts.get("EntityRegistrantName", set())
        _require(len(names) == 1 and bool(next(iter(names))), "Co-registrant name is missing or ambiguous.")
        if cik == primary or "DocumentType" in facts:
            _require(facts.get("DocumentType") == {manifest["form"]}, "Primary or co-registrant form contradicts the filing.")
        if cik == primary or "DocumentPeriodEndDate" in facts:
            _require({normalized_date(value) for value in facts.get("DocumentPeriodEndDate", set())}
                     == {manifest["reporting_period_end"]}, "Primary or co-registrant period contradicts the filing.")
    for record in identities.values():
        for field in ("DocumentType", "DocumentPeriodEndDate"):
            record["facts"].setdefault(field, set(identities[primary]["facts"][field]))
    return identities


def combined_registrant_facts(payloads, manifest, cik):
    """Return scoped XML DEI only after matching index, XML and inline identities.

    The archive owner may be a dimensional subsidiary. Its document type and
    period can inherit only from the unique undimensioned reporting registrant;
    the original archive CIK, context entities and dimensions remain untouched.
    """
    from .filing_sources import _xml

    primary_name = manifest.get("primary_document")
    if primary_name is None:
        primary = next(record for record in manifest["files"] if record["role"] == "main_inline_xbrl_filing")
        primary_name = urlsplit(primary["source_url"]).path.rsplit("/", 1)[-1]
    discovered = {key: manifest[key] for key in (*_IDENTITY_FIELDS, "cik", "filing_index_url")}
    discovered["primary_document"] = primary_name
    canonical, associations = canonical_identity(payloads["filing_index"], discovered)
    _require(canonical["cik"] == manifest["cik"], "Manifest CIK is not the canonical archive owner.")
    filers = {row["cik"] for row in associations}
    _require(cik in filers, "Requested CIK is not an explicit index filer.")
    xml = _xml(payloads["extracted_xbrl_instance"])
    _require(xml.tag == "{http://www.xbrl.org/2003/instance}xbrl", "Source is not an XBRL instance.")
    inline = html.document_fromstring(payloads["main_inline_xbrl_filing"], parser=html.HTMLParser(encoding="utf-8", no_network=True))
    xml_records = _registrant_identities(xml, filers, manifest, inline=False)
    inline_records = _registrant_identities(inline, filers, manifest, inline=True)
    _require(set(xml_records) == set(inline_records) and cik in xml_records,
             "XML and inline registrant CIKs disagree or requested CIK is absent.")
    for registrant, record in xml_records.items():
        other = inline_records[registrant]
        _require(record["scope"] == other["scope"], "XML and inline co-registrant legal identities disagree.")
        _require({value.casefold() for value in record["facts"]["EntityRegistrantName"]}
                 == {value.casefold() for value in other["facts"]["EntityRegistrantName"]},
                 "XML and inline co-registrant names disagree.")
    return xml_records[cik]["facts"]


def verify_document_binding(manifest, discovered_dict, payloads):
    """Prove a requested registrant binding before publishing source bytes.

    Registrants need matching XML/inline DEI identities. A secondary registrant
    requires a LegalEntityAxis context; missing secondary form/period facts
    inherit only from the uniquely proven undimensioned primary. The SEC archive
    owner can be either registrant. No manifest or source bytes are rewritten.
    """
    try:
        requested, _ = _discovery(discovered_dict)
        _require(isinstance(manifest, dict) and manifest.get("status") == "verified", "A verified canonical manifest is required.")
        records = [manifest["index_document"], *manifest["files"]]
        _require(len(records) == 4 and {record["role"] for record in records} == ROLES, "Canonical source roles are incomplete.")
        for record in records:
            _require(record.get("status") == "verified" and record.get("verification", {}).get("result") == "passed"
                     and bool(record.get("retrieved_at_utc")), "Canonical source verification record is missing.")
            data = payloads[record["role"]]
            _require(record.get("size_bytes") == len(data) and record.get("sha256") == hashlib.sha256(data).hexdigest(),
                     "Canonical source bytes changed.")
        verify_documents(manifest, payloads)
        canonical, _ = canonical_identity(payloads["filing_index"], discovered_dict)
        for key in (*_IDENTITY_FIELDS, "cik", "filing_index_url"):
            _require(manifest.get(key) == canonical[key], "Canonical manifest identity differs from discovered filing.")
        primary = next(record for record in records if record["role"] == "main_inline_xbrl_filing")
        _require(primary["source_url"] == _base(canonical["cik"], canonical["accession_number"]) + canonical["primary_document"],
                 "Canonical primary source differs from discovered document.")
        if requested != canonical["cik"]:
            combined_registrant_facts(payloads, manifest, requested)
        return True
    except RegistrantBindingError:
        raise
    except (OSError, ValueError, KeyError, TypeError, etree.Error) as exc:
        raise RegistrantBindingError(f"Registrant binding verification failed: {exc}") from exc


def _read_archive(manifest, data_root):
    root, seen, payloads = Path(data_root).resolve(), set(), {}
    try:
        for record in [manifest["index_document"], *manifest["files"]]:
            relative = Path(record["local_path"])
            path = (root / relative).resolve()
            _require(not relative.is_absolute() and path.is_relative_to(root) and path not in seen,
                     "Canonical source paths must be distinct and within the data root.")
            seen.add(path)
            _require(record["role"] not in payloads, "Canonical source roles must be distinct.")
            payloads[record["role"]] = path.read_bytes()
    except RegistrantBindingError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RegistrantBindingError(f"Cannot read canonical archive: {exc}") from exc
    return payloads


def verify_discovery_binding(manifest, discovered_dict, data_root):
    """Verify saved source paths/hashes and a requested registrant association."""
    return verify_document_binding(manifest, discovered_dict, _read_archive(manifest, data_root))


def index_registrants(manifest, data_root):
    """Return deterministic index filer metadata from a verified canonical archive.

    This is the index's explicit filer list, independent of which registrant
    requested the accession. Requested secondary DEI identity is established by
    verify_discovery_binding rather than inferred from this list alone.
    """
    try:
        payloads = _read_archive(manifest, data_root)
        primary = next(record for record in manifest["files"] if record["role"] == "main_inline_xbrl_filing")
        discovered = {key: manifest[key] for key in (*_IDENTITY_FIELDS, "cik", "company", "filing_index_url")}
        discovered["primary_document"] = urlsplit(primary["source_url"]).path.rsplit("/", 1)[-1]
        verify_document_binding(manifest, discovered, payloads)
        _, associations = canonical_identity(payloads["filing_index"], discovered)
        return [{key: row[key] for key in ("cik", "name", "is_canonical")} for row in associations]
    except RegistrantBindingError:
        raise
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        raise RegistrantBindingError(f"Cannot establish canonical index registrants: {exc}") from exc
