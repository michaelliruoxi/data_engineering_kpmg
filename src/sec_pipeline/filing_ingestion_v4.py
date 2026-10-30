"""Commit one source-verified document set in a single filing transaction."""

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from uuid import UUID

from .bulk_storage import store_many
from .chunking import store_chunks
from .database import (
    ChunkInput, CompanyInput, FactInput, FinancialTableInput, ReportInput, SourceDocumentInput,
    register_company, register_source_document, seed_manifest, store_financial_table, store_report,
)
from .document_sets import SUPPLEMENTAL_ROLE, verify_document_set
from .fact_extraction import store_facts
from .filing_parser_v4 import (
    REQUIRED_CHECKS, VERSIONS, ParsedFilingSet, parse_filing_set, validate_filing_set,
)
from .processing import ProcessingError
from .parser_policy import policy_metadata, require_current_receipt
from .source_set_storage import (
    DocumentMemberInput, DocumentSetInput, FactSourceOccurrenceInput, ReportMembershipInput,
    V4ProcessingSetInput, store_document_set, store_source_occurrences, store_v4_processing_set,
)
from .v4_table_cells import store_v4_table_cells, v4_relational_cells


REQUIRED_SOURCE_CHECKS = tuple(REQUIRED_CHECKS)


def _require(condition, message):
    if not condition:
        raise ProcessingError(message)


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _active(conn, phase):
    _require(not conn.closed and conn.info.transaction_status.name == 'INTRANS',
             f'V4 transaction is inactive or aborted after {phase}.')


def configuration_from_manifest(manifest, manifest_sha256):
    """Build the immutable receipt configuration from already verified sources."""
    records = [manifest['index_document'], *manifest['files']]
    _require(manifest.get('schema_version') == 2 and manifest.get('status') == 'verified', 'V4 requires a verified schema-2 manifest.')
    _require(isinstance(manifest_sha256, str) and len(manifest_sha256) == 64
             and all(character in '0123456789abcdef' for character in manifest_sha256), 'Invalid document-set manifest hash.')
    _require(len({record['local_path'] for record in records}) == len(records), 'Source paths are repeated.')
    return {'versions': dict(VERSIONS), **policy_metadata(VERSIONS),
            'source_hashes': {record['local_path']: record['sha256'] for record in records},
            'source_documents': {record['local_path']: {key: record[key] for key in ('role', 'source_url', 'sha256', 'size_bytes')}
                                 for record in records},
            'document_set_manifest_sha256': manifest_sha256,
            'core_manifest_sha256': manifest['core_manifest']['sha256'], 'document_set': deepcopy(manifest['document_set'])}


def rebind_chunks(document, report_id):
    """Change only the temporary report UUID; retain original section keys/data."""
    _require(isinstance(report_id, UUID), 'Stored report ID must be a UUID.')
    result = []
    for chunk in document.chunks:
        _require(isinstance(chunk, ChunkInput) and chunk.report_id == document.report_placeholder_id
                 and chunk.section_id is None, 'Chunks must retain their parser placeholder and unresolved section UUID.')
        updated = replace(chunk, report_id=report_id)
        _require(replace(updated, report_id=chunk.report_id) == chunk, 'Rebinding changed chunk content.')
        result.append(updated)
    return tuple(result)


