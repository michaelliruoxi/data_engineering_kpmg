"""Immutable V4 document sets, source bindings, and explicit quality assessments.

These helpers own no connection or outer transaction. Source readers establish
the HTML evidence; database guards additionally enforce exact stored scope,
value correspondence, complete membership, and immutable replay.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from .database import DataConflict, DatabaseError


POLICY = 'inline-set-v1'
VERSIONS = {'facts': 'facts-v1', 'tables': 'tables-v4', 'reports': 'report-v1', 'chunks': 'paragraph-v1'}
SOURCE_CHECKS = frozenset({'document_set_complete', 'union_correspondence', 'numeric_values_compared',
                          'source_cells_complete', 'source_bindings_exact', 'reports_complete', 'chunks_complete'})
ISSUE_CODES = frozenset({'instant_context_under_duration_header', 'opening_balance_date_not_prior_day'})


def _uuid(value, label):
    if not isinstance(value, UUID):
        raise DatabaseError(f'{label} must be a UUID.')


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise DatabaseError(f'{label} must be nonempty text.')


def _hash(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise DatabaseError(f'{label} must be a lowercase SHA-256.')


def _object(value, label):
    if not isinstance(value, dict):
        raise DatabaseError(f'{label} must be a JSON object.')
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise DatabaseError(f'{label} must contain finite JSON values.') from exc


@dataclass(frozen=True, kw_only=True)
class DocumentSetInput:
    filing_id: UUID
    set_key: str
    xml_document_id: UUID
    primary_html_document_id: UUID
    index_document_id: UUID
    manifest_sha256: str
    evidence: dict[str, Any]
    policy_version: str = POLICY

    def __post_init__(self):
        for name in ('filing_id', 'xml_document_id', 'primary_html_document_id', 'index_document_id'):
            _uuid(getattr(self, name), name)
        if len({self.xml_document_id, self.primary_html_document_id, self.index_document_id}) != 3:
            raise DatabaseError('Index, primary HTML, and target XML must be distinct sources.')
        _text(self.set_key, 'set_key')
        _hash(self.manifest_sha256, 'manifest_sha256')
        _object(self.evidence, 'evidence')
        if self.policy_version != POLICY:
            raise DatabaseError('Unsupported document-set policy.')
        checks = self.evidence.get('source_checks')
        urls = self.evidence.get('member_source_urls')
        if (not isinstance(checks, dict) or any(checks.get(key) is not True for key in
                ('index_membership_complete','single_primary_target','excluded_targets_separate'))
                or self.evidence.get('inline_target') != '' or not isinstance(urls, list) or not urls
                or any(not isinstance(url, str) or not url for url in urls) or len(set(urls)) != len(urls)
                or not isinstance(self.evidence.get('excluded_inline_documents'), list)):
            raise DatabaseError('Document-set input requires explicit complete default-target membership proof.')


@dataclass(frozen=True, kw_only=True)
class DocumentMemberInput:
    source_document_id: UUID
    source_sha256: str
    sequence: int
    document_type: str
    is_primary: bool
    index_locator: dict[str, Any]

    def __post_init__(self):
        _uuid(self.source_document_id, 'source_document_id')
        _hash(self.source_sha256, 'source_sha256')
        if type(self.sequence) is not int or self.sequence < 1:
            raise DatabaseError('Document sequence must be a positive integer.')
        _text(self.document_type, 'document_type')
        if self.document_type.upper() in {'EX-FILING FEES', 'EX-98'}:
            raise DatabaseError('Separate target exhibits cannot join the primary inline set.')
        if type(self.is_primary) is not bool:
            raise DatabaseError('is_primary must be a boolean.')
        _object(self.index_locator, 'index_locator')
        _text(self.index_locator.get('source_xpath'), 'index locator XPath')
        _hash(self.index_locator.get('source_sha256'), 'index locator source hash')


@dataclass(frozen=True, kw_only=True)
class FactSourceOccurrenceInput:
    filing_id: UUID
    document_set_id: UUID
    fact_id: UUID
    html_document_id: UUID
    inline_id: str
    source_xpath: str
    evidence: dict[str, Any]
    fragments: tuple[dict[str, Any], ...] = ()

    def __post_init__(self):
        for name in ('filing_id', 'document_set_id', 'fact_id', 'html_document_id'):
            _uuid(getattr(self, name), name)
        _text(self.inline_id, 'inline_id')
        _text(self.source_xpath, 'source_xpath')
        _object(self.evidence, 'evidence')
        required = {'xml_id', 'concept', 'context_ref', 'unit_ref', 'unit', 'period', 'dimensions',
                    'raw_value', 'is_nil', 'inline_mapped_value', 'inline_is_nil', 'html_sha256', 'xml_sha256'}
        if not required <= self.evidence.keys() or self.evidence['xml_id'] != self.inline_id:
            raise DatabaseError('An occurrence requires its complete source-to-XML evidence.')
        for name in ('html_sha256', 'xml_sha256'):
            _hash(self.evidence[name], name)
        if type(self.evidence['is_nil']) is not bool or self.evidence['inline_is_nil'] is not self.evidence['is_nil']:
            raise DatabaseError('Inline and XML nil states must agree exactly.')
        if self.evidence.get('value_comparison', 'exact') not in {'exact','escaped-markup-infoset-v1'}:
            raise DatabaseError('Unsupported source value comparator.')
        if not self.evidence['is_nil'] and (not isinstance(self.evidence['raw_value'], str)
                or not isinstance(self.evidence['inline_mapped_value'], str)):
            raise DatabaseError('Non-nil occurrence values must be exact strings.')
        if self.evidence['is_nil']:
            if (self.evidence['inline_mapped_value'] is not None or self.evidence['raw_value'] not in (None, '')
                    or self.evidence.get('value_comparison', 'exact') != 'exact'):
                raise DatabaseError('A nil occurrence cannot acquire a value.')
        elif self.evidence.get('value_comparison', 'exact') == 'escaped-markup-infoset-v1':
            if (self.evidence['unit'] is not None or self.evidence.get('inline_kind') != 'nonnumeric'
                    or self.evidence.get('inline_escape') is not True):
                raise DatabaseError('Escaped-markup comparison requires an explicit nonnumeric escaped source.')
            for name in ('inline_canonical_sha256', 'xml_canonical_sha256'):
                _hash(self.evidence.get(name), name)
            if self.evidence['inline_canonical_sha256'] != self.evidence['xml_canonical_sha256']:
                raise DatabaseError('Escaped source and XML markup must have identical canonical content.')
        elif self.evidence.get('value_comparison', 'exact') != 'exact':
            raise DatabaseError('Unsupported source value comparator.')
        elif self.evidence['unit'] is not None:
            try:
                left, right = Decimal(self.evidence['inline_mapped_value']), Decimal(self.evidence['raw_value'])
            except (TypeError, InvalidOperation, ValueError) as exc:
                raise DatabaseError('Numeric occurrence values must be finite source decimals.') from exc
            if not left.is_finite() or not right.is_finite() or left != right:
                raise DatabaseError('Inline/XML numeric mismatches cannot be stored as review issues.')
        elif self.evidence['inline_mapped_value'] != self.evidence['raw_value']:
            raise DatabaseError('Inline/XML text correspondence must be exact.')
        if not isinstance(self.fragments, tuple):
            raise DatabaseError('Occurrence fragments must be an ordered tuple.')
        for index, fragment in enumerate(self.fragments):
            _object(fragment, 'fragment')
            if set(fragment) != {'source_document_id', 'source_xpath', 'fragment_order'} or fragment['fragment_order'] != index:
                raise DatabaseError('Fragments require consecutive explicit source locations.')
            try:
                UUID(fragment['source_document_id'])
            except (ValueError, TypeError, AttributeError) as exc:
                raise DatabaseError('Fragment source_document_id must be a UUID string.') from exc
            _text(fragment['source_xpath'], 'fragment source XPath')


@dataclass(frozen=True, kw_only=True)
class ReviewBindingInput:
    filing_id: UUID
    document_set_id: UUID
    table_id: UUID
    cell_id: UUID
    fact_id: UUID
    occurrence_id: UUID
    reference_order: int
    evidence: dict[str, Any]

    def __post_init__(self):
        for name in ('filing_id', 'document_set_id', 'table_id', 'cell_id', 'fact_id', 'occurrence_id'):
            _uuid(getattr(self, name), name)
        if type(self.reference_order) is not int or self.reference_order < 0:
            raise DatabaseError('Source reference order must be nonnegative.')
        _object(self.evidence, 'evidence')


@dataclass(frozen=True, kw_only=True)
class QualityIssueInput:
    filing_id: UUID
    document_set_id: UUID
    issue_key: str
    binding_id: UUID
    code: str
    expected: dict[str, Any]
    observed: dict[str, Any]
    evidence: dict[str, Any]
    rule_version: str = 'cash-period-review-v1'

    def __post_init__(self):
        for name in ('filing_id', 'document_set_id', 'binding_id'):
            _uuid(getattr(self, name), name)
        _text(self.issue_key, 'issue_key')
        if self.code not in ISSUE_CODES or self.rule_version != 'cash-period-review-v1':
            raise DatabaseError('Unsupported source-quality rule.')
        for name in ('expected', 'observed', 'evidence'):
            _object(getattr(self, name), name)
        if self.expected == self.observed:
            raise DatabaseError('A quality issue must retain distinct expected and observed states.')


@dataclass(frozen=True, kw_only=True)
class ReportMembershipInput:
    report_id: UUID
    chunking_version: str = 'paragraph-v1'

    def __post_init__(self):
        _uuid(self.report_id, 'report_id')
        if self.chunking_version != VERSIONS['chunks']:
            raise DatabaseError('Unsupported V4 chunking version.')


@dataclass(frozen=True, kw_only=True)
class V4ProcessingSetInput:
    filing_id: UUID
    set_key: str
    document_set_id: UUID
    source_checks: dict[str, bool]
    semantic_checks: dict[str, bool]
    coverage: dict[str, str]
    configuration: dict[str, Any]
    versions: dict[str, str] = field(default_factory=lambda: dict(VERSIONS))

    def __post_init__(self):
        _uuid(self.filing_id, 'filing_id')
        _uuid(self.document_set_id, 'document_set_id')
        _text(self.set_key, 'set_key')
        if self.versions != VERSIONS:
            raise DatabaseError('V4 selection requires exact supported artifact versions.')
        if (not isinstance(self.source_checks, dict) or not SOURCE_CHECKS <= self.source_checks.keys()
                or any(not isinstance(key, str) or not key.strip() or value is not True
                       for key, value in self.source_checks.items())):
            raise DatabaseError('Every V4 source fidelity check must explicitly pass.')
        if (not isinstance(self.semantic_checks, dict) or not self.semantic_checks
                or 'period_header_consistency' not in self.semantic_checks
                or any(not isinstance(key, str) or not key.strip() or type(value) is not bool
                       for key, value in self.semantic_checks.items())):
            raise DatabaseError('Semantic checks require explicit named boolean results.')
        if (not isinstance(self.coverage, dict) or set(self.coverage) != set(VERSIONS)
                or any(value not in ({'unknown', 'partial', 'complete'} if key == 'reports' else {'partial', 'complete'})
                       for key, value in self.coverage.items())):
            raise DatabaseError('V4 requires declared non-absent coverage for every stage.')
        _object(self.configuration, 'configuration')


def _checked(record, expected):
    if not isinstance(record, expected):
        raise DatabaseError(f'Expected {expected.__name__}.')
    data = asdict(record)
    expected(**data)
    return data


def _begin(conn, filing_id):
    if conn.autocommit:
        raise DatabaseError('V4 writes require caller-managed transactions.')
    conn.execute('SELECT sec.lock_processing_filing(%s)', (filing_id,))


def _adapt(value):
    return Jsonb(value) if isinstance(value, (dict, list, tuple)) else value


def _lookup(conn, table, values, keys):
    query = sql.SQL('SELECT id FROM sec.{} WHERE {}').format(sql.Identifier(table), sql.SQL(' AND ').join(
        sql.SQL('{} = %s').format(sql.Identifier(key)) for key in keys))
    row = conn.execute(query, [_adapt(values[key]) for key in keys]).fetchone()
    return row[0] if row else None


def _exact(conn, table, values, keys):
    identifier = _lookup(conn, table, values, keys)
    if identifier is not None:
        query = sql.SQL('SELECT id FROM sec.{} WHERE id=%s AND {}').format(sql.Identifier(table), sql.SQL(' AND ').join(
            sql.SQL('{} IS NOT DISTINCT FROM %s').format(sql.Identifier(name)) for name in values))
        if conn.execute(query, [identifier, *(_adapt(value) for value in values.values())]).fetchone() is None:
            raise DataConflict(f'Conflicting immutable {table} content.')
        return identifier
    columns = tuple(values)
    query = sql.SQL('INSERT INTO sec.{} ({}) VALUES ({}) RETURNING id').format(
        sql.Identifier(table), sql.SQL(', ').join(map(sql.Identifier, columns)),
        sql.SQL(', ').join(sql.Placeholder() for _ in columns))
    return conn.execute(query, [_adapt(values[name]) for name in columns]).fetchone()[0]


def store_document_set(conn, record: DocumentSetInput, members: Sequence[DocumentMemberInput]) -> UUID:
    data = _checked(record, DocumentSetInput)
    members = [_checked(member, DocumentMemberInput) for member in members]
    if (not members or len({item['source_document_id'] for item in members}) != len(members)
            or len({item['sequence'] for item in members}) != len(members)
            or [item['source_document_id'] for item in members if item['is_primary']] != [record.primary_html_document_id]):
        raise DatabaseError('Document membership must be unique and contain exactly the declared primary HTML.')
    _begin(conn, record.filing_id)
    existing = _lookup(conn, 'source_document_sets', data, ('filing_id', 'set_key'))
    identifier = existing or uuid4()
    with conn.transaction():
        if existing:
            actual = conn.execute('SELECT source_document_id FROM sec.source_document_set_members WHERE document_set_id=%s',
                                  (identifier,)).fetchall()
            if {row[0] for row in actual} != {member['source_document_id'] for member in members}:
                raise DataConflict('Document-set replay cannot omit or add members.')
            _exact(conn, 'source_document_sets', data, ('filing_id', 'set_key'))
        for member in members:
            _exact(conn, 'source_document_set_members', {'document_set_id': identifier,
                   'filing_id': record.filing_id, **member}, ('document_set_id', 'source_document_id'))
        actual = conn.execute('SELECT source_document_id FROM sec.source_document_set_members WHERE document_set_id=%s',
                              (identifier,)).fetchall()
        if {row[0] for row in actual} != {member['source_document_id'] for member in members}:
            raise DataConflict('Document-set replay cannot omit or add members.')
        if existing:
            return _exact(conn, 'source_document_sets', data, ('filing_id', 'set_key'))
        return _exact(conn, 'source_document_sets', {'id': identifier, **data}, ('filing_id', 'set_key'))


def _store_records(conn, records, expected, table, keys):
    items = [_checked(record, expected) for record in records]
    if not items:
        return []
    filings = {item['filing_id'] for item in items}
    if len(filings) != 1:
        raise DatabaseError('A V4 storage batch must belong to one filing.')
    _begin(conn, next(iter(filings)))
    with conn.transaction():
        return [_exact(conn, table, item, keys) for item in items]


def store_source_occurrences(conn, records: Sequence[FactSourceOccurrenceInput], *, batch_size=100) -> list[UUID]:
    """Bounded pipelines with indexed immutable replay; no per-fact round trip."""
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise DatabaseError('Occurrence pipeline batch_size must be between 1 and 100.')
    items = [_checked(record, FactSourceOccurrenceInput) for record in records]
    if not items:
        return []
    if len({item['filing_id'] for item in items}) != 1:
        raise DatabaseError('An occurrence batch must belong to one filing.')
    _begin(conn, items[0]['filing_id'])
    columns = tuple(items[0])
    keys = ('document_set_id', 'fact_id')
    insert = sql.SQL('INSERT INTO sec.fact_source_occurrences ({}) VALUES ({}) ON CONFLICT ({}) DO NOTHING RETURNING id').format(
        sql.SQL(', ').join(map(sql.Identifier, columns)), sql.SQL(', ').join(sql.Placeholder() for _ in columns),
        sql.SQL(', ').join(map(sql.Identifier, keys)))
    identical = sql.SQL('SELECT id FROM sec.fact_source_occurrences WHERE {} FOR SHARE').format(sql.SQL(' AND ').join(
        sql.SQL('{} = %s' if name in keys else '{} IS NOT DISTINCT FROM %s').format(sql.Identifier(name)) for name in columns))
    result = []
    with conn.transaction():
        for offset in range(0, len(items), batch_size):
            bound = [[_adapt(item[name]) for name in columns] for item in items[offset:offset + batch_size]]
            with conn.pipeline():
                inserted = [conn.execute(insert, values) for values in bound]
            rows = [cursor.fetchone() for cursor in inserted]
            conflicts = [index for index, row in enumerate(rows) if row is None]
            if conflicts:
                with conn.pipeline():
                    checked = [conn.execute(identical, bound[index]) for index in conflicts]
                for index, cursor in zip(conflicts, checked):
                    rows[index] = cursor.fetchone()
                    if rows[index] is None:
                        raise DataConflict('Conflicting immutable fact source occurrence; use a new source-set identity.')
            result.extend(row[0] for row in rows)
    return result


def store_review_bindings(conn, records: Sequence[ReviewBindingInput]) -> list[UUID]:
    return _store_records(conn, records, ReviewBindingInput, 'cell_source_fact_bindings', ('cell_id', 'reference_order'))


def store_quality_issues(conn, records: Sequence[QualityIssueInput]) -> list[UUID]:
    return _store_records(conn, records, QualityIssueInput, 'source_quality_issues', ('document_set_id', 'issue_key'))


def store_v4_processing_set(conn, record: V4ProcessingSetInput, *, table_ids: Sequence[UUID],
                            reports: Sequence[ReportMembershipInput], issue_ids: Sequence[UUID] = ()) -> UUID:
    """Atomically seal explicit members and pre-created immutable quality issues.

    SQL derives counts/status and checks all selected artifacts. No preferred
    selection is changed. The caller retains the outer commit/rollback decision.
    """
    data = _checked(record, V4ProcessingSetInput)
    tables, issues = list(table_ids), list(issue_ids)
    report_data = [_checked(report, ReportMembershipInput) for report in reports]
    for identifier in [*tables, *issues]:
        _uuid(identifier, 'member identifier')
    if (not tables or not report_data or len(set(tables)) != len(tables) or len(set(issues)) != len(issues)
            or len({item['report_id'] for item in report_data}) != len(report_data)):
        raise DatabaseError('V4 processing membership must be nonempty and unique.')
    _begin(conn, record.filing_id)
    existing = _lookup(conn, 'source_processing_sets', data, ('filing_id', 'set_key'))
    identifier = existing or uuid4()
    specifications = [('source_processing_tables', 'table_id', [{'table_id': value} for value in tables]),
                      ('source_processing_reports', 'report_id', report_data),
                      ('source_processing_issues', 'issue_id', [{'issue_id': value} for value in issues])]
    with conn.transaction():
        for table, key, entries in specifications:
            query = sql.SQL('SELECT {} FROM sec.{} WHERE processing_set_id=%s').format(sql.Identifier(key), sql.Identifier(table))
            if existing:
                actual = {row[0] for row in conn.execute(query, (identifier,)).fetchall()}
                if actual != {entry[key] for entry in entries}:
                    raise DataConflict('V4 processing replay must retain every selected member.')
            for entry in entries:
                _exact(conn, table, {'processing_set_id': identifier, 'filing_id': record.filing_id, **entry},
                       ('processing_set_id', key))
            actual = {row[0] for row in conn.execute(query, (identifier,)).fetchall()}
            if actual != {entry[key] for entry in entries}:
                raise DataConflict('V4 processing replay must retain every selected member.')
        if existing:
            return _exact(conn, 'source_processing_sets', data, ('filing_id', 'set_key'))
        return _exact(conn, 'source_processing_sets', {'id': identifier, **data}, ('filing_id', 'set_key'))
