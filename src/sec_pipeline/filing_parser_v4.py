"""Pure, source-reconstructed filing sets with separate original HTML reports.

The selected primary statements must all belong to one unambiguous source
member. Facts span the verified inline set; reports and chunks never combine
documents. Report coverage remains unknown beyond the existing cleaner.
"""

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
from uuid import NAMESPACE_URL, UUID, uuid5

from lxml import html

from . import filing_adapter as base
from . import filing_adapter_v3 as source_adapter
from .chunking import chunk_report
from .database import ChunkInput, FactInput, FinancialTableInput, ReportInput
from .document_sets import verify_document_set
from .fact_extraction import IX, XML_ROLE, extract_facts
from .inline_document_sets import InlineSourceDocument, read_inline_document_set
from .processing import DryRunContext, ParseResult, ProcessingError, SourceIds
from .registrants import index_registrants
from .report_cleaning import clean_report
from .source_cells_v3 import SourceBindings, transform_tables as transform_v3, validate_representations
from .source_cells_v4 import transform_tables as transform_v4, validate_transformation
from .source_locators import inline_elements, inline_source_xpath, source_xpath
from .statement_headings import recover_headings
from .tables import (
    _STATEMENT_CONCEPTS, _hidden, _inline_namespaces, _statement_headings,
    extract_tables, local, select_statement_segments, validate_statement_segments,
)
from .v4_table_cells import v4_relational_cells


VERSIONS = {'facts': 'facts-v1', 'tables': 'tables-v4', 'reports': 'report-v1', 'chunks': 'paragraph-v1'}
COVERAGE = {'facts': 'complete', 'tables': 'partial', 'reports': 'unknown', 'chunks': 'complete'}
PRIMARY_KINDS = frozenset({'balance_sheet', 'income_statement', 'cash_flow'})
REQUIRED_CHECKS = ('document_set_complete', 'union_correspondence', 'numeric_values_compared',
                   'source_cells_complete', 'source_bindings_exact', 'reports_complete', 'chunks_complete',
                   'fact_source_scope', 'statement_member_unambiguous', 'statement_segment_coverage',
                   'section_source_locators', 'section_labels_not_promoted', 'index_registrants_verified',
                   'fresh_source_reconstruction')


class FilingSetParseError(ProcessingError):
    """Original sources do not establish a complete supported V4 filing set."""


@dataclass(frozen=True)
class ParsedDocument:
    source_document_id: UUID
    source_url: str
    filename: str
    source_sha256: str
    is_primary: bool
    report_placeholder_id: UUID
    report: ReportInput
    chunks: tuple[ChunkInput, ...]
    tables: tuple[FinancialTableInput, ...]


@dataclass(frozen=True)
class ParsedFilingSet:
    filing_id: UUID
    xml_document_id: UUID
    manifest_json: str
    manifest_sha256: str
    facts: tuple[FactInput, ...]
    occurrences: tuple[dict, ...]
    documents: tuple[ParsedDocument, ...]
    checks: dict[str, bool]
    semantic_checks: dict[str, bool]
    issues: tuple[dict, ...]
    coverage: dict[str, str]
    counts: dict[str, int]
    source_inventory: dict
    registrants: tuple[dict, ...]

    @property
    def manifest(self):
        """Return a disposable copy; the retained verified receipt is immutable."""
        return json.loads(self.manifest_json)

    @property
    def versions(self):
        return dict(VERSIONS)

    @property
    def tables(self):
        return tuple(table for document in self.documents for table in document.tables)


@dataclass(frozen=True)
class FilingSetValidation:
    checks: dict[str, bool]
    semantic_checks: dict[str, bool]
    issues: tuple[dict, ...]
    counts: dict[str, int]


def _require(condition, message):
    if not condition:
        raise FilingSetParseError(message)


def _uuid(value, label):
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise FilingSetParseError(f'Invalid authoritative {label} UUID.') from exc


def dry_run_ids(manifest):
    """Explicit deterministic placeholders; ingestion supplies registered IDs."""
    filing = uuid5(NAMESPACE_URL, manifest['filing_index_url'])
    sources = {record['source_url']: uuid5(filing, record['source_url'])
               for record in [manifest['index_document'], *manifest['files']]}
    return filing, sources


