"""Archive an index-proven primary inline set without changing legacy sources.

Each HTML remains an original document. Acquisition proves index membership
and the default target; fact/continuation correspondence is a separate check.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

from .acquisition import AcquisitionError, FilingIdentity, _archive_link, _immutable_write
from .filing_sources import verify_filing
from .source_locators import source_xpath


POLICY = 'inline-set-v1'
MANIFEST_NAME = 'source_document_set_manifest.json'
SUPPLEMENTAL_ROLE = 'supplemental_inline_xbrl_document'
INLINE_ROLES = {'main_inline_xbrl_filing', SUPPLEMENTAL_ROLE}
IX = {'http://www.xbrl.org/2013/inlineXBRL', 'http://www.xbrl.org/2008/inlineXBRL'}
IX_ELEMENTS = {'header', 'references', 'resources', 'hidden', 'nonNumeric', 'nonFraction',
               'continuation', 'exclude', 'footnote', 'relationship'}
XHTML = 'http://www.w3.org/1999/xhtml'
XLINK = 'http://www.w3.org/1999/xlink'
LINK = 'http://www.xbrl.org/2003/linkbase'
XML_BASE = '{http://www.w3.org/XML/1998/namespace}base'
EXCLUDED_TYPES = {'EX-FILING FEES', 'EX-98'}
GUIDANCE_URL = 'https://www.sec.gov/files/edgar/filer-information/specifications/xbrl-guide-2026-06-29.pdf#page=181'
MAX_MEMBERS = 64
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SET_BYTES = 256 * 1024 * 1024
IDENTITY_FIELDS = ('cik', 'company', 'accession_number', 'form', 'filing_date', 'reporting_period_end', 'filing_index_url')
ENRICHMENT_FIELDS = {'sequence', 'document_type', 'is_primary', 'index_locator', 'target_membership'}


class DocumentSetError(AcquisitionError):
    """The index or source documents do not prove one primary inline set."""


def _require(condition, message):
    if not condition:
        raise DocumentSetError(message)


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _contained(root, relative):
    _require(isinstance(relative, str) and relative, 'Document-set source path is missing.')
    value = Path(relative)
    path = (root / value).resolve()
    _require(not value.is_absolute() and path.is_relative_to(root), 'Document-set path escapes the data root.')
    return path


def _read(path):
    _require(path.is_file() and 0 < path.stat().st_size <= MAX_FILE_BYTES, 'Document-set file is missing or outside the size bound.')
    return path.read_bytes()


def _filing(core):
    primary = next(record for record in core['files'] if record['role'] == 'main_inline_xbrl_filing')
    return FilingIdentity(**{key: core[key] for key in ('cik', 'company', 'accession_number', 'form', 'filing_date', 'reporting_period_end')},
                          primary_document=urlsplit(primary['source_url']).path.rsplit('/', 1)[-1])


def discover_inline_documents(index_bytes, core):
    """Partition every index-marked inline attachment into the primary set/exclusions."""
    filing = _filing(core)
    tree = html.document_fromstring(index_bytes, parser=html.HTMLParser(encoding='utf-8', no_network=True)).getroottree()
    index_hash = _hash(index_bytes)
    primary_url = filing.archive_url + filing.primary_document
    members, excluded, urls, sequences = [], [], set(), set()
    for row in tree.xpath('//table//tr'):
        cells = row.xpath('./td')
        if len(cells) < 4:
            continue
        links = cells[2].xpath('.//a/@href')
        marker = any(' '.join(''.join(node.itertext()).split()).casefold() == 'ixbrl'
                     for node in cells[2].xpath('.//span'))
        viewer = any(urlsplit(urljoin(filing.archive_url, href)).path in {
            '/ix', '/ixviewer/ix.html', '/ixviewer/doc/action', '/ixviewer/doc/action.html'} for href in links)
        if not marker and not viewer:
            continue
        _require(len(links) == 1, 'Inline index row has no unique document link.')
        url = _archive_link(links[0], filing)
        _require(url.lower().endswith(('.htm', '.html')), 'Inline index attachment is not HTML.')
        sequence = ' '.join(cells[0].itertext()).strip()
        _require(re.fullmatch(r'[1-9][0-9]*', sequence), 'Inline attachment has no numeric SEC sequence.')
        sequence = int(sequence)
        _require(sequence not in sequences and url not in urls, 'Inline index attachment sequence or URL is repeated.')
        sequences.add(sequence); urls.add(url)
        document_type = ' '.join(''.join(cells[3].itertext()).upper().split())
        is_primary = url == primary_url
        _require((is_primary and document_type == core['form']) or (not is_primary and (
            document_type in EXCLUDED_TYPES or document_type == core['form']
            or re.fullmatch(r'EX-[0-9]+(?:\.[0-9A-Z]+)*', document_type))),
            'Index contains an unsupported or conflicting inline attachment type.')
        descriptor = {'source_url': url, 'sequence': sequence, 'document_type': document_type, 'is_primary': is_primary,
                      'index_locator': {'source_xpath': source_xpath(tree, row), 'source_sha256': index_hash}}
        if document_type in EXCLUDED_TYPES:
            descriptor.update(exclusion_reason='separate_target_attachment_type', guidance_url=GUIDANCE_URL)
            excluded.append(descriptor)
        else:
            members.append(descriptor)
    _require(1 <= len(members) <= MAX_MEMBERS and sum(record['is_primary'] for record in members) == 1,
             'The index does not prove one bounded primary inline document set.')
    members.sort(key=lambda record: (not record['is_primary'], record['sequence']))
    excluded.sort(key=lambda record: record['sequence'])
    return members, excluded


def _target_proof(data, source_url, schema_url):
    _require(isinstance(data, bytes) and 0 < len(data) <= MAX_FILE_BYTES, 'Inline set member exceeds the source size bound.')
    try:
        root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    except etree.Error as exc:
        raise DocumentSetError('Inline set member is not well-formed XML.') from exc
    _require(not root.getroottree().docinfo.doctype and root.tag == '{' + XHTML + '}html' and root.prefix is None,
             'Inline set member must be an XHTML document without a DTD.')
    inline, references = [], set()
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        _require(XML_BASE not in node.attrib, 'Inline set members cannot change their XML base URI.')
        name = etree.QName(node)
        if name.namespace not in IX:
            continue
        inline.append(node)
        _require(not any(etree.QName(key).localname == 'target' for key in node.attrib),
                 'Only the default inline target is supported; target attributes are not allowed.')
        _require(name.localname not in {'tuple', 'fraction'}, 'Inline tuples and fractions are unsupported.')
        _require(name.localname in IX_ELEMENTS, 'Inline set member contains an unsupported inline element.')
        if name.localname == 'references':
            for reference in node:
                if reference.tag == '{' + LINK + '}schemaRef':
                    href = reference.get('{' + XLINK + '}href', '')
                    resolved = urljoin(source_url, href)
                    _require(bool(href) and resolved == schema_url, 'Inline member references a different target schema.')
                    references.add(resolved)
    _require(bool(inline), 'Index-marked inline member has no genuine inline elements.')
    return {'inline_target': '', 'inline_namespaces': sorted({etree.QName(node).namespace for node in inline}),
            'schema_reference_urls': sorted(references),
            'fact_elements': sum(etree.QName(node).localname in {'nonNumeric', 'nonFraction'} for node in inline)}


def _expected_set(core, members, excluded):
    xml_record = next(record for record in core['files'] if record['role'] == 'extracted_xbrl_instance')
    primary = next(record for record in members if record['is_primary'])
    return {'set_key': 'primary-inline-set', 'policy_version': POLICY, 'inline_target': '',
            'target_xml_source_url': xml_record['source_url'], 'primary_html_source_url': primary['source_url'],
            'member_source_urls': [record['source_url'] for record in members], 'excluded_inline_documents': excluded,
            'source_checks': {'index_membership_complete': True, 'single_primary_target': True, 'excluded_targets_separate': True}}


def _verify_contents(manifest, core, payloads):
    _require(sum(len(data) for data in payloads.values()) <= MAX_SET_BYTES, 'Document set exceeds the combined source size bound.')
    _require(manifest.get('schema_version') == 2 and manifest.get('status') == 'verified', 'Expected a verified schema-2 document set.')
    _require(all(manifest.get(key) == core.get(key) for key in IDENTITY_FIELDS), 'Document-set filing identity differs from its immutable core manifest.')
    _require(manifest.get('index_document') == core['index_document'], 'Document-set index receipt differs from the original archive.')
    index_data = payloads[core['index_document']['source_url']]
    members, excluded = discover_inline_documents(index_data, core)
    _require(manifest.get('document_set') == _expected_set(core, members, excluded), 'Document-set membership or target proof differs from the original index.')
    _require(all(value is True for value in manifest['document_set']['source_checks'].values()), 'Document-set source checks must be explicit booleans.')
    files = manifest.get('files')
    _require(isinstance(files, list) and len(files) == len(core['files']) + len(members) - 1,
             'Document-set source inventory is incomplete or contains extra sources.')
    records = [manifest['index_document'], *files]
    urls = [record.get('source_url') for record in records]
    _require(len(set(urls)) == len(urls) and set(urls) == set(payloads), 'Document-set sources are missing, repeated or unlisted.')
    by_url = {record['source_url']: record for record in records}
    for original in core['files']:
        current = by_url.get(original['source_url'])
        _require(current is not None and {key: value for key, value in current.items() if key not in ENRICHMENT_FIELDS} == original,
                 'Document-set core source receipt was modified.')
    for record in records:
        data = payloads[record['source_url']]
        _require(record.get('status') == 'verified' and record.get('verification', {}).get('result') == 'passed'
                 and bool(record.get('retrieved_at_utc')) and record.get('size_bytes') == len(data)
                 and record.get('sha256') == _hash(data), 'Document-set source hash, size or verification receipt differs.')
    schema = next(record['source_url'] for record in core['files'] if record['role'] == 'xbrl_extension_schema')
    references = set()
    for descriptor in members:
        record = by_url.get(descriptor['source_url'])
        _require(record is not None and all(record.get(key) == value for key, value in descriptor.items()),
                 'Inline member metadata differs from its exact index row.')
        _require(type(record['sequence']) is int and type(record['is_primary']) is bool, 'Inline member sequence or primary flag has an invalid type.')
        role = 'main_inline_xbrl_filing' if descriptor['is_primary'] else SUPPLEMENTAL_ROLE
        _require(record.get('role') == role, 'Inline document set has a conflicting source role.')
        proof = _target_proof(payloads[record['source_url']], record['source_url'], schema)
        _require(record.get('target_membership') == proof, 'Inline target membership proof differs from original bytes.')
        _require(type(record['target_membership']['fact_elements']) is int, 'Inline target inventory count must be an integer.')
        references.update(proof['schema_reference_urls'])
    _require(references == {schema}, 'Inline set has no unique reference to its target instance schema.')
    return manifest


def _record_paths(manifest, core, root):
    primary = next(record for record in core['files'] if record['role'] == 'main_inline_xbrl_filing')
    directory = _contained(root, primary['local_path']).parent
    paths, urls, output = set(), set(), []
    for record in [manifest['index_document'], *manifest['files']]:
        source = _contained(root, record['local_path'])
        _require(source not in paths and record['source_url'] not in urls, 'Document-set paths and URLs must be distinct.')
        paths.add(source); urls.add(record['source_url'])
        if record.get('role') == SUPPLEMENTAL_ROLE:
            _require(source.parent == directory and source.name == urlsplit(record['source_url']).path.rsplit('/', 1)[-1],
                     'Supplemental inline path differs from its original archive filename.')
        output.append((record, source))
    return output


def verify_document_set(manifest_path, data_root):
    """Re-read the immutable legacy archive and every separate inline member."""
    root = Path(data_root).resolve()
    path = Path(manifest_path).resolve()
    _require(path.is_relative_to(root), 'Document-set manifest is outside the data root.')
    try:
        manifest = json.loads(_read(path))
        reference = manifest['core_manifest']
        core_path = _contained(root, reference['local_path'])
        _require(core_path != path and reference['sha256'] == _hash(_read(core_path)), 'Legacy core manifest changed or references itself.')
        core = verify_filing(core_path, root)
        payloads = {}
        paths = _record_paths(manifest, core, root)
        _require(sum(source.stat().st_size for _, source in paths) <= MAX_SET_BYTES, 'Document set exceeds the combined source size bound.')
        for record, source in paths:
            payloads[record['source_url']] = _read(source)
        return _verify_contents(manifest, core, payloads)
    except DocumentSetError:
        raise
    except (OSError, ValueError, TypeError, KeyError, StopIteration, etree.Error) as exc:
        raise DocumentSetError(f'Document-set verification failed: {exc}') from exc


def download_document_set(client, core_manifest_path, data_root):
    """Publish a separate manifest last, preserving every existing source byte."""
    root = Path(data_root).resolve()
    core_path = Path(core_manifest_path).resolve()
    _require(core_path.is_relative_to(root), 'Legacy core manifest is outside the data root.')
    core_bytes = _read(core_path)
    core = verify_filing(core_path, root)
    path = core_path.with_name(MANIFEST_NAME)
    reference = {'local_path': core_path.relative_to(root).as_posix(), 'sha256': _hash(core_bytes)}
    if path.exists():
        existing = verify_document_set(path, root)
        _require(existing['core_manifest'] == reference, 'Existing document set refers to a different legacy manifest.')
        return path
    payloads = {record['source_url']: _read(_contained(root, record['local_path']))
                for record in [core['index_document'], *core['files']]}
    members, excluded = discover_inline_documents(payloads[core['index_document']['source_url']], core)
    primary = next(record for record in core['files'] if record['role'] == 'main_inline_xbrl_filing')
    schema_url = next(record['source_url'] for record in core['files'] if record['role'] == 'xbrl_extension_schema')
    directory = _contained(root, primary['local_path']).parent
    files = deepcopy(core['files'])
    extras = []
    for descriptor in members:
        url = descriptor['source_url']
        if descriptor['is_primary']:
            record = next(record for record in files if record['role'] == 'main_inline_xbrl_filing')
        else:
            data, receipt = client.get(url)
            _require(sum(len(value) for value in payloads.values()) + len(data) <= MAX_SET_BYTES,
                     'Document set exceeds the combined source size bound.')
            payloads[url] = data
            source = directory / urlsplit(url).path.rsplit('/', 1)[-1]
            _require(source.resolve().parent == directory and source.resolve().is_relative_to(root), 'Supplemental source escapes the archive.')
            record = {'role': SUPPLEMENTAL_ROLE, 'source_url': url, 'local_path': source.relative_to(root).as_posix(),
                      **receipt, 'status': 'verified', 'verification': {'result': 'passed'}}
            files.append(record)
            extras.append((source, data))
        record.update(descriptor)
        record['target_membership'] = _target_proof(payloads[url], url, schema_url)
    manifest = {'schema_version': 2, 'status': 'verified', **{key: core[key] for key in IDENTITY_FIELDS},
                'core_manifest': reference, 'index_document': deepcopy(core['index_document']), 'files': files,
                'document_set': _expected_set(core, members, excluded),
                'verified_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}
    _record_paths(manifest, core, root)
    _verify_contents(manifest, core, payloads)
    for source, data in extras:
        _require(not source.exists() or source.is_file() and source.read_bytes() == data,
                 'An existing supplemental source has different bytes.')
    _require(_read(core_path) == core_bytes, 'Legacy manifest changed during document-set acquisition.')
    for source, data in extras:
        _immutable_write(source, data)
    _immutable_write(path, (json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode('utf-8'))
    verify_document_set(path, root)
    return path