def _parsed_checks(parsed, manifest, digest, filing_id, sources):
    _require(isinstance(parsed, ParsedFilingSet) and parsed.manifest == manifest
             and parsed.manifest_sha256 == digest and parsed.filing_id == filing_id and parsed.versions == VERSIONS,
             'Parsed filing scope or retained manifest differs from the registered sources.')
    _require(isinstance(parsed.checks, Mapping) and set(REQUIRED_SOURCE_CHECKS) <= parsed.checks.keys()
             and all(value is True for value in parsed.checks.values()), 'Every required V4 source check must pass explicitly.')
    xml_id = sources[manifest['document_set']['target_xml_source_url']]
    _require(parsed.xml_document_id == xml_id and bool(parsed.facts), 'Target XML identity or fact inventory is missing.')
    fact_ids = set()
    for fact in parsed.facts:
        _require(isinstance(fact, FactInput) and fact.filing_id == filing_id and fact.source_document_id == xml_id
                 and fact.extraction_version == VERSIONS['facts']
                 and fact.occurrence_key == fact.source_reference.get('element_id')
                 and fact.occurrence_key not in fact_ids, 'Facts must retain unique original XML occurrences and scope.')
        fact_ids.add(fact.occurrence_key)
    members = manifest['document_set']['member_source_urls']
    _require(len(parsed.documents) == len(members) and {document.source_url for document in parsed.documents} == set(members),
             'Parsed reports must enumerate every original inline member exactly once.')
    expected_scope = {'filing_id': str(filing_id), 'xml_document_id': str(xml_id), 'facts_version': VERSIONS['facts']}
    issue_keys, table_keys = set(), set()
    for document in parsed.documents:
        _require(document.source_document_id == sources[document.source_url], 'Parsed HTML uses an unregistered source UUID.')
        report = document.report
        _require(isinstance(report, ReportInput) and report.filing_id == filing_id
                 and report.source_document_id == document.source_document_id
                 and report.extraction_version == VERSIONS['reports'] and bool(document.chunks),
                 'Every original HTML member requires its own report and chunk inventory.')
        for chunk in document.chunks:
            _require(isinstance(chunk, ChunkInput) and chunk.filing_id == filing_id
                     and chunk.chunking_version == VERSIONS['chunks'] and chunk.report_id == document.report_placeholder_id,
                     'A chunk differs from its original document/report/version scope.')
        for table in document.tables:
            _require(isinstance(table, FinancialTableInput) and table.filing_id == filing_id
                     and table.source_document_id == document.source_document_id and table.extraction_version == VERSIONS['tables']
                     and table.structure.get('schema_version') == 3 and table.structure.get('fact_scope') == expected_scope,
                     'A table differs from its original HTML or target XML scope.')
            key = (table.source_document_id, table.table_key)
            _require(key not in table_keys, 'A physical table identity is repeated.')
            table_keys.add(key)
            for cell, accepted, descriptor in v4_relational_cells(UUID(int=0), table):
                _require(cell.resolution_status != 'unresolved', 'Unaccounted financial cells block V4 ingestion.')
                for reference in [*accepted, *descriptor.get('source_fact_references', [])]:
                    _require(reference.get('xml_id') in fact_ids and reference.get('scope') == expected_scope,
                             'A V4 table reference has no matching original XML scope.')
                for issue in descriptor.get('quality_issues', []):
                    _require(issue['issue_key'] not in issue_keys, 'Quality issue keys must be unique.')
                    issue_keys.add(issue['issue_key'])
    _require(bool(table_keys), 'V4 requires verified primary statement tables.')
    _require(len(parsed.issues) == len(issue_keys) and {issue['issue_key'] for issue in parsed.issues} == issue_keys,
             'Parsed quality issue inventory differs from the retained source cells.')
    _require(isinstance(parsed.semantic_checks, Mapping) and parsed.semantic_checks.get('period_header_consistency') is (not issue_keys)
             and all(type(value) is bool and (value or key == 'period_header_consistency')
                     for key, value in parsed.semantic_checks.items()), 'Semantic review must match the exact retained quality issues.')
    occurrences = [record['inline_id'] for record in parsed.occurrences]
    _require(len(occurrences) == len(fact_ids) and set(occurrences) == fact_ids, 'Source occurrences must cover every XML fact once.')
    member_ids = {sources[url] for url in members}
    _require(all(UUID(record['inline_document_id']) in member_ids for record in parsed.occurrences),
             'A source occurrence belongs to an unselected HTML document.')


def _expected_counts(parsed):
    counts = Counter()
    for document in parsed.documents:
        for table in document.tables:
            for cell, _, _ in v4_relational_cells(UUID(int=0), table):
                if cell.kind == 'data':
                    counts['financial_cells'] += 1
                    if cell.resolution_status == 'resolved':
                        counts['accepted_cells'] += 1
                if cell.resolution_status == 'source_only':
                    counts['source_only_cells'] += 1
                if cell.resolution_status == 'review_required':
                    counts['review_required_cells'] += 1
    return {'inline_documents': len(parsed.documents), 'facts': len(parsed.facts), 'tables': len(parsed.tables),
            'reports': len(parsed.documents), 'chunks': sum(len(document.chunks) for document in parsed.documents),
            **{key: counts[key] for key in ('financial_cells', 'accepted_cells', 'source_only_cells', 'review_required_cells')},
            'quality_issues': len(parsed.issues), 'unaccounted_cells': 0}


def _status(parsed):
    return 'review_required' if parsed.issues else 'passed'


