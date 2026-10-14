"""Exact fact correspondence across separate archived Inline XBRL documents.

Original XHTML trees are never combined or reparented. A second parse of each
original byte stream supplies the established HTML locators; namespace-aware
XHTML supplies contexts, units, transformations and continuation semantics.
"""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import hashlib
from html import escape
import json
from pathlib import Path
from urllib.parse import urljoin
from uuid import UUID

from lxml import etree, html

from .database import FactInput
from . import fact_extraction as fx
from .inline_value_transforms import (
    numeric_value, nonnumeric_value, validate_transform, validate_numeric_attributes, UnsupportedInlineTransform,
)
from .source_locators import source_xpath, inline_source_xpath


IX = 'http://www.xbrl.org/2013/inlineXBRL'
XHTML = 'http://www.w3.org/1999/xhtml'
XML = 'http://www.w3.org/XML/1998/namespace'
KINDS = {f'{{{IX}}}nonFraction': 'numeric', f'{{{IX}}}nonNumeric': 'nonnumeric'}
POLICY = 'inline-document-set-v1'
MARKUP_POLICY = 'escaped-markup-infoset-v1'
INLINE_NAMES = {'header', 'hidden', 'references', 'resources', 'nonFraction', 'nonNumeric', 'continuation',
                'exclude', 'footnote', 'relationship', 'tuple', 'fraction'}


class InlineDocumentSetError(ValueError):
    """The selected documents cannot prove complete, unambiguous correspondence."""

    def __init__(self, message, issues=()):
        super().__init__(message)
        self.issues = tuple(issues)


@dataclass(frozen=True)
class InlineSourceDocument:
    source_document_id: UUID | str
    filename: str
    path: Path | str
    sha256: str
    source_url: str


@dataclass(frozen=True)
class InlineDocumentSetResult:
    occurrences: tuple[dict, ...]
    inventory: dict


def _require(condition, message):
    if not condition:
        raise InlineDocumentSetError(message)


def _ncname(value):
    if not isinstance(value, str) or not value or any(character in value for character in '{}:'):
        return False
    try:
        return etree.QName(value).localname == value
    except ValueError:
        return False


def _bool(value, default=False):
    if value is None:
        return default
    _require(value in {'true', 'false', '1', '0'}, 'Invalid inline boolean attribute.')
    return value in {'true', '1'}


def _expanded(value, node):
    try:
        namespace, name = fx._resolve(value, node)
    except fx._Reject as exc:
        raise InlineDocumentSetError(exc.reason) from exc
    _require(namespace is not None and _ncname(name), 'Inline QName lacks a valid namespace or local name.')
    return '{' + namespace + '}' + name


def _period(fact):
    if fact.period_kind == 'instant':
        return {'instant': fact.instant_date.isoformat()}
    if fact.period_kind == 'duration':
        return {'startDate': fact.period_start.isoformat(), 'endDate': fact.period_end.isoformat()}
    return {'forever': ''}


def _reference(fact):
    return {'xml_id': fact.occurrence_key, 'concept': '{' + fact.concept_namespace + '}' + fact.concept_name,
            'context_ref': fact.source_reference.get('context_ref'), 'unit_ref': fact.source_reference.get('unit_ref'),
            'unit': fact.unit, 'period': _period(fact), 'dimensions': fact.dimensions,
            'raw_value': fact.raw_value, 'is_nil': fact.is_nil}


def _append_text(parts, value, escaped):
    if value:
        parts.append(escape(value, quote=False) if escaped else value)


