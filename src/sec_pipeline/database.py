"""PostgreSQL storage contracts. Write helpers never commit the caller's transaction."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb


class DatabaseError(ValueError):
    """Invalid configuration, migration, or local input."""


class DataConflict(DatabaseError):
    """An existing identity has different content; use a new version."""


@dataclass(frozen=True, kw_only=True)
class CompanyInput:
    cik: str
    name: str
    ticker: str | None = None


@dataclass(frozen=True, kw_only=True)
class FilingInput:
    company_id: UUID
    accession_number: str
    form: str
    filing_date: date
    reporting_period_end: date | None = None
    amendment_of_id: UUID | None = None
    source_url: str | None = None


@dataclass(frozen=True, kw_only=True)
class SourceDocumentInput:
    filing_id: UUID
    role: str
    source_url: str
    local_path: str
    sha256: str
    size_bytes: int
    retrieved_at: datetime


@dataclass(frozen=True, kw_only=True)
class ReportInput:
    filing_id: UUID
    source_document_id: UUID
    extraction_version: str
    full_text: str
    sections: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class ChunkInput:
    filing_id: UUID
    report_id: UUID
    chunking_version: str
    chunk_index: int
    text: str
    offset_start: int
    offset_end: int
    section: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class FactInput:
    filing_id: UUID
    source_document_id: UUID
    extraction_version: str
    occurrence_key: str
    concept_namespace: str
    concept_name: str
    period_kind: str
    label: str | None = None
    numeric_value: Decimal | None = None
    raw_value: str | None = None
    is_nil: bool = False
    reported_decimals: str | None = None
    reported_precision: str | None = None
    unit: dict[str, Any] | None = None
    period_start: date | None = None
    period_end: date | None = None
    instant_date: date | None = None
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    source_reference: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class FinancialTableInput:
    filing_id: UUID
    source_document_id: UUID
    extraction_version: str
    table_key: str
    statement_type: str
    structure: dict[str, Any]
    readable_text: str


def connection_settings(env_file: Path | str | None = Path('.env')) -> dict[str, Any]:
    """Read simple KEY=value configuration; process environment wins per key."""
    settings: dict[str, str] = {}
    if env_file is not None:
        path = Path(env_file)
        if path.exists():
            for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                key, separator, value = line.partition('=')
                if not separator or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
                    raise DatabaseError(f'Invalid environment file syntax at line {number}.')
                value = value.strip()
                if value.startswith(('"', "'")):
                    if len(value) < 2 or value[-1] != value[0]:
                        raise DatabaseError(f'Unclosed quote at line {number}.')
                    value = value[1:-1]
                settings[key.strip()] = value
    settings.update(os.environ)
    password = settings.get('PGPASSWORD', settings.get('POSTGRES_PASSWORD', ''))
    if not password or password == 'CHANGE_ME':
        raise DatabaseError('Set a non-placeholder POSTGRES_PASSWORD or PGPASSWORD.')
    try:
        port = int(settings.get('PGPORT', settings.get('POSTGRES_PORT', '5432')))
    except ValueError as exc:
        raise DatabaseError('Database port must be an integer.') from exc
    if not 1 <= port <= 65535:
        raise DatabaseError('Database port must be between 1 and 65535.')
    connection: dict[str, Any] = {
        'host': settings.get('PGHOST', '127.0.0.1'),
        'port': port,
        'dbname': settings.get('PGDATABASE', settings.get('POSTGRES_DB', 'sec_filings')),
        'user': settings.get('PGUSER', settings.get('POSTGRES_USER', 'sec_admin')),
        'password': password,
        'connect_timeout': 10,
        'application_name': 'sec-pipeline',
    }
    sslmode = settings.get('PGSSLMODE')
    if sslmode is not None:
        if sslmode not in {'disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'}:
            raise DatabaseError(
                'PGSSLMODE must be disable, allow, prefer, require, verify-ca, or verify-full.'
            )
        connection['sslmode'] = sslmode
    if settings.get('PGSSLROOTCERT'):
        connection['sslrootcert'] = settings['PGSSLROOTCERT']
    return connection


def connect(env_file: Path | str | None = Path('.env'), **overrides: Any) -> psycopg.Connection:
    """Open a connection; use a context manager to commit or roll back on exit."""
    return psycopg.connect(**(connection_settings(env_file) | overrides))


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply the packaged migration batch atomically and reject history drift."""
    migrations = sorted(
        (resource.name, resource.read_bytes())
        for resource in files('sec_pipeline.migrations').iterdir()
        if re.fullmatch(r'\d{3}_[a-z0-9_]+\.sql', resource.name)
    )
    if not migrations:
        raise DatabaseError('Packaged SQL migrations are missing.')
    applied_now: list[str] = []
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(170017, 1)')
        conn.execute('CREATE SCHEMA IF NOT EXISTS sec')
        conn.execute('''CREATE TABLE IF NOT EXISTS sec.schema_migrations (
            name text PRIMARY KEY, sha256 text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now())''')
        applied = dict(conn.execute('SELECT name, sha256 FROM sec.schema_migrations').fetchall())
        packaged = {name for name, _ in migrations}
        if set(applied) - packaged:
            raise DatabaseError('Database contains migrations absent from this package.')
        pending_seen = False
        for name, content in migrations:
            digest = hashlib.sha256(content).hexdigest()
            if name in applied:
                if applied[name] != digest:
                    raise DatabaseError(f'Applied migration checksum changed: {name}')
                if pending_seen:
                    raise DatabaseError('Migration history is not a contiguous prefix.')
            else:
                pending_seen = True
        for name, content in migrations:
            if name in applied:
                continue
            conn.execute(content.decode('utf-8'))
            conn.execute('INSERT INTO sec.schema_migrations (name, sha256) VALUES (%s, %s)',
                         (name, hashlib.sha256(content).hexdigest()))
            applied_now.append(name)
    return applied_now


