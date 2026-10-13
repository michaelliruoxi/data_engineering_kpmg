"""Relational cells for the versioned table extractor's schema-v1 contract.

The table's retained layout JSON is checked against every relational cell and
accepted fact reference by PostgreSQL. This loader never guesses a fact from an
amount or promotes unresolved references. The caller owns the transaction.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from .database import DataConflict, DatabaseError, FinancialTableInput


class TableCellError(DatabaseError):
    """Table layout or accepted fact references cannot be represented faithfully."""


@dataclass(frozen=True)
class TableCellInput:
    filing_id: UUID
    table_id: UUID
    cell_key: str
    row_index: int
    column_index: int
    row_span: int
    column_span: int
    kind: str
    display_text: str
    source_locator: dict[str, Any]
    resolution_status: str
    unresolved_reason: str | None
    row_heading: str
    column_headings: list[str]
    column_period: dict[str, Any] | None
    is_total: bool


_CLASSIFICATIONS = {
    'blank': ('blank', 'non_fact'),
    'header': ('header', 'non_fact'),
    'label': ('label', 'non_fact'),
    'formatting': ('formatting', 'non_fact'),
    'resolved_label': ('label', 'resolved'),
    'resolved': ('data', 'resolved'),
    'unresolved': ('data', 'unresolved'),
}
_METHODS = {'inline_id', 'same_row_semantic_repeat'}
_JSON_COLUMNS = {'source_locator', 'column_headings', 'column_period', 'evidence'}


def relational_cells(table_id: UUID, table: FinancialTableInput):
    """Validate the cell contract before the first write; return cells and refs."""
    structure = table.structure
    if structure.get('schema_version') != 1 or not isinstance(structure.get('rows'), list):
        raise TableCellError('Relational cells require the table schema-v1 rows contract.')
    cells, seen, occupied = [], set(), set()
    for row in structure['rows']:
        if not isinstance(row, list):
            raise TableCellError('Every table row must contain a list of anchor cells.')
        for cell in row:
            if not isinstance(cell, dict):
                raise TableCellError('Every anchor cell must be an object.')
            key = cell.get('cell_id')
            if not isinstance(key, str) or not key.strip() or key in seen:
                raise TableCellError('Cell keys must be nonempty and unique within a table.')
            seen.add(key)
            for field, minimum in (('row', 0), ('column', 0), ('rowspan', 1), ('colspan', 1)):
                if type(cell.get(field)) is not int or cell[field] < minimum:
                    raise TableCellError(f'Invalid cell {field}.')
            slots = {(r, c) for r in range(cell['row'], cell['row'] + cell['rowspan'])
                     for c in range(cell['column'], cell['column'] + cell['colspan'])}
            if occupied & slots:
                raise TableCellError('Cell spans overlap another anchor cell.')
            occupied.update(slots)
            classification = cell.get('classification')
            if classification not in _CLASSIFICATIONS:
                raise TableCellError('Unknown cell classification.')
            kind, status = _CLASSIFICATIONS[classification]
            refs = cell.get('fact_references')
            reason = cell.get('unresolved_reason')
            if not isinstance(refs, list) or any(not isinstance(ref, dict) for ref in refs):
                raise TableCellError('Cell fact references must be a list of objects.')
            if status == 'resolved' and not refs:
                raise TableCellError('A resolved cell requires verified fact references.')
            if status != 'resolved' and refs:
                raise TableCellError('Unresolved and non-fact cells cannot have accepted fact links.')
            if status == 'unresolved':
                if not isinstance(reason, str) or not reason.strip():
                    raise TableCellError('An unresolved cell requires a reason.')
            elif reason is not None:
                raise TableCellError('Only unresolved cells may carry an unresolved reason.')
            ref_ids = []
            for ref in refs:
                if ref.get('method') not in _METHODS:
                    raise TableCellError('Unsupported fact-link verification method.')
                xml_id = ref.get('xml_id')
                if not isinstance(xml_id, str) or not xml_id.strip():
                    raise TableCellError('Every accepted reference needs an XML occurrence ID.')
                if not ref.get('inline_xpath'):
                    raise TableCellError('Every accepted reference needs inline source evidence.')
                if ref['method'] == 'same_row_semantic_repeat' and not ref.get('repeated_from_inline_xpath'):
                    raise TableCellError('Repeated values need the verified origin locator.')
                ref_ids.append(xml_id)
            if len(set(ref_ids)) != len(ref_ids):
                raise TableCellError('Repeated XML occurrence in one cell is ambiguous.')
            if not isinstance(cell.get('display_text'), str) or not cell.get('source_xpath'):
                raise TableCellError('Every cell needs display text and a source locator.')
            headings = cell.get('column_headings')
            if (not isinstance(headings, list) or any(not isinstance(h, str) for h in headings)
                    or not isinstance(cell.get('row_heading'), str)
                    or type(cell.get('is_total')) is not bool
                    or (cell.get('column_period') is not None and not isinstance(cell['column_period'], dict))):
                raise TableCellError('Invalid cell headings, period, or total marker.')
            record = TableCellInput(
                table.filing_id, table_id, key, cell['row'], cell['column'],
                cell['rowspan'], cell['colspan'], kind, cell['display_text'],
                {'xpath': cell['source_xpath'], 'html_id': cell.get('source_html_id')},
                status, reason, cell['row_heading'], headings, cell.get('column_period'),
                cell['is_total'],
            )
            cells.append((record, refs))
    return cells


def _insert_exact(conn, table_name, values, key_columns, returning='id'):
    """Insert or return identical existing data, never rewrite an identity."""
    from psycopg import sql

    columns = list(values)
    bound = [Jsonb(values[c]) if c in _JSON_COLUMNS and values[c] is not None else values[c]
             for c in columns]
    query = sql.SQL('INSERT INTO sec.{} ({}) VALUES ({}) ON CONFLICT ({}) DO NOTHING RETURNING {}').format(
        sql.Identifier(table_name), sql.SQL(', ').join(map(sql.Identifier, columns)),
        sql.SQL(', ').join(sql.Placeholder() for _ in columns),
        sql.SQL(', ').join(map(sql.Identifier, key_columns)), sql.Identifier(returning),
    )
    row = conn.execute(query, bound).fetchone()
    if row is not None:
        return row[0]
    predicate = sql.SQL(' AND ').join(
        sql.SQL('{} IS NOT DISTINCT FROM %s').format(sql.Identifier(c)) for c in columns)
    row = conn.execute(sql.SQL('SELECT {} FROM sec.{} WHERE {} FOR SHARE').format(
        sql.Identifier(returning), sql.Identifier(table_name), predicate), bound).fetchone()
    if row is None:
        raise DataConflict(f'Conflicting {table_name} content; create a new table extraction version.')
    return row[0]


def store_table_cells(conn, table_id: UUID, table: FinancialTableInput,
                      xml_id_to_fact_id: dict[str, UUID]) -> dict[str, UUID]:
    """Persist every anchor cell and verified link, with an atomic replay check.

    This is called after load_tables verifies the stored facts against XML. SQL
    constraints independently check accepted evidence and legacy membership.
    """
    from dataclasses import asdict

    if conn.autocommit:
        raise TableCellError('Caller-managed transaction required.')
    parsed = relational_cells(table_id, table)
    for _, refs in parsed:
        for ref in refs:
            if ref['xml_id'] not in xml_id_to_fact_id:
                raise TableCellError(f'Missing stored fact: {ref["xml_id"]}')
    # Start the caller's implicit transaction before opening our savepoint.
    # A transaction() block on an idle connection would otherwise commit here.
    conn.execute('SELECT sec.lock_processing_filing(%s)', (table.filing_id,))
    result = {}
    with conn.transaction():
        conn.execute('''UPDATE sec.financial_tables SET relational_cells_version = 1
                        WHERE id = %s AND filing_id = %s AND relational_cells_version IS NULL''',
                     (table_id, table.filing_id))
        for cell, refs in parsed:
            cell_id = _insert_exact(conn, 'table_cells', asdict(cell), ('table_id', 'cell_key'))
            result[cell.cell_key] = cell_id
            for ordinal, ref in enumerate(refs):
                _insert_exact(conn, 'cell_fact_links', {
                    'filing_id': table.filing_id, 'table_id': table_id, 'cell_id': cell_id,
                    'fact_id': xml_id_to_fact_id[ref['xml_id']], 'reference_order': ordinal,
                    'link_method': ref['method'], 'evidence': ref,
                }, ('cell_id', 'fact_id'), returning='fact_id')
        stored = dict(conn.execute('SELECT cell_key, id FROM sec.table_cells WHERE table_id = %s',
                                   (table_id,)).fetchall())
        if stored != result:
            raise DataConflict('Stored cells do not match this table extraction.')
        conn.execute('SELECT sec.validate_table_cell_consistency(%s)', (table_id,))
    return result