def _summary(parsed, configuration, counts, *, mode):
    status = _status(parsed)
    return {'mode': mode, 'accession_number': parsed.manifest['accession_number'], 'versions': dict(VERSIONS),
            'document_set_manifest_sha256': parsed.manifest_sha256, 'source_hashes': configuration['source_hashes'],
            'configuration': configuration, 'coverage': dict(parsed.coverage), 'counts': counts,
            'source_validation_status': 'passed', 'validation_status': status,
            'source_checks': dict(parsed.checks), 'semantic_checks': dict(parsed.semantic_checks),
            'validation': {'passed': status == 'passed', 'required_checks': list(REQUIRED_SOURCE_CHECKS), 'checks': dict(parsed.checks)},
            'preferred': False}


def run_dry_run(manifest_path, data_root):
    """Return the pure parser's exact source/review result without a connection."""
    parsed = parse_filing_set(Path(manifest_path), Path(data_root).resolve())
    configuration = configuration_from_manifest(parsed.manifest, parsed.manifest_sha256)
    # The parser owns deterministic dry-run IDs for all sources; its own full
    # reconstruction includes XML/index/schema IDs beyond the inline members.
    _require(set(REQUIRED_SOURCE_CHECKS) <= parsed.checks.keys() and all(value is True for value in parsed.checks.values()),
             'Dry-run source checks did not all pass.')
    return _summary(parsed, configuration, _expected_counts(parsed), mode='dry_run')


def _assert_ids(conn, statement, parameters, expected, label):
    rows = conn.execute(statement, parameters).fetchall()
    _require(len(rows) == len(expected) and {row[0] for row in rows} == set(expected),
             f'Actual stored {label} IDs differ from the accepted records.')


def _verify_stored(conn, parsed, sources, fact_ids, occurrence_ids, document_set_id, reports, chunks, tables, issues):
    filing_id = parsed.filing_id
    rows = conn.execute('SELECT id,filing_id,role,source_url,sha256,size_bytes FROM sec.source_documents WHERE id=ANY(%s)',
                        (list(sources.values()),)).fetchall()
    expected = {(sources[record['source_url']], filing_id, record['role'], record['source_url'], record['sha256'], record['size_bytes'])
                for record in [parsed.manifest['index_document'], *parsed.manifest['files']]}
    _require(len(rows) == len(expected) and set(rows) == expected, 'Registered source IDs, roles or hashes differ from verified receipts.')
    rows = conn.execute('SELECT occurrence_key,id FROM sec.financial_facts WHERE filing_id=%s AND source_document_id=%s AND extraction_version=%s',
                        (filing_id, parsed.xml_document_id, VERSIONS['facts'])).fetchall()
    _require(len(rows) == len(fact_ids) and dict(rows) == fact_ids, 'Actual stored XML fact identities differ from the accepted occurrences.')
    rows = conn.execute('SELECT inline_id,id,fact_id,html_document_id FROM sec.fact_source_occurrences WHERE document_set_id=%s AND filing_id=%s',
                        (document_set_id, filing_id)).fetchall()
    expected_occurrences = {(record['inline_id'], occurrence_ids[record['inline_id']], fact_ids[record['inline_id']], UUID(record['inline_document_id']))
                            for record in parsed.occurrences}
    _require(len(rows) == len(expected_occurrences) and set(rows) == expected_occurrences, 'Actual source occurrence membership differs from the complete inline set.')
    for document in parsed.documents:
        report_id = reports[document.source_document_id]
        _assert_ids(conn, 'SELECT id FROM sec.reports WHERE filing_id=%s AND source_document_id=%s AND extraction_version=%s',
                    (filing_id, document.source_document_id, VERSIONS['reports']), [report_id], 'reports')
        _assert_ids(conn, 'SELECT id FROM sec.chunks WHERE filing_id=%s AND report_id=%s AND chunking_version=%s',
                    (filing_id, report_id, VERSIONS['chunks']), chunks[document.source_document_id], 'chunks')
        _assert_ids(conn, 'SELECT id FROM sec.financial_tables WHERE filing_id=%s AND source_document_id=%s AND extraction_version=%s',
                    (filing_id, document.source_document_id, VERSIONS['tables']), tables[document.source_document_id], 'tables')
    _assert_ids(conn, 'SELECT id FROM sec.source_quality_issues WHERE document_set_id=%s AND filing_id=%s',
                (document_set_id, filing_id), issues.values(), 'quality issues')