def _content(node, escaped, source_url, *, fragment_ids=None, preserved_links=None):
    """Map one original fragment, preserving all relevant text and markup."""
    parts = []
    _append_text(parts, node.text, escaped)
    for child in node:
        if not isinstance(child.tag, str):
            _require(not escaped, 'Escaped markup comments or processing instructions are outside the lossless serializer.')
            _append_text(parts, child.tail, escaped)
            continue
        name = etree.QName(child)
        if child.tag == f'{{{IX}}}exclude':
            pass
        elif name.namespace == IX or not escaped:
            parts.append(_content(child, escaped, source_url, fragment_ids=fragment_ids, preserved_links=preserved_links))
        else:
            _require(name.namespace == XHTML, 'Escaped text contains unsupported non-XHTML markup.')
            attributes = []
            for attribute, value in child.attrib.items():
                attr = etree.QName(attribute)
                _require(attr.namespace in (None, XML), 'Escaped XHTML has an unsupported namespaced attribute.')
                key = 'xml:' + attr.localname if attr.namespace == XML else attr.localname
                _require(key not in {'action', 'archive', 'background', 'cite', 'classid', 'codebase', 'data',
                                     'longdesc', 'profile', 'usemap'},
                         'Escaped markup has a URI attribute outside the supported source serializer.')
                if (fragment_ids is not None and key == 'href' and name.localname == 'a'
                        and value.startswith('#')):
                    identifier = value[1:]
                    _require(_ncname(identifier) and identifier in fragment_ids,
                             'A preserved fragment link requires a unique target in its original document.')
                    preserved_links.append({'href': value, 'target_id': identifier,
                                            'source_xpath': child.getroottree().getpath(child)})
                elif key in {'href', 'src'}:
                    value = urljoin(source_url, value)
                encoded = escape(value, quote=False).replace('"', '&quot;').replace('\t', '&#9;').replace('\n', '&#10;').replace('\r', '&#13;')
                attributes.append(' ' + key + '="' + encoded + '"')
            content = _content(child, True, source_url, fragment_ids=fragment_ids, preserved_links=preserved_links)
            tag = name.localname
            parts.append('<' + tag + ''.join(attributes) + ('>' + content + '</' + tag + '>' if content else '/>'))
        _append_text(parts, child.tail, escaped)
    return ''.join(parts)


