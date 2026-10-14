"""Bounded pipelined inserts with immutable replay checks.

Callers retain ownership of the transaction, domain validation, section lookup,
and table-cell consistency checks. A raised error requires caller rollback; this
helper neither commits nor recovers an aborted transaction. Input order and
duplicate identities are preserved, with PostgreSQL deciding exact equality.
"""

from collections.abc import Mapping
from dataclasses import asdict, fields
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .database import ChunkInput, DataConflict, DatabaseError, FactInput, _JSON_FIELDS
from .table_cells import TableCellInput


MAX_BATCH_SIZE = 100
_JSON_COLUMNS = _JSON_FIELDS | {'source_locator', 'column_headings', 'column_period', 'evidence'}
_MAPPING_COLUMNS = {
    'cell_fact_links': ('filing_id', 'table_id', 'cell_id', 'fact_id', 'reference_order', 'link_method', 'evidence'),
    'financial_table_facts': ('filing_id', 'table_id', 'fact_id'),
}
_CONTRACTS = {
    'financial_facts': (FactInput, ('source_document_id', 'extraction_version', 'occurrence_key'), 'id'),
    'chunks': (ChunkInput, ('report_id', 'chunking_version', 'chunk_index'), 'id'),
    'table_cells': (TableCellInput, ('table_id', 'cell_key'), 'id'),
    'cell_fact_links': (None, ('cell_id', 'fact_id'), 'fact_id'),
    'financial_table_facts': (None, ('table_id', 'fact_id'), 'fact_id'),
}


def store_many(
    conn: psycopg.Connection,
    table: str,
    records,
    keys: tuple[str, ...],
    *,
    returning: str = 'id',
    batch_size: int = MAX_BATCH_SIZE,
) -> list[UUID]:
    """Insert immutable records in bounded pipelines, returning IDs in input order.

    Supported records are FactInput, ChunkInput, TableCellInput, and complete
    cell_fact_links/financial_table_facts mappings. Chunks must already have any required section ID.
    All input contracts are checked before SQL; JSON payloads and queued cursors
    are built for at most ``batch_size`` records at a time. Identical duplicates
    reuse their IDs; changed duplicates raise DataConflict, including duplicates
    spanning batches. No INSERT uses UPDATE or replaces existing content.
    """
    if conn.autocommit:
        raise DatabaseError('Write helpers require autocommit=False and caller-managed transactions.')
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise DatabaseError(f'Batch size must be an integer from 1 to {MAX_BATCH_SIZE}.')
    if not isinstance(table, str) or table not in _CONTRACTS:
        raise DatabaseError('Unsupported bulk-storage table.')
    record_type, identity, result_column = _CONTRACTS[table]
    if not isinstance(keys, tuple) or keys != identity or returning != result_column:
        raise DatabaseError('Bulk storage requires the table\'s canonical identity and return column.')
    if isinstance(records, (str, bytes, Mapping)):
        raise DatabaseError('Bulk records must be an iterable of supported records.')
    try:
        items = tuple(records)
    except TypeError as exc:
        raise DatabaseError('Bulk records must be an iterable of supported records.') from exc
    columns = tuple(field.name for field in fields(record_type)) if record_type is not None else _MAPPING_COLUMNS[table]
    for record in items:
        if record_type is not None:
            if type(record) is not record_type:
                raise DatabaseError(f'Bulk {table} records must be {record_type.__name__} instances.')
        elif not isinstance(record, Mapping) or set(record) != set(columns):
            raise DatabaseError('Bulk links require all and only the supported link fields.')
        if isinstance(record, FactInput) and record.numeric_value is not None:
            if not isinstance(record.numeric_value, Decimal) or not record.numeric_value.is_finite():
                raise DatabaseError('numeric_value must be a finite Decimal, never float.')
    if not items:
        return []

    insert = sql.SQL('INSERT INTO sec.{} ({}) VALUES ({}) ON CONFLICT ({}) DO NOTHING RETURNING {}').format(
        sql.Identifier(table), sql.SQL(', ').join(map(sql.Identifier, columns)),
        sql.SQL(', ').join(sql.Placeholder() for _ in columns),
        sql.SQL(', ').join(map(sql.Identifier, keys)), sql.Identifier(returning),
    )
    # Canonical identity columns are NOT NULL. Ordinary equality lets PostgreSQL
    # locate the unique row through its index before comparing nullable payloads.
    # NULL-safe predicates on every column otherwise turn replays into repeated
    # scans of the entire facts/chunks/cells table.
    identical = sql.SQL(' AND ').join(
        sql.SQL('{} = %s' if column in keys else '{} IS NOT DISTINCT FROM %s').format(sql.Identifier(column))
        for column in columns)
    select = sql.SQL('SELECT {} FROM sec.{} WHERE {} FOR SHARE').format(
        sql.Identifier(returning), sql.Identifier(table), identical)
    result: list[UUID] = []
    for offset in range(0, len(items), batch_size):
        bound = []
        for record in items[offset:offset + batch_size]:
            data = asdict(record) if record_type is not None else record
            bound.append([Jsonb(data[column]) if column in _JSON_COLUMNS and data[column] is not None
                          else data[column] for column in columns])
        # A cursor cannot be read until the pipeline has synchronized. SQL errors
        # propagate from execute/sync and leave recovery to the caller.
        with conn.pipeline():
            inserted = [conn.execute(insert, values) for values in bound]
        rows = [cursor.fetchone() for cursor in inserted]
        conflicts = [index for index, row in enumerate(rows) if row is None]
        if conflicts:
            with conn.pipeline():
                checked = [conn.execute(select, bound[index]) for index in conflicts]
            for index, cursor in zip(conflicts, checked):
                rows[index] = cursor.fetchone()
                if rows[index] is None:
                    raise DataConflict(f'Conflicting {table} identity; preserve the original and use a new version.')
        result.extend(row[0] for row in rows)
    return result