def run_ingestion(conn, *, manifest_path, data_root, processing_set_key='automated-v4', coverage=None):
    """Return only after one outer transaction seals and commits the entire set.

    The caller supplies an idle, non-autocommit connection. No preferred legacy
    selection is changed. Any metadata/write/reconstruction/sealing failure rolls
    back this transaction, including newly registered sources and companies.
    """
    _require(not conn.closed and not conn.autocommit and conn.info.transaction_status.name == 'IDLE',
             'V4 ingestion requires an idle open connection with autocommit=False.')
    _require(isinstance(processing_set_key, str) and bool(processing_set_key.strip()), 'V4 processing-set key is required.')
    path, root = Path(manifest_path).resolve(), Path(data_root).resolve()
    manifest = verify_document_set(path, root)
    digest = _digest(path)
    configuration = configuration_from_manifest(manifest, digest)
    core_path = (root / manifest['core_manifest']['local_path']).resolve()
    phase, summary = 'metadata', None
    try:
        with conn.transaction():
            seed = seed_manifest(conn, core_path, root)
            filing_id = seed['filing_id']
            _require(isinstance(filing_id, UUID), 'Metadata seeding returned no filing UUID.')
            require_current_receipt(conn, filing_id, processing_set_key, VERSIONS, document_set=True)
            sources = {}
            for record in [manifest['index_document'], *manifest['files']]:
                if record['role'] == SUPPLEMENTAL_ROLE:
                    identifier = register_source_document(conn, SourceDocumentInput(
                        filing_id=filing_id, role=record['role'], source_url=record['source_url'], local_path=record['local_path'],
                        sha256=record['sha256'], size_bytes=record['size_bytes'], retrieved_at=datetime.fromisoformat(record['retrieved_at_utc']),
                        title=Path(record['local_path']).name, document_type=record['document_type']))
                else:
                    identifier = seed['document_ids'][record['local_path']]
                _require(isinstance(identifier, UUID), 'A registered source lacks its authoritative UUID.')
                sources[record['source_url']] = identifier
            _require(len(set(sources.values())) == len(sources) and filing_id not in sources.values(), 'Registered source identities are not distinct.')
            _active(conn, phase)
            phase = 'parse and validate'
            parsed = parse_filing_set(path, root, filing_id=filing_id, source_document_ids=sources)
            _parsed_checks(parsed, manifest, digest, filing_id, sources)
            declared_coverage = dict(parsed.coverage) if coverage is None else dict(coverage)
            _require(declared_coverage == dict(parsed.coverage), 'Coverage must retain the parser-proven declaration without promotion.')
            for registrant in parsed.registrants:
                register_company(conn, CompanyInput(cik=registrant['cik'], name=registrant['name']))
            _active(conn, phase)
            phase = 'document set'
            members = {record['source_url']: record for record in manifest['files'] if record.get('is_primary') is not None}
            document_set_id = store_document_set(conn, DocumentSetInput(
                filing_id=filing_id, set_key=manifest['document_set']['set_key'], xml_document_id=parsed.xml_document_id,
                primary_html_document_id=sources[manifest['document_set']['primary_html_source_url']],
                index_document_id=sources[manifest['index_document']['source_url']], manifest_sha256=digest,
                evidence=deepcopy(manifest['document_set'])), [DocumentMemberInput(
                    source_document_id=sources[url], source_sha256=members[url]['sha256'], sequence=members[url]['sequence'],
                    document_type=members[url]['document_type'], is_primary=members[url]['is_primary'],
                    index_locator=deepcopy(members[url]['index_locator'])) for url in manifest['document_set']['member_source_urls']])
            _active(conn, phase)
            phase = 'facts'
            fact_ids = store_facts(conn, parsed.facts, bulk=True).fact_ids
            _require(set(fact_ids) == {fact.occurrence_key for fact in parsed.facts}
                     and len(set(fact_ids.values())) == len(fact_ids), 'Facts storage did not return every distinct source occurrence.')
            _active(conn, phase)
            phase = 'source occurrences'
            occurrence_records = [FactSourceOccurrenceInput(filing_id=filing_id, document_set_id=document_set_id,
                fact_id=fact_ids[item['inline_id']], html_document_id=UUID(item['inline_document_id']), inline_id=item['inline_id'],
                source_xpath=item['source_xpath'], evidence=deepcopy(item['evidence']), fragments=tuple(deepcopy(item['fragments'])))
                for item in parsed.occurrences]
            stored_occurrences = store_source_occurrences(conn, occurrence_records)
            _require(len(stored_occurrences) == len(occurrence_records) and len(set(stored_occurrences)) == len(stored_occurrences),
                     'Source occurrence storage did not return a complete unique inventory.')
            occurrence_ids = {record.inline_id: identifier for record, identifier in zip(occurrence_records, stored_occurrences)}
            _active(conn, phase)
            reports, chunks, tables, issues = {}, {}, {}, {}
            phase = 'reports and chunks'
            for document in parsed.documents:
                report_id = store_report(conn, document.report)
                reports[document.source_document_id] = report_id
                chunks[document.source_document_id] = store_chunks(conn, rebind_chunks(document, report_id), bulk=True)
                _require(len(chunks[document.source_document_id]) == len(document.chunks), 'Chunk storage returned an incomplete inventory.')
                _active(conn, phase)
            phase = 'tables and cells'
            for document in parsed.documents:
                tables[document.source_document_id] = []
                for table in document.tables:
                    table_id = store_financial_table(conn, table)
                    tables[document.source_document_id].append(table_id)
                    accepted = {reference['xml_id'] for row in table.structure['rows'] for cell in row for reference in cell['fact_references']}
                    store_many(conn, 'financial_table_facts', [dict(filing_id=filing_id, table_id=table_id, fact_id=fact_ids[key])
                               for key in sorted(accepted)], ('table_id', 'fact_id'), returning='fact_id')
                    stored = store_v4_table_cells(conn, table_id, table, fact_ids, occurrence_ids, document_set_id, bulk=True)
                    _require(not set(issues).intersection(stored.issue_ids), 'Stored issue identities overlap across tables.')
                    issues.update(stored.issue_ids)
                    _active(conn, phase)
            phase = 'fresh source reconstruction'
            validation = validate_filing_set(parsed, path, root, filing_id=filing_id, source_document_ids=sources)
            _require(dict(validation.checks) == dict(parsed.checks) and dict(validation.semantic_checks) == dict(parsed.semantic_checks)
                     and tuple(validation.issues) == tuple(parsed.issues), 'Final reconstruction differs from the accepted payload.')
            _parsed_checks(parsed, manifest, digest, filing_id, sources)
            _active(conn, phase)
            phase = 'stored inventory verification'
            _verify_stored(conn, parsed, sources, fact_ids, occurrence_ids, document_set_id, reports, chunks, tables, issues)
            _require(set(issues) == {issue['issue_key'] for issue in parsed.issues}, 'Stored quality issues differ from parsed source conflicts.')
            phase = 'source processing seal'
            set_id = store_v4_processing_set(conn, V4ProcessingSetInput(filing_id=filing_id, set_key=processing_set_key,
                document_set_id=document_set_id, source_checks=dict(validation.checks), semantic_checks=dict(validation.semantic_checks),
                coverage=declared_coverage, configuration=deepcopy(configuration)),
                table_ids=[identifier for values in tables.values() for identifier in values],
                reports=[ReportMembershipInput(report_id=identifier) for identifier in reports.values()], issue_ids=list(issues.values()))
            row = conn.execute('SELECT document_set_id,source_validation_status,validation_status,counts,configuration FROM sec.source_processing_sets WHERE id=%s AND filing_id=%s',
                               (set_id, filing_id)).fetchone()
            _require(row is not None and row[0] == document_set_id and row[1] == 'passed' and row[2] == _status(parsed)
                     and row[3] == _expected_counts(parsed) and row[4] == configuration,
                     'Sealed source-set scope, status, counts or configuration differs from actual accepted records.')
            _require(verify_document_set(path, root) == manifest and _digest(path) == digest,
                     'Source documents or document-set manifest changed before commit.')
            _active(conn, phase)
            summary = _summary(parsed, configuration, deepcopy(row[3]), mode='ingest')
            summary.update(filing_id=str(filing_id), document_set_id=str(document_set_id), processing_set_id=str(set_id),
                           contract_version=2, source_document_ids={url: str(identifier) for url, identifier in sources.items()})
            json.dumps(summary)
            phase = 'commit'
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError(f'V4 ingestion failed during {phase}.') from exc
    _require(summary is not None and not conn.closed and conn.info.transaction_status.name == 'IDLE',
             'V4 ingestion did not complete its outer commit.')
    return summary