def _source_ids(manifest, filing_id, source_document_ids):
    if filing_id is None and source_document_ids is None:
        return dry_run_ids(manifest)
    _require(filing_id is not None and isinstance(source_document_ids, Mapping),
             'Supply both filing_id and source_document_ids, or neither for a dry run.')
    filing = _uuid(filing_id, 'filing')
    expected = {record['source_url'] for record in [manifest['index_document'], *manifest['files']]}
    _require(set(source_document_ids) == expected, 'Authoritative source IDs must enumerate exactly the verified source URLs.')
    sources = {url: _uuid(value, 'source document') for url, value in source_document_ids.items()}
    _require(len(set(sources.values())) == len(sources) and filing not in sources.values(),
             'Filing and original source document IDs must be distinct.')
    return filing, sources


def _path(root, record):
    relative = Path(record['local_path'])
    path = (root / relative).resolve()
    _require(not relative.is_absolute() and path.is_relative_to(root), 'A source path escapes its verified data root.')
    return path


def _context(manifest, root, filing, xml_record, html_record, sources, report_id=None, report=None):
    # These are adapter-check arguments, not a replacement source manifest.
    member_manifest = {**manifest, 'files': [xml_record, {**html_record, 'role': 'main_inline_xbrl_filing'}]}
    return DryRunContext(manifest=member_manifest, data_root=root,
        source_ids=SourceIds(filing, sources[xml_record['source_url']], sources[html_record['source_url']]),
        versions={**VERSIONS, 'tables': 'tables-v2'}, outputs={'report': ParseResult([report])} if report else {},
        report_id=report_id or uuid5(sources[html_record['source_url']], VERSIONS['reports']))


def _statement_signals(tree):
    """Detect competing headed financial statements without merging their data."""
    found = []
    for table in tree.xpath('//table'):
        if _hidden(table) or table.xpath('.//table'):
            continue
        kinds = _statement_headings(table) | {heading.kind for heading in recover_headings(table)}
        if not kinds:
            continue
        concepts = set()
        for node in inline_elements(tree, IX, 'nonfraction'):
            if table not in node.iterancestors() or _hidden(node):
                continue
            prefix, separator, name = node.get('name', '').partition(':')
            namespace = _inline_namespaces(node).get(prefix, '')
            if separator and re.fullmatch(r'https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^/]+', namespace):
                concepts.add(name)
        for kind in sorted(kinds & PRIMARY_KINDS):
            if any(concepts.intersection(group) for group in _STATEMENT_CONCEPTS[kind]):
                found.append({'statement_type': kind, 'source_xpath': source_xpath(tree, table)})
    return found