_JSON_FIELDS = {'sections', 'metadata', 'unit', 'dimensions', 'source_reference', 'structure'}


def _value(name: str, value: Any) -> Any:
    return Jsonb(value) if name in _JSON_FIELDS and value is not None else value


def _store(conn: psycopg.Connection, table: str, record: Any, keys: tuple[str, ...]) -> UUID:
    """Insert once; compare all supplied fields on replay rather than overwriting."""
    if conn.autocommit:
        raise DatabaseError('Write helpers require autocommit=False and caller-managed transactions.')
    data = asdict(record)
    columns = list(data)
    values = [_value(name, data[name]) for name in columns]
    statement = sql.SQL('INSERT INTO sec.{} ({}) VALUES ({}) ON CONFLICT ({}) DO NOTHING RETURNING id').format(
        sql.Identifier(table), sql.SQL(', ').join(map(sql.Identifier, columns)),
        sql.SQL(', ').join(sql.Placeholder() for _ in columns),
        sql.SQL(', ').join(map(sql.Identifier, keys)),
    )
    row = conn.execute(statement, values).fetchone()
    if row:
        return row[0]
    identical = sql.SQL(' AND ').join(
        sql.SQL('{} IS NOT DISTINCT FROM %s').format(sql.Identifier(name)) for name in columns
    )
    row = conn.execute(sql.SQL('SELECT id FROM sec.{} WHERE {} FOR SHARE').format(
        sql.Identifier(table), identical), values).fetchone()
    if row:
        return row[0]
    raise DataConflict(f'Conflicting {table} identity; preserve the original and use a new version.')


def register_company(conn: psycopg.Connection, record: CompanyInput) -> UUID:
    return _store(conn, 'companies', record, ('cik',))


def register_filing(conn: psycopg.Connection, record: FilingInput) -> UUID:
    return _store(conn, 'filings', record, ('accession_number',))


def register_source_document(conn: psycopg.Connection, record: SourceDocumentInput) -> UUID:
    if record.retrieved_at.tzinfo is None or record.retrieved_at.utcoffset() is None:
        raise DatabaseError('retrieved_at must include a timezone.')
    return _store(conn, 'source_documents', record, ('filing_id', 'local_path'))


def store_report(conn: psycopg.Connection, record: ReportInput) -> UUID:
    return _store(conn, 'reports', record, ('source_document_id', 'extraction_version'))


def store_chunk(conn: psycopg.Connection, record: ChunkInput) -> UUID:
    return _store(conn, 'chunks', record, ('report_id', 'chunking_version', 'chunk_index'))


def store_fact(conn: psycopg.Connection, record: FactInput) -> UUID:
    if record.numeric_value is not None:
        if not isinstance(record.numeric_value, Decimal) or not record.numeric_value.is_finite():
            raise DatabaseError('numeric_value must be a finite Decimal, never float.')
    return _store(conn, 'financial_facts', record,
                  ('source_document_id', 'extraction_version', 'occurrence_key'))


def store_financial_table(conn: psycopg.Connection, record: FinancialTableInput) -> UUID:
    return _store(conn, 'financial_tables', record,
                  ('source_document_id', 'extraction_version', 'table_key'))


def link_table_fact(conn: psycopg.Connection, filing_id: UUID, table_id: UUID, fact_id: UUID) -> None:
    if conn.autocommit:
        raise DatabaseError('Write helpers require autocommit=False and caller-managed transactions.')
    conn.execute('''INSERT INTO sec.financial_table_facts (filing_id, table_id, fact_id)
        VALUES (%s, %s, %s) ON CONFLICT (table_id, fact_id) DO NOTHING''',
                 (filing_id, table_id, fact_id))
    row = conn.execute('''SELECT filing_id FROM sec.financial_table_facts
        WHERE table_id = %s AND fact_id = %s''', (table_id, fact_id)).fetchone()
    if row is None or row[0] != filing_id:
        raise DataConflict('Table/fact relationship has a different filing.')


