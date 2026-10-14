"""Store schema3 cells without changing accepted legacy loader behavior."""

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from uuid import UUID

from .database import DataConflict
from .source_set_storage import (
    ISSUE_CODES, QualityIssueInput, ReviewBindingInput, store_quality_issues, store_review_bindings,
)
from .table_cells import TableCellError, _insert_exact, relational_cells


@dataclass(frozen=True)
class V4CellWriteResult:
    cell_ids: dict[str, UUID]
    binding_ids: dict[str, UUID]
    issue_ids: dict[str, UUID]


def v4_relational_cells(table_id, table):
    """Reuse all existing grid/reference checks, then validate explicit conflicts."""
    if table.extraction_version != 'tables-v4' or table.structure.get('schema_version') != 3:
        raise TableCellError('V4 cells require tables-v4 and structure schema3.')
    original = {cell['cell_id']: cell for row in table.structure['rows'] for cell in row}
    surrogate = deepcopy(table.structure)
    surrogate['schema_version'] = 2
    conflicts = set()
    issue_keys = set()
    for row in surrogate['rows']:
        for cell in row:
            if cell.get('classification') != 'source_conflict':
                if any(cell.get(name) for name in ('source_fact_references', 'quality_issue_keys', 'quality_issues')):
                    raise TableCellError('Only source conflicts may carry review evidence.')
                continue
            refs = cell.get('source_fact_references')
            issues = cell.get('quality_issues')
            if (cell.get('fact_references') != [] or cell.get('unresolved_reason') is not None
                    or not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict)
                    or refs[0].get('method') != 'inline_id' or not refs[0].get('xml_id')
                    or not refs[0].get('inline_xpath') or refs[0].get('inline_document_id') != str(table.source_document_id)
                    or refs[0].get('scope') != table.structure.get('fact_scope')
                    or not isinstance(issues, list) or not issues
                    or not isinstance(cell.get('column_period'), dict)):
                raise TableCellError('A conflict requires one exact direct source binding and explicit issues.')
            keys = []
            for issue in issues:
                if (not isinstance(issue, dict) or set(issue) != {'issue_key','code','rule_version','expected','observed','evidence'}
                        or not isinstance(issue['issue_key'], str) or not issue['issue_key'].strip()
                        or issue['issue_key'] in issue_keys or issue['code'] not in ISSUE_CODES
                        or issue['rule_version'] != 'cash-period-review-v1'
                        or not isinstance(issue['expected'], dict) or not isinstance(issue['observed'], dict)
                        or not isinstance(issue['evidence'], dict)
                        or issue['expected'].get('period') != cell['column_period']
                        or issue['observed'].get('period') != refs[0].get('period')
                        or issue['expected'] == issue['observed']):
                    raise TableCellError('Conflict issues must preserve distinct displayed and source periods.')
                keys.append(issue['issue_key'])
                issue_keys.add(issue['issue_key'])
            if cell.get('quality_issue_keys') != keys:
                raise TableCellError('Conflict issue keys must enumerate the retained issue records in order.')
            conflicts.add(cell['cell_id'])
            cell['classification'] = 'formatting'
    cells = relational_cells(table_id, replace(table, extraction_version='tables-v3', structure=surrogate))
    return [(replace(cell, kind='data', resolution_status='review_required') if cell.cell_key in conflicts else cell,
             refs, original[cell.cell_key]) for cell, refs in cells]


def store_v4_table_cells(conn, table_id, table, xml_id_to_fact_id, occurrence_ids, document_set_id, *, bulk=False):
    """Write cells, accepted links and separate review bindings in caller scope."""
    if conn.autocommit:
        raise TableCellError('V4 cell writes require caller-managed transactions.')
    parsed = v4_relational_cells(table_id, table)
    for _, accepted, descriptor in parsed:
        for ref in [*accepted, *descriptor.get('source_fact_references', [])]:
            if ref['xml_id'] not in xml_id_to_fact_id or ref['xml_id'] not in occurrence_ids:
                raise TableCellError('Every V4 reference requires its stored fact and exact source occurrence.')
    conn.execute('SELECT sec.lock_processing_filing(%s)', (table.filing_id,))
    cells, bindings, issues = {}, {}, {}
    with conn.transaction():
        conn.execute('UPDATE sec.financial_tables SET relational_cells_version=3 WHERE id=%s AND filing_id=%s AND relational_cells_version IS NULL',
                     (table_id, table.filing_id))
        if bulk:
            from .bulk_storage import store_many
            ids = store_many(conn, 'table_cells', [cell for cell, _, _ in parsed], ('table_id', 'cell_key'))
        else:
            ids = [None] * len(parsed)
        accepted_links = []
        for (cell, accepted, descriptor), cell_id in zip(parsed, ids):
            if not bulk:
                cell_id = _insert_exact(conn, 'table_cells', asdict(cell), ('table_id', 'cell_key'))
            cells[cell.cell_key] = cell_id
            if cell.resolution_status == 'source_only':
                _insert_exact(conn, 'cell_source_observations', {
                    'filing_id': table.filing_id, 'table_id': table_id, 'cell_id': cell_id,
                    'source_document_id': table.source_document_id, 'source_sha256': table.structure['html_sha256'],
                    'verification_version': 'source-marker-v1', 'evidence': descriptor['source_only_evidence'],
                }, ('cell_id',))
            for ordinal, ref in enumerate(accepted):
                record = {'filing_id': table.filing_id, 'table_id': table_id, 'cell_id': cell_id,
                          'fact_id': xml_id_to_fact_id[ref['xml_id']], 'reference_order': ordinal,
                          'link_method': ref['method'], 'evidence': ref}
                if bulk:
                    accepted_links.append(record)
                else:
                    _insert_exact(conn, 'cell_fact_links', record, ('cell_id', 'fact_id'), returning='fact_id')
            if cell.resolution_status == 'review_required':
                [ref] = descriptor['source_fact_references']
                [binding] = store_review_bindings(conn, [ReviewBindingInput(
                    filing_id=table.filing_id, document_set_id=document_set_id, table_id=table_id, cell_id=cell_id,
                    fact_id=xml_id_to_fact_id[ref['xml_id']], occurrence_id=occurrence_ids[ref['xml_id']], reference_order=0, evidence=ref)])
                bindings[cell.cell_key] = binding
                issue_records = [QualityIssueInput(filing_id=table.filing_id, document_set_id=document_set_id,
                                                  binding_id=binding, **issue) for issue in descriptor['quality_issues']]
                stored = store_quality_issues(conn, issue_records)
                issues.update((record.issue_key, identifier) for record, identifier in zip(issue_records, stored))
        if bulk and accepted_links:
            store_many(conn, 'cell_fact_links', accepted_links, ('cell_id', 'fact_id'), returning='fact_id')
        stored = dict(conn.execute('SELECT cell_key,id FROM sec.table_cells WHERE table_id=%s', (table_id,)).fetchall())
        if stored != cells:
            raise DataConflict('V4 relational cell inventory differs from its retained table.')
        conn.execute('SELECT sec.validate_table_cell_consistency(%s)', (table_id,))
    return V4CellWriteResult(cells, bindings, issues)