def _choose_member(member_records, root, facts):
    selected, failures, signals = [], {}, {}
    for record in member_records:
        path = _path(root, record)
        tree = html.parse(str(path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
        try:
            segments = select_statement_segments(tree, facts)
            _require({segment.statement_type for segment in segments} == PRIMARY_KINDS,
                     'A statement member must contain every primary statement group.')
            selected.append(record)
        except ValueError as exc:
            failures[record['source_url']] = str(exc)
            signals[record['source_url']] = _statement_signals(tree)
    _require(len(selected) == 1, 'Expected one original document containing all three primary statements; '
             f'found {len(selected)}. Selection failures: {json.dumps(failures, sort_keys=True)}')
    _require(not any(signals.values()), 'Other document members contain competing or split primary-statement evidence: '
             + json.dumps({url: value for url, value in signals.items() if value}, sort_keys=True))
    return selected[0]


def _checked_tables(manifest, root, filing, xml_record, member, sources, facts, occurrences):
    path, xml_path = _path(root, member), _path(root, xml_record)
    context = _context(manifest, root, filing, xml_record, member, sources)
    v2 = extract_tables(path, xml_path, filing_id=filing, html_document_id=context.source_ids.html_document_id,
                        xml_document_id=context.source_ids.xml_document_id, facts_version='facts-v1', tables_version='tables-v2')
    source = path.read_bytes()
    bindings = SourceBindings(source, facts)
    _require(validate_statement_segments(v2, bindings.tree, facts, xml_path=xml_path) is True,
             'Primary statement grouping differs from original source evidence.')
    v3 = transform_v3(v2, source, facts)
    validate_representations(v3, source, facts)
    references, cells = source_adapter._table_checks(context, v3, {fact.occurrence_key: fact for fact in facts}, bindings)
    _require(references and cells, 'Table references, cells or original locators are inconsistent.')
    v4 = tuple(transform_v4(v3, source, facts))
    _require(validate_transformation(v4, v3, source, facts) is True, 'V4 review transformation is not source-exact.')
    by_occurrence = {item['inline_id']: item for item in occurrences}
    for table in v4:
        for cell, accepted, descriptor in v4_relational_cells(UUID(int=0), table):
            _require(cell.resolution_status != 'unresolved',
                     f'Unaccounted financial cell {table.table_key}/{cell.cell_key}: {descriptor.get("unresolved_reason")}')
            refs = [*accepted, *descriptor.get('source_fact_references', [])]
            if (descriptor['column'] > 0 and descriptor.get('column_period')
                    and descriptor['classification'] in {'formatting', 'label'}
                    and re.fullmatch(r'\(?\s*[$€£¥]?\s*[+-]?\d[\d,\s]*(?:\.\d+)?\s*%?\s*\)?', descriptor['display_text'])
                    and not any(re.fullmatch(r'notereferencenumbers?', re.sub(r'\s+', '', heading), re.I)
                                for heading in descriptor.get('column_headings', []))):
                raise FilingSetParseError('A displayed financial number has no accounted source representation.')
            targets = bindings.tree.xpath(descriptor['source_xpath'])
            _require(len(targets) == 1, 'A table cell has no unique original DOM locator.')
            visible_numeric = {node.get('id') for node in targets[0].iter()
                               if local(node) == 'nonfraction' and not _hidden(node)}
            _require(visible_numeric <= {ref['xml_id'] for ref in refs},
                     'An original tagged cell has been left outside the fact/reference inventory.')
            for ref in refs:
                occurrence = by_occurrence.get(ref['xml_id'])
                _require(occurrence is not None and occurrence['inline_document_id'] == str(table.source_document_id)
                         and occurrence['evidence']['inline_xpath'] == ref['inline_xpath'],
                         'A cell reference does not belong to its original document-set occurrence.')
    return v4


def _build(manifest_path, data_root, *, filing_id=None, source_document_ids=None):
    root, manifest_path = Path(data_root).resolve(), Path(manifest_path).resolve()
    manifest = verify_document_set(manifest_path, root)
    manifest_bytes = manifest_path.read_bytes()
    _require(json.loads(manifest_bytes) == manifest, 'Document-set manifest changed after source verification.')
    filing, sources = _source_ids(manifest, filing_id, source_document_ids)
    records = {record['source_url']: record for record in manifest['files']}
    xml_record = records[manifest['document_set']['target_xml_source_url']]
    _require(xml_record['role'] == XML_ROLE, 'Document set does not identify the original target XML.')
    members = [records[url] for url in manifest['document_set']['member_source_urls']]
    facts = tuple(extract_facts(_path(root, xml_record), filing_id=filing,
        source_document_id=sources[xml_record['source_url']], extraction_version='facts-v1').require_complete().facts)
    descriptors = [InlineSourceDocument(sources[record['source_url']], _path(root, record).name, _path(root, record),
                                       record['sha256'], record['source_url']) for record in members]
    union = read_inline_document_set(descriptors, facts, xml_sha256=xml_record['sha256'])
    selected = _choose_member(members, root, facts)
    tables = _checked_tables(manifest, root, filing, xml_record, selected, sources, facts, union.occurrences)
    documents = []
    for record in members:
        path = _path(root, record)
        source_id = sources[record['source_url']]
        report_id = uuid5(source_id, VERSIONS['reports'])
        cleaned = clean_report(path, form=manifest['form'])
        report = ReportInput(filing_id=filing, source_document_id=source_id, extraction_version='report-v1',
                             full_text=cleaned.full_text, sections=deepcopy(cleaned.sections))
        chunks = tuple(chunk_report(filing_id=filing, report_id=report_id, full_text=report.full_text,
                                   sections=report.sections, chunking_version='paragraph-v1'))
        context = _context(manifest, root, filing, xml_record, record, sources, report_id, report)
        document = html.parse(str(path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
        sections, locators, chunk_bounds = base._report_checks(context, report, chunks, document, path)
        _require(sections and locators and chunk_bounds, 'Report sections or chunk bounds fail original-document validation.')
        _require(all(section.get('validation_status') in {'detected', 'unverified'} for section in report.sections),
                 'Heuristic report section labels cannot be promoted to validated.')
        documents.append(ParsedDocument(source_id, record['source_url'], path.name, record['sha256'], record['is_primary'],
                         report_id, report, chunks, tables if record['source_url'] == selected['source_url'] else ()))
    core = json.loads(_path(root, manifest['core_manifest']).read_bytes())
    registrants = tuple(index_registrants(core, root))
    _require(bool(registrants), 'The original index does not establish filing registrants.')
    issues = tuple(deepcopy(issue) for table in tables for row in table.structure['rows'] for cell in row
                   for issue in cell.get('quality_issues', []))
    _require(len({issue['issue_key'] for issue in issues}) == len(issues), 'Quality issues must have unique source identities.')
    inventory = Counter(cell['classification'] for table in tables for row in table.structure['rows'] for cell in row)
    counts = {'facts': len(facts), 'occurrences': len(union.occurrences), 'members': len(documents),
              'tables': len(tables), 'reports': len(documents), 'chunks': sum(len(doc.chunks) for doc in documents),
              'cells': sum(inventory.values()), 'source_only_cells': inventory['source_only_marker'],
              'review_cells': inventory['source_conflict'], 'issues': len(issues), 'unresolved_cells': inventory['unresolved']}
    checks = {key: True for key in REQUIRED_CHECKS if key != 'fresh_source_reconstruction'}
    return ParsedFilingSet(filing, sources[xml_record['source_url']], json.dumps(manifest, sort_keys=True, separators=(',', ':')),
        hashlib.sha256(manifest_bytes).hexdigest(), facts, union.occurrences, tuple(documents), checks,
        {'period_header_consistency': not issues}, issues, dict(COVERAGE), counts, union.inventory, registrants)


def validate_filing_set(parsed, manifest_path, data_root, *, filing_id=None, source_document_ids=None):
    """Independently rebuild every retained payload before accepting any write."""
    _require(isinstance(parsed, ParsedFilingSet), 'V4 validation requires the complete parsed filing-set DTO.')
    expected = _build(manifest_path, data_root, filing_id=filing_id, source_document_ids=source_document_ids)
    for name in ('filing_id', 'xml_document_id', 'manifest_json', 'manifest_sha256', 'facts', 'occurrences', 'documents',
                 'semantic_checks', 'issues', 'coverage', 'counts', 'source_inventory', 'registrants'):
        _require(getattr(parsed, name) == getattr(expected, name), f'V4 {name} differs from fresh original-source reconstruction.')
    supplied = {name: value for name, value in parsed.checks.items() if name != 'fresh_source_reconstruction'}
    _require(supplied == expected.checks and parsed.checks.get('fresh_source_reconstruction', True) is True,
             'Retained V4 source checks differ from independently verified evidence.')
    return FilingSetValidation({**expected.checks, 'fresh_source_reconstruction': True},
                               deepcopy(expected.semantic_checks), deepcopy(expected.issues), dict(expected.counts))


def parse_filing_set(manifest_path, data_root, *, filing_id=None, source_document_ids=None):
    """Return only a complete, twice source-verified set; perform no database IO."""
    result = _build(manifest_path, data_root, filing_id=filing_id, source_document_ids=source_document_ids)
    validation = validate_filing_set(result, manifest_path, data_root, filing_id=filing_id, source_document_ids=source_document_ids)
    return replace(result, checks=validation.checks)