def _verified_manifest(manifest_path: Path, data_root: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict):
        raise DatabaseError('The source manifest must be a JSON object.')
    if manifest.get('schema_version') != 1 or manifest.get('status') != 'verified':
        raise DatabaseError('Expected a verified version 1 source manifest.')
    for key in ('company', 'cik', 'accession_number', 'form', 'filing_date',
                'reporting_period_end', 'filing_index_url'):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise DatabaseError(f'Missing manifest field: {key}')
    if not isinstance(manifest.get('files'), list) or not isinstance(manifest.get('index_document'), dict):
        raise DatabaseError('The manifest requires a files array and index_document object.')
    records = [manifest['index_document'], *manifest['files']]
    if not all(isinstance(record, dict) for record in records):
        raise DatabaseError('Every source document must be a JSON object.')
    roles = {'filing_index', 'main_inline_xbrl_filing', 'extracted_xbrl_instance', 'xbrl_extension_schema'}
    if len(records) != 4 or {r.get('role') for r in records} != roles:
        raise DatabaseError('The sample manifest must describe its four source documents.')
    root = data_root.resolve()
    seen: set[Path] = set()
    for record in records:
        for key in ('local_path', 'source_url', 'sha256', 'retrieved_at_utc'):
            if not isinstance(record.get(key), str) or not record[key]:
                raise DatabaseError(f'Missing source field: {key}')
        verification = record.get('verification')
        if (record.get('status') != 'verified' or not isinstance(verification, dict)
                or verification.get('result') != 'passed'):
            raise DatabaseError('Every source document must have passed verification.')
        relative = Path(record['local_path'])
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root) or path in seen:
            raise DatabaseError('Source paths must be distinct and contained within the data root.')
        seen.add(path)
        if not path.is_file():
            raise DatabaseError(f'Missing source document: {relative.name}')
        digest = hashlib.sha256()
        size = 0
        with path.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
                size += len(block)
        if record.get('size_bytes') != size or record['sha256'] != digest.hexdigest():
            raise DatabaseError(f'Source size or SHA-256 mismatch: {relative.name}')
    return manifest


def seed_manifest(conn: psycopg.Connection, manifest_path: Path, data_root: Path) -> dict[str, Any]:
    """Verify local bytes, then register metadata atomically. Never fetch or extract."""
    manifest = _verified_manifest(Path(manifest_path), Path(data_root))
    with conn.transaction():
        company_id = register_company(conn, CompanyInput(cik=manifest['cik'], name=manifest['company']))
        filing_id = register_filing(conn, FilingInput(
            company_id=company_id, accession_number=manifest['accession_number'],
            form=manifest['form'], filing_date=date.fromisoformat(manifest['filing_date']),
            reporting_period_end=date.fromisoformat(manifest['reporting_period_end']),
            source_url=manifest['filing_index_url'],
        ))
        document_ids = {}
        for record in [manifest['index_document'], *manifest['files']]:
            document_ids[record['local_path']] = register_source_document(conn, SourceDocumentInput(
                filing_id=filing_id, role=record['role'], source_url=record['source_url'],
                local_path=record['local_path'], sha256=record['sha256'], size_bytes=record['size_bytes'],
                retrieved_at=datetime.fromisoformat(record['retrieved_at_utc']),
            ))
    return {'company_id': company_id, 'filing_id': filing_id, 'document_ids': document_ids}


def status(conn: psycopg.Connection) -> dict[str, Any]:
    """Return connection, migration and row-count information without credentials."""
    result: dict[str, Any] = {'database': conn.info.dbname, 'server_version': conn.info.server_version}
    if conn.execute("SELECT to_regclass('sec.schema_migrations')").fetchone()[0] is None:
        return result | {'migrations': [], 'counts': {}, 'ready': False}
    result['migrations'] = [row[0] for row in conn.execute(
        'SELECT name FROM sec.schema_migrations ORDER BY name').fetchall()]
    result['counts'] = {}
    for table in ('companies', 'filings', 'source_documents', 'reports', 'chunks',
                  'financial_facts', 'financial_tables', 'financial_table_facts'):
        if conn.execute('SELECT to_regclass(%s)', (f'sec.{table}',)).fetchone()[0] is None:
            result['counts'][table] = None
        else:
            result['counts'][table] = conn.execute(sql.SQL('SELECT count(*) FROM sec.{}').format(
                sql.Identifier(table))).fetchone()[0]
    result['ready'] = bool(result['migrations']) and all(v is not None for v in result['counts'].values())
    return result