def _markup_canonical(value):
    """Canonicalize only serialization details, preserving text/attribute data.

    This is a value-fragment comparison, not a combined filing DOM. Malformed
    markup is rejected instead of repaired by an HTML parser.
    """
    _require('<!DOCTYPE' not in value.upper() and '<!ENTITY' not in value.upper(), 'Escaped markup declares an unsupported DTD/entity.')
    try:
        root = etree.fromstring(('<fragment>' + value + '</fragment>').encode('utf-8'),
                               etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    except etree.XMLSyntaxError as exc:
        raise InlineDocumentSetError('Escaped markup is not a losslessly parseable XML fragment.') from exc

    def canonical(node):
        if not isinstance(node.tag, str):
            _require(isinstance(node, etree._Comment), 'Escaped markup contains a processing instruction.')
            return ['comment', node.text or '', node.tail or '']
        name = etree.QName(node)
        _require(name.namespace in (None, XHTML), 'Escaped markup has an unsupported element namespace.')
        attrs = []
        for key, content in node.attrib.items():
            attr = etree.QName(key)
            _require(attr.namespace in (None, XML), 'Escaped markup has an unsupported attribute namespace.')
            attrs.append([attr.namespace or '', attr.localname, content])
        return [name.localname, sorted(attrs), node.text or '', [canonical(child) for child in node], node.tail or '']

    return json.dumps(canonical(root), ensure_ascii=False, separators=(',', ':'))


class _Document:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.document_id = str(UUID(str(descriptor.source_document_id)))
        self.data = Path(descriptor.path).read_bytes()
        self.hash = hashlib.sha256(self.data).hexdigest()
        _require(self.hash == descriptor.sha256, 'Inline document hash differs from its verified source receipt.')
        _require(descriptor.filename == Path(descriptor.path).name, 'Inline filename differs from its original archived path.')
        try:
            self.root = fx._parse(self.data, 'Inline document ' + descriptor.filename)
        except fx.FactExtractionError as exc:
            raise InlineDocumentSetError(str(exc)) from exc
        _require(self.root.tag == f'{{{XHTML}}}html', 'Inline source must retain its original XHTML root namespace.')
        self.original_ids = {node.get('id'): node for node in self.root.iter() if node.get('id')}
        self.html = html.document_fromstring(self.data, parser=html.HTMLParser(encoding='utf-8', no_network=True)).getroottree()
        self.html_ids = {}
        for node in self.html.iter():
            if node.get('id'):
                self.html_ids.setdefault(node.get('id'), []).append(node)

    def locator(self, node):
        identifier = node.get('id')
        candidates = self.html_ids.get(identifier, [])
        _require(len(candidates) == 1, 'Original XML element lacks a unique unchanged HTML locator.')
        target = candidates[0]
        original = etree.QName(node)
        _require(str(target.tag).split(':')[-1].lower() == original.localname.lower(),
                 'Namespace-aware and HTML DOMs disagree on the source element kind.')
        return {'source_document_id': self.document_id, 'filename': self.descriptor.filename,
                'source_xpath': source_xpath(self.html, target), 'inline_xpath': inline_source_xpath(self.html, target),
                'html_sha256': self.hash}


def read_inline_document_set(documents, facts, *, xml_sha256):
    """Prove an ID-bijective default-target set without changing XML facts.

    Input documents must already be selected by the index-verified document-set
    manifest. Independent exhibits are not silently excluded here: unexpected
    targets or unmatched occurrences fail the complete set.
    """
    _require(documents and facts, 'A document set requires source members and existing XML facts.')
    _require(isinstance(xml_sha256, str) and len(xml_sha256) == 64 and all(char in '0123456789abcdef' for char in xml_sha256),
             'The target XML hash must be a verified SHA-256.')
    docs = [_Document(value) for value in documents]
    _require(len({doc.document_id for doc in docs}) == len(docs)
             and len({doc.descriptor.filename for doc in docs}) == len(docs)
             and len({doc.descriptor.source_url for doc in docs}) == len(docs), 'Document-set member identities must be unique.')
    scopes = {(fact.filing_id, fact.source_document_id, fact.extraction_version) for fact in facts if isinstance(fact, FactInput)}
    _require(len(scopes) == 1 and all(isinstance(fact, FactInput) for fact in facts), 'Existing XML facts must retain one exact source/version scope.')
    _require(str(next(iter(scopes))[1]) not in {doc.document_id for doc in docs}, 'HTML and target XML document IDs must remain distinct.')
    by_fact = {}
    for fact in facts:
        identifier = fact.source_reference.get('element_id')
        _require(_ncname(identifier) and identifier == fact.occurrence_key and identifier not in by_fact,
                 'Target facts require unique unchanged XML/inline occurrence IDs.')
        by_fact[identifier] = fact
    all_ids, contexts, units, numeric, continuations, owners = {}, {}, {}, {}, {}, {}
    all_inline, footnotes, relationships = [], [], []
    for doc in docs:
        for node in doc.root.iter():
            if not isinstance(node.tag, str):
                continue
            name = etree.QName(node)
            identifier = node.get('id')
            if identifier:
                _require(_ncname(identifier) and identifier not in all_ids, 'Duplicate or ambiguous ID in the original inline document set: ' + identifier)
                all_ids[identifier] = node
                owners[node] = doc
            _require(node.get(f'{{{XML}}}base') is None, 'XML base overrides are outside the selected SEC inline-set policy.')
            _require(not (name.namespace == XHTML and name.localname.lower() == 'base'),
                     'HTML base overrides are outside the original-document URI mapping policy.')
            _require(name.localname not in INLINE_NAMES or name.namespace in {IX, XHTML},
                     'An inline-like element uses an unsupported namespace.')
            if name.namespace == IX:
                all_inline.append(node)
                _require(name.localname in INLINE_NAMES, 'An unknown Inline XBRL element cannot be silently omitted.')
                _require(node.get('target') is None and node.tag not in {f'{{{IX}}}tuple', f'{{{IX}}}fraction'},
                         'Unsupported separate target, tuple or fraction in the primary SEC inline set.')
                _require(node.get('tupleRef') is None, 'Tuple references are outside the selected inline-set policy.')
                if node.tag in KINDS:
                    _require(_ncname(identifier), 'An inline fact has no unambiguous original ID.')
                    numeric[identifier] = node
                elif node.tag == f'{{{IX}}}continuation':
                    _require(_ncname(identifier) and not any(parent.tag == f'{{{IX}}}hidden' for parent in node.iterancestors()),
                             'Continuation IDs must be explicit and outside ix:hidden.')
                    continuations[identifier] = node
                elif node.tag == f'{{{IX}}}footnote':
                    _require(_ncname(identifier), 'An inline footnote has no original set-wide ID.')
                    footnotes.append(node)
                elif node.tag == f'{{{IX}}}relationship':
                    relationships.append(node)
            if node.tag in {f'{{{fx.XBRLI}}}context', f'{{{fx.XBRLI}}}unit'}:
                _require(_ncname(identifier) and any(parent.tag == f'{{{IX}}}resources' for parent in node.iterancestors()),
                         'Context/unit definitions require IDs in original inline resources.')
                _require(not any(isinstance(child.tag, str) and etree.QName(child).namespace == IX for child in node.iterdescendants()),
                         'Inline elements cannot be nested within a source context or unit.')
                try:
                    parsed = fx._context(node) if node.tag.endswith('}context') else fx._unit(node)
                except fx._Reject as exc:
                    raise InlineDocumentSetError(exc.reason) from exc
                (contexts if node.tag.endswith('}context') else units)[identifier] = (node, parsed)
    _require(set(numeric) == set(by_fact), 'Target XML facts and the complete inline member set are not ID-bijective.',)
    footnote_ids = {node.get('id') for node in footnotes}
    for relationship in relationships:
        _require(relationship.getparent().tag == f'{{{IX}}}resources', 'Inline relationships require original resources placement.')
        sources, targets = relationship.get('fromRefs', '').split(), relationship.get('toRefs', '').split()
        _require(sources and targets and len(set(sources)) == len(sources) and len(set(targets)) == len(targets)
                 and not (set(sources) & set(targets)) and all(identifier in numeric for identifier in sources)
                 and (all(identifier in numeric for identifier in targets) or all(identifier in footnote_ids for identifier in targets)),
                 'Missing, duplicate or incompatible set-wide relationship references.')
    incoming = Counter()
    for node in all_inline:
        continuation = node.get('continuedAt')
        if continuation is not None:
            _require(node.tag in {f'{{{IX}}}nonNumeric', f'{{{IX}}}footnote', f'{{{IX}}}continuation'}
                     and _ncname(continuation) and continuation in continuations,
                     'Missing or invalid set-wide continuation reference.')
            incoming[continuation] += 1
    _require(all(incoming[key] == 1 for key in continuations), 'Continuation chains have shared, duplicate or unreferenced fragments.')
    def chain(node):
        found, seen = [], set()
        while node is not None:
            _require(node not in seen and len(found) < 10000, 'Cyclic or excessive inline continuation chain.')
            _require(not any(previous in set(node.iterancestors()) or node in set(previous.iterancestors()) for previous in found),
                     'Continuation chain members contain one another in the original DOM.')
            seen.add(node); found.append(node)
            node = continuations.get(node.get('continuedAt'))
        return found

    for node in [*continuations.values(), *footnotes]:
        chain(node)
    occurrences, issues = [], []
    counts = Counter({key: 0 for key in ('nil_values_verified', 'numeric_values_equal', 'nonnumeric_values_equal',
                                       'nonnumeric_markup_values_equal', 'nonnumeric_literal_values_equal')})
    for identifier, fact in by_fact.items():
        node = numeric[identifier]
        doc = owners[node]
        try:
            locator = doc.locator(node)
            kind = KINDS[node.tag]
            _require(node.get(f'{{{XML}}}lang') == fact.source_reference.get('lang'),
                     'Inline language differs from the original target XML occurrence.')
            if kind == 'nonnumeric':
                _require(not any(node.get(key) is not None for key in ('unitRef', 'scale', 'sign', 'decimals', 'precision')),
                         'Nonnumeric facts contain unsupported numeric attributes.')
            else:
                _require(node.get('escape') is None, 'Numeric facts cannot have an escape attribute.')
                validate_numeric_attributes(scale=node.get('scale'), sign=node.get('sign'))
            context_ref, unit_ref = node.get('contextRef'), node.get('unitRef')
            _require(context_ref == fact.source_reference.get('context_ref') and context_ref in contexts,
                     'Inline context reference differs from its target XML occurrence.')
            context_node, context = contexts[context_ref]
            for field in ('period_kind', 'period_start', 'period_end', 'instant_date'):
                _require(context.get(field) == getattr(fact, field), 'Inline context period differs from target XML.')
            _require(context['entity'] == fact.source_reference.get('entity') and context['dimensions'] == fact.dimensions
                     and context['dimension_containers'] == fact.source_reference.get('dimension_containers'),
                     'Inline context entity or dimensions differ from target XML.')
            _require(_expanded(node.get('name'), node) == '{' + fact.concept_namespace + '}' + fact.concept_name,
                     'Inline concept QName differs from target XML.')
            _require(unit_ref == fact.source_reference.get('unit_ref') and (kind == 'numeric') == (fact.unit is not None),
                     'Inline numeric kind or unit reference differs from target XML.')
            unit_source = None
            if kind == 'numeric':
                _require(unit_ref in units and units[unit_ref][1] == fact.unit, 'Set-wide inline unit differs from target XML.')
                unit_source = owners[units[unit_ref][0]].locator(units[unit_ref][0])
                _require(node.get('decimals') == fact.reported_decimals and node.get('precision') == fact.reported_precision,
                         'Inline numeric reporting precision differs from target XML.')
            try:
                nil = fx._is_nil(node)
            except fx._Reject as exc:
                raise InlineDocumentSetError(exc.reason) from exc
            _require(nil == fact.is_nil, 'Inline nil flag differs from target XML.')
            escaped = _bool(node.get('escape'))
            _require(kind == 'nonnumeric' or not escaped, 'Numeric inline facts cannot use escaped text.')
            source_chain = chain(node)
            for member in source_chain:
                owners.setdefault(member, doc)
            fragments = []
            values = []
            for order, member in enumerate(source_chain):
                owner = owners[member]
                fragment = owner.locator(member)
                content = _content(member, escaped, owner.descriptor.source_url)
                values.append(content)
                fragments.append({**fragment, 'fragment_order': order, 'element_id': member.get('id'),
                                  'text': _content(member, False, owner.descriptor.source_url),
                                  'mapped_fragment_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
            content = ''.join(values)
            formatted = _expanded(node.get('format'), node) if node.get('format') is not None else None
            if formatted:
                validate_transform(formatted, kind)
            comparison = 'exact'
            canonical = {}
            if nil:
                _require(not any(isinstance(child.tag, str) for child in node) and len(source_chain) == 1
                         and not content.strip(' \t\r\n') and fact.raw_value is None and fact.numeric_value is None,
                         'Explicit nil occurrence has content or a numeric/XML value.')
                mapped = None
                counts['nil_values_verified'] += 1
            elif kind == 'numeric':
                _require(node.get('continuedAt') is None, 'Numeric facts cannot acquire continued text.')
                _require((node.get('decimals') is None) != (node.get('precision') is None),
                         'Nonnil numeric facts require exactly one reporting-precision attribute.')
                _require(all(isinstance(child.tag, str) for child in node),
                         'Numeric source content cannot contain comments or processing instructions.')
                children = [child for child in node if isinstance(child.tag, str)]
                if children:
                    _require(len(children) == 1 and not node.text and all(not child.tail for child in node),
                             'Nested numeric content must have one numeric child without mixed text.')
                    for child in children:
                        _require(child.tag == f'{{{IX}}}nonFraction', 'Numeric inline content contains unsupported markup.')
                        _require(child.get('unitRef') == unit_ref and child.get('scale') == node.get('scale')
                                 and (_expanded(child.get('format'), child) if child.get('format') is not None else None) == formatted,
                                 'Nested numeric facts disagree on format, unit or scale.')
                value = numeric_value(content, formatted, scale=node.get('scale'), sign=node.get('sign'))
                _require(value == fact.numeric_value and value == Decimal(fact.raw_value), 'Transformed inline number differs from the original XML amount.')
                mapped = str(value)
                counts['numeric_values_equal'] += 1
            else:
                mapped = nonnumeric_value(content, formatted)
                if mapped != fact.raw_value:
                    _require(escaped and formatted is None, 'Transformed nonnumeric value differs from target XML.')
                    original_canonical, target_canonical = _markup_canonical(mapped), _markup_canonical(fact.raw_value)
                    if original_canonical != target_canonical:
                        # SEC extracted instances sometimes retain the exact
                        # original same-document fragment spelling. Keep the
                        # normal resolved-URI mapping for every accepted value;
                        # only an otherwise exact original-markup match may use
                        # this separately recorded source-preservation strategy.
                        preserved_values, links = [], []
                        for member in source_chain:
                            owner = owners[member]
                            local_links = []
                            preserved_values.append(_content(member, True, owner.descriptor.source_url,
                                fragment_ids=owner.original_ids, preserved_links=local_links))
                            links.extend({**link, 'source_document_id': owner.document_id,
                                          'source_url': owner.descriptor.source_url, 'source_dom': 'original_xhtml'}
                                         for link in local_links)
                        preserved = ''.join(preserved_values)
                        preserved_canonical = _markup_canonical(preserved)
                        _require(links and preserved_canonical == target_canonical,
                                 'Escaped text differs semantically from the target XML fragment.')
                        mapped, original_canonical = preserved, preserved_canonical
                        for fragment, value in zip(fragments, preserved_values):
                            fragment['mapped_fragment_sha256'] = hashlib.sha256(value.encode('utf-8')).hexdigest()
                        canonical['inline_uri_mapping'] = {'strategy': 'preserved-original-local-fragments-v1', 'links': links}
                    _require(original_canonical == target_canonical, 'Escaped text differs semantically from the target XML fragment.')
                    comparison = MARKUP_POLICY
                    canonical.update(inline_canonical_sha256=hashlib.sha256(original_canonical.encode()).hexdigest(),
                                     xml_canonical_sha256=hashlib.sha256(target_canonical.encode()).hexdigest())
                counts['nonnumeric_values_equal'] += 1
                counts['nonnumeric_markup_values_equal' if comparison == MARKUP_POLICY else 'nonnumeric_literal_values_equal'] += 1
            evidence = {**_reference(fact), 'inline_kind': kind, 'inline_escape': escaped,
                        'inline_mapped_value': mapped, 'inline_is_nil': nil, 'value_comparison': comparison, **canonical,
                        'html_sha256': doc.hash, 'xml_sha256': xml_sha256, 'inline_xpath': locator['inline_xpath'],
                        'inline_format': formatted, 'inline_scale': node.get('scale'), 'inline_sign': node.get('sign'),
                        'inline_attributes': dict(node.attrib), 'filename': doc.descriptor.filename,
                        'context_source': owners[context_node].locator(context_node), 'unit_source': unit_source,
                        'continuation_fragments': fragments}
            occurrences.append({'inline_document_id': doc.document_id, 'inline_id': identifier,
                                'source_xpath': locator['source_xpath'], 'evidence': evidence,
                                'fragments': tuple({'source_document_id': part['source_document_id'], 'source_xpath': part['source_xpath'],
                                                    'fragment_order': part['fragment_order']} for part in fragments)})
        except (InlineDocumentSetError, UnsupportedInlineTransform, ValueError) as exc:
            issues.append({'inline_id': identifier, 'filename': doc.descriptor.filename, 'problem': str(exc)})
    if issues:
        raise InlineDocumentSetError(f'{len(issues)} inline occurrence(s) lack exact document-set correspondence.', issues)
    members = [{'source_document_id': doc.document_id, 'filename': doc.descriptor.filename, 'sha256': doc.hash,
                'source_url': doc.descriptor.source_url} for doc in docs]
    return InlineDocumentSetResult(tuple(occurrences), {
        'verification_version': POLICY, 'complete': True, 'members': members, 'xml_sha256': xml_sha256,
        'facts': len(facts), 'inline_elements': len(numeric), 'matched': len(occurrences),
        'contexts': len(contexts), 'units': len(units), 'continuations': len(continuations),
        'footnotes': len(footnotes), 'relationships': len(relationships), 'mismatches': [], 'inline_ids_without_fact': [],
        'numeric_values_not_compared': 0, **dict(counts),
    })
