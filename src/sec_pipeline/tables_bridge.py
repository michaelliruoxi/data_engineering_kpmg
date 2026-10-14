"""Adapt Jazzy's primary statements to the runner's facts-first stages.

The runner owns source verification and the outer transaction. This module
does not connect, commit, or substitute locally generated UUIDs for stored facts.
"""

from pathlib import Path

from .facts_bridge import fact_ids_for_tables
from .processing import ParseResult, ProcessingError, WriteResult
from .tables import FactScope, extract_tables, load_tables


def _source_path(context, role):
    records = [r for r in context.manifest['files'] if r.get('role') == role]
    if len(records) != 1:
        raise ProcessingError(f'Tables require exactly one {role} source.')
    root = Path(context.data_root).resolve()
    path = Path(records[0]['local_path'])
    resolved = (root / path).resolve()
    if path.is_absolute() or not resolved.is_relative_to(root):
        raise ProcessingError('Table sources must remain within the verified data root.')
    return resolved


def parse_tables(context):
    """Pure stage: retain inline/XML IDs; no database UUID lookup here."""
    if (context.versions.get('facts') != 'facts-v1'
            or context.versions.get('tables') not in {'tables-v1', 'tables-v2'}):
        raise ProcessingError('This bridge requires facts-v1 and tables-v1 or tables-v2.')
    tables = extract_tables(
        _source_path(context, 'main_inline_xbrl_filing'),
        _source_path(context, 'extracted_xbrl_instance'),
        filing_id=context.source_ids.filing_id,
        html_document_id=context.source_ids.html_document_id,
        xml_document_id=context.source_ids.xml_document_id,
        facts_version=context.versions['facts'],
        tables_version=context.versions['tables'],
    )
    return ParseResult(tables, unresolved_cells=sum(
        t.structure['inventory'].get('unresolved', 0) for t in tables))


def write_tables(conn, context, *, bulk=False):
    """Resolve UUIDs from this run's facts and load within its transaction."""
    parsed = parse_tables(context)
    scope = FactScope(
        context.source_ids.filing_id, context.source_ids.xml_document_id,
        context.versions['facts'], fact_ids_for_tables(context),
    )
    stored = load_tables(conn, parsed.items, scope, bulk=True) if bulk else load_tables(conn, parsed.items, scope)
    return WriteResult(parsed, [stored[t.table_key] for t in parsed.items])
