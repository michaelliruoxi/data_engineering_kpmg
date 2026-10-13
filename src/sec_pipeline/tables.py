"""Primary statement extraction for the supplied inline-XBRL filing.

No HTTP requests or database writes occur during extraction. Layout and XML
values are separate: a displayed cash outflow may have a positive XBRL value.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID

from lxml import etree, html

from .database import FinancialTableInput, store_financial_table, link_table_fact
from .table_cells import TableCellError, relational_cells, store_table_cells

XBRLI = 'http://www.xbrl.org/2003/instance'
XBRLDI = 'http://xbrl.org/2006/xbrldi'
XSI = 'http://www.w3.org/2001/XMLSchema-instance'
VERSION = 'tables-v1'


class TableExtractionError(ValueError):
    """Missing, ambiguous, or inconsistent source evidence."""


def text(element):
    return ' '.join(''.join(element.itertext()).split())


def local(element):
    return str(element.tag).split('}')[-1].split(':')[-1].lower()


def expanded(name, ns):
    prefix, sep, value = name.partition(':')
    if not sep:
        value, prefix = prefix, None
    if prefix not in ns:
        raise TableExtractionError(f'Unknown namespace prefix: {prefix}')
    return '{' + ns[prefix] + '}' + value


def xml_evidence(path):
    """Read evidence for linking; this is not the team's FactInput extractor."""
    tree = etree.parse(str(path), etree.XMLParser(resolve_entities=False, no_network=True))
    root = tree.getroot()
    contexts = {}
    for c in root.findall(f'{{{XBRLI}}}context'):
        p = c.find(f'{{{XBRLI}}}period')
        period = {etree.QName(v).localname: text(v) for v in p}
        dims = []
        for d in c.iter():
            if d.tag == f'{{{XBRLDI}}}explicitMember':
                dims.append({'kind': 'explicit', 'dimension': expanded(d.get('dimension'), d.nsmap),
                             'member': expanded(text(d), d.nsmap)})
            elif d.tag == f'{{{XBRLDI}}}typedMember':
                dims.append({'kind': 'typed', 'dimension': expanded(d.get('dimension'), d.nsmap),
                             'value': etree.tostring(d[0], encoding='unicode', with_tail=False)})
        contexts[c.get('id')] = {'period': period, 'dimensions': dims}
    units = {}
    for u in root.findall(f'{{{XBRLI}}}unit'):
        divide = u.find(f'{{{XBRLI}}}divide')
        if divide is None:
            numerator, denominator = list(u), []
        else:
            numerator = list(divide.find(f'{{{XBRLI}}}unitNumerator'))
            denominator = list(divide.find(f'{{{XBRLI}}}unitDenominator'))
        units[u.get('id')] = {'numerator': [expanded(text(m), m.nsmap) for m in numerator],
                             'denominator': [expanded(text(m), m.nsmap) for m in denominator]}
    facts = {}
    for f in root:
        if not f.get('contextRef'):
            continue
        fid = f.get('id')
        if not fid or fid in facts:
            raise TableExtractionError('Missing or duplicate XML fact ID')
        facts[fid] = {'xml_id': fid, 'concept': f.tag, 'context_ref': f.get('contextRef'),
                      'unit_ref': f.get('unitRef'), 'unit': units.get(f.get('unitRef')),
                      'raw_value': text(f), 'is_nil': f.get(f'{{{XSI}}}nil') in ('true', '1'),
                      **contexts[f.get('contextRef')]}
    return facts


def layout_grid(table, tree):
    """Anchor cells with explicit spans; covered slots reference their anchor."""
    rows, occupied = [], {}
    for ri, tr in enumerate(table.xpath('./tr|./thead/tr|./tbody/tr|./tfoot/tr')):
        cells, col = [], 0
        for element in tr.xpath('./td|./th'):
            while (ri, col) in occupied:
                col += 1
            rs, cs = int(element.get('rowspan', '1')), int(element.get('colspan', '1'))
            if rs < 1 or cs < 1:
                raise TableExtractionError('Unsupported zero/negative cell span')
            cid = f'r{ri:03d}c{col:03d}'
            for rr in range(ri, ri + rs):
                for cc in range(col, col + cs):
                    if (rr, cc) in occupied:
                        raise TableExtractionError('Overlapping merged cells')
                    occupied[rr, cc] = cid
            cells.append({'cell_id': cid, 'row': ri, 'column': col, 'rowspan': rs,
                          'colspan': cs, 'display_text': text(element),
                          'source_xpath': tree.getpath(element), 'source_html_id': element.get('id'),
                          'source_html': etree.tostring(element, encoding='unicode', with_tail=False),
                          '_element': element})
            col += cs
        rows.append(cells)
    width = max((c for _, c in occupied), default=-1) + 1
    grid = [[occupied.get((r, c)) for c in range(width)] for r in range(len(rows))]
    return rows, grid


def heading_before(table):
    # Only surrounding siblings, never document-order table numbers.
    result, sibling = [], table.getprevious()
    for _ in range(8):
        if sibling is None or local(sibling) == 'table':
            break
        value = text(sibling)
        if value:
            result.append(value)
        sibling = sibling.getprevious()
    return result


def select_statements(tree):
    signatures = {
        'income_statement': ('INCOMESTATEMENTS', ['total revenue', 'operating income', 'net income']),
        'balance_sheet': ('BALANCESHEETS', ['total assets', 'total liabilities', 'stockholders']),
        'cash_flow': ('CASHFLOWSSTATEMENTS', ['net cash from operations', 'net cash used in financing',
                                               'cash and cash equivalents, end of period']),
    }
    result = {}
    for kind, (heading, labels) in signatures.items():
        candidates = []
        for table in tree.xpath('//table'):
            near = [re.sub(r'[^A-Z]', '', s.upper()) for s in heading_before(table)]
            content = text(table).lower()
            if heading in near and all(label in content for label in labels):
                candidates.append(table)
        if len(candidates) != 1:
            raise TableExtractionError(f'{kind}: expected one statement, found {len(candidates)}')
        result[kind] = candidates[0]
    return result


def column_headers(rows, first_data_row, column):
    return [c['display_text'] for row in rows[:first_data_row] for c in row
            if c['display_text'] and c['column'] <= column < c['column'] + c['colspan']]


def header_period(headers):
    s = ' '.join(headers)
    year = re.search(r'\b(20\d\d)\b', s)
    if not year:
        return None
    # Sample headings; unsupported dates are retained and fail semantic fallback.
    y = int(year.group())
    if 'Three Months' in s and 'March 31' in s:
        return {'startDate': f'{y}-01-01', 'endDate': f'{y}-03-31'}
    if 'Nine Months' in s and 'March 31' in s:
        return {'startDate': f'{y-1}-07-01', 'endDate': f'{y}-03-31'}
    for label, suffix in [('March 31', '03-31'), ('June 30', '06-30')]:
        if label in s:
            return {'instant': f'{y}-{suffix}'}
    return None


def verify_inline(element, evidence, ns):
    if expanded(element.get('name', ''), ns) != evidence['concept']:
        raise TableExtractionError('Concept mismatch')
    if element.get('contextref') != evidence['context_ref']:
        raise TableExtractionError('Context mismatch')
    if element.get('unitref') != evidence['unit_ref']:
        raise TableExtractionError('Unit mismatch')
    nil = element.get('xsi:nil') in ('true', '1')
    if nil != evidence['is_nil']:
        raise TableExtractionError('Nil mismatch')
    if nil:
        return
    fmt = element.get('format', '').split(':')[-1]
    if fmt not in ('num-dot-decimal', 'numdotdecimal', 'zerodash', 'num-dash', ''):
        raise TableExtractionError(f'Unsupported inline numeric format: {fmt}')
    lexical = re.sub(r'[\s,]', '', text(element))
    if fmt in ('zerodash', 'num-dash') and lexical in ('-', '–', '—'):
        lexical = '0'
    try:
        value = Decimal(lexical) * Decimal(10) ** int(element.get('scale', '0'))
        if element.get('sign') == '-':
            value = -value
        if value != Decimal(evidence['raw_value']):
            raise TableExtractionError('Inline/XML value mismatch')
    except (InvalidOperation, ValueError) as exc:
        raise TableExtractionError('Unsupported numeric value') from exc


def extract_tables(html_path, xml_path, *, filing_id: UUID, html_document_id: UUID,
                   xml_document_id: UUID, facts_version='facts-v1', tables_version=VERSION):
    if html_document_id == xml_document_id:
        raise TableExtractionError('HTML and XML must have distinct source IDs')
    tree = html.parse(str(html_path))
    ns = {k[6:]: v for k, v in tree.getroot().attrib.items() if k.startswith('xmlns:')}
    facts = xml_evidence(xml_path)
    html_ids = {}
    for element in tree.iter():
        if local(element) == 'nonfraction' and element.get('id'):
            html_ids[element.get('id')] = html_ids.get(element.get('id'), 0) + 1
    output = []
    for kind, table in select_statements(tree).items():
        rows, grid = layout_grid(table, tree)
        first_data = next(i for i, row in enumerate(rows) if any(
            local(e) == 'nonfraction' for c in row for e in c['_element'].iter()))
        scope = {'filing_id': str(filing_id), 'xml_document_id': str(xml_document_id),
                 'facts_version': facts_version}
        for ri, row in enumerate(rows):
            row_label = row[0]['display_text'] if row else ''
            row_refs = []
            for cell in row:
                element = cell['_element']
                inline = [e for e in element.iter() if local(e) == 'nonfraction']
                cell['row_heading'] = row_label
                cell['column_headings'] = column_headers(rows, first_data, cell['column'])
                cell['column_period'] = header_period(cell['column_headings'])
                cell['fact_references'] = []
                cell['unresolved_reason'] = None
                value = cell['display_text']
                cell['classification'] = ('blank' if not value else 'header' if ri < first_data
                    else 'label' if cell['column'] == 0 else 'formatting')
                if inline:
                    try:
                        if len({f.get('id') for f in inline}) != len(inline):
                            raise TableExtractionError('Duplicate inline ID in cell')
                        for f in inline:
                            fid = f.get('id')
                            evidence = facts.get(fid)
                            if html_ids.get(fid, 0) != 1:
                                raise TableExtractionError('Missing or ambiguous inline ID')
                            if evidence is None:
                                raise TableExtractionError('Missing matching XML ID')
                            verify_inline(f, evidence, ns)
                            cell['fact_references'].append({**evidence, 'scope': scope,
                                'method': 'inline_id', 'inline_xpath': tree.getpath(f),
                                'scale': f.get('scale', '0'), 'inline_sign': f.get('sign'),
                                'format': f.get('format')})
                        if cell['column'] > 0:
                            header = cell['column_period']
                            if header is None:
                                raise TableExtractionError('Unresolved column period')
                            for ref in cell['fact_references']:
                                wanted = header
                                if kind == 'cash_flow' and 'instant' in ref['period']:
                                    if 'beginning of period' in row_label:
                                        wanted = {'instant': (date.fromisoformat(header['startDate'])
                                                              - timedelta(days=1)).isoformat()}
                                    elif 'end of period' in row_label:
                                        wanted = {'instant': header['endDate']}
                                    else:
                                        raise TableExtractionError('Unexpected instant in cash-flow row')
                                if ref['period'] != wanted:
                                    raise TableExtractionError('Fact period differs from column heading')
                        cell['classification'] = 'resolved_label' if cell['column'] == 0 else 'resolved'
                        row_refs.extend(cell['fact_references'])
                    except TableExtractionError as exc:
                        cell['fact_references'] = []
                        cell['classification'], cell['unresolved_reason'] = 'unresolved', str(exc)
                elif ri >= first_data and cell['column'] > 0 and re.fullmatch(
                        r'\(?\s*[\d,]+(?:\.\d+)?\s*\)?|[-–—]', value):
                    cell['classification'], cell['unresolved_reason'] = 'unresolved', 'untagged_financial_cell'
                cell['is_total'] = row_label.lower().startswith(('total ', 'net '))
        # The supplied cash flow repeats an untagged end cash value. Resolve only
        # from uniquely matching SAME ROW concept/unit/dimensions and instant.
        for row in rows:
            refs = [f for c in row for f in c['fact_references']]
            for cell in row:
                if cell['unresolved_reason'] != 'untagged_financial_cell':
                    continue
                period = cell['column_period'] or {}
                end = period.get('instant') or period.get('endDate')
                semantic = {(f['concept'], json.dumps(f['unit'], sort_keys=True),
                             json.dumps(f['dimensions'], sort_keys=True)) for f in refs}
                candidates = {f['xml_id']: f for f in refs if f['period'] == {'instant': end}}
                if len(semantic) == 1 and len(candidates) == 1:
                    f = next(iter(candidates.values()))
                    displayed = Decimal(cell['display_text'].replace(',', ''))
                    if displayed * Decimal(10) ** int(f['scale']) == Decimal(f['raw_value']):
                        cell['fact_references'] = [{**f, 'method': 'same_row_semantic_repeat',
                            'repeated_from_inline_xpath': f['inline_xpath']}]
                        cell['classification'], cell['unresolved_reason'] = 'resolved', None
        for row in rows:
            for c in row:
                del c['_element']
        counts = {}
        for row in rows:
            for cell in row:
                cls = cell['classification']
                counts[cls] = counts.get(cls, 0) + 1
        structure = {'schema_version': 1, 'table_key': kind, 'source_xpath': tree.getpath(table),
                     'heading_context': heading_before(table), 'fact_scope': scope,
                     'html_sha256': hashlib.sha256(Path(html_path).read_bytes()).hexdigest(),
                     'xml_sha256': hashlib.sha256(Path(xml_path).read_bytes()).hexdigest(),
                     'scale_qualifiers': [c['display_text'] for row in rows[:first_data] for c in row
                                          if 'millions' in c['display_text'].lower()],
                     'rows': rows, 'grid': grid, 'inventory': counts,
                     'following_text': text(table.getnext()) if table.getnext() is not None else ''}
        # Keep physical columns, parentheses, dollars and empty cells in TSV.
        anchors = {c['cell_id']: c for row in rows for c in row}
        lines = []
        for ri, slots in enumerate(grid):
            values = [anchors[s]['display_text'] if s and anchors[s]['row'] == ri
                      and anchors[s]['column'] == ci else '' for ci, s in enumerate(slots)]
            lines.append('\t'.join(values))
        value_columns = sorted({c['column'] for row in rows for c in row
                                if c['classification'] in ('resolved', 'unresolved') and c['column'] > 0})
        structure['presentation_columns'] = [
            {'column': col, 'headings': column_headers(rows, first_data, col),
             'period': header_period(column_headers(rows, first_data, col))} for col in value_columns]
        structure['presentation_rows'] = []
        last_period_heading = max((c['row'] for row in rows[:first_data] for c in row
            if c['column'] > 0 and re.search(r'20\d\d', c['display_text'])), default=first_data - 1)
        for row in rows[last_period_heading + 1:]:
            if not any(c['display_text'] for c in row):
                continue
            values = []
            for col in value_columns:
                parts = [c['display_text'] for c in row if col - 1 <= c['column'] <= col + 1]
                values.append(' '.join(p for p in parts if p))
            structure['presentation_rows'].append({'label': row[0]['display_text'] if row else '',
                'values': values, 'source_row': row[0]['row'] if row else None})
        output.append(FinancialTableInput(filing_id=filing_id, source_document_id=html_document_id,
            extraction_version=tables_version, table_key=kind, statement_type=kind,
            structure=structure, readable_text='\n'.join(lines)))
    return output


@dataclass(frozen=True)
class FactScope:
    filing_id: UUID
    xml_document_id: UUID
    extraction_version: str
    # Map original XML IDs to UUIDs returned by the facts loader. No assumption
    # is made about whether Bryce's occurrence_key equals the XML ID.
    xml_id_to_fact_id: dict[str, UUID]


def load_tables(conn, tables, scope: FactScope):
    """Caller owns whole-import transaction; savepoint prevents partial stage writes.

    Verify actual stored records, not just labels attached to a dictionary.
    """
    if conn.autocommit:
        raise TableExtractionError('Caller-managed transaction required')
    for table in tables:
        # Validate every anchor and classification, not only the inventory count.
        # A stale zero-unresolved inventory must not promote unresolved links.
        try:
            relational_cells(UUID(int=0), table)
        except TableCellError as exc:
            raise TableExtractionError(str(exc)) from exc
        expected = {'filing_id': str(scope.filing_id), 'xml_document_id': str(scope.xml_document_id),
                    'facts_version': scope.extraction_version}
        if (table.filing_id != scope.filing_id or table.source_document_id == scope.xml_document_id
                or table.structure['fact_scope'] != expected):
            raise TableExtractionError('Invalid table/fact scope')
        if table.structure['inventory'].get('unresolved', 0) or any(
                cell['classification'] == 'unresolved' for row in table.structure['rows'] for cell in row):
            raise TableExtractionError('Unresolved financial cells block loading')
        for row in table.structure['rows']:
            for cell in row:
                for ref in cell['fact_references']:
                    if ref['scope'] != expected:
                        raise TableExtractionError('Invalid cell fact scope')
                    fid = scope.xml_id_to_fact_id.get(ref['xml_id'])
                    if fid is None:
                        raise TableExtractionError(f'Missing stored fact: {ref["xml_id"]}')
                    record = conn.execute('''SELECT filing_id, source_document_id, extraction_version,
                        concept_namespace, concept_name, raw_value, unit, period_kind, period_start,
                        period_end, instant_date, dimensions, is_nil, numeric_value, occurrence_key, source_reference
                        FROM sec.financial_facts WHERE id = %s''', (fid,)).fetchone()
                    ns, name = ref['concept'][1:].split('}', 1)
                    period = ref['period']
                    kind = 'instant' if 'instant' in period else 'duration' if 'startDate' in period else 'forever'
                    expected_record = (scope.filing_id, scope.xml_document_id, scope.extraction_version,
                        ns, name, None if ref['is_nil'] else ref['raw_value'], ref['unit'], kind,
                        date.fromisoformat(period['startDate']) if 'startDate' in period else None,
                        date.fromisoformat(period['endDate']) if 'endDate' in period else None,
                        date.fromisoformat(period['instant']) if 'instant' in period else None,
                        ref['dimensions'], ref['is_nil'], None if ref['is_nil'] or ref['unit'] is None else Decimal(ref['raw_value']))
                    if record is not None:
                        occurrence, provenance = record[-2:]
                        source_ids = [provenance.get(k) for k in ('element_id', 'xml_id', 'id', 'fact_id')]
                        if ref['xml_id'] not in source_ids and occurrence != ref['xml_id']:
                            raise TableExtractionError('Stored occurrence does not identify XML fact ID')
                    if record is None or tuple(record[:-2]) != expected_record:
                        raise TableExtractionError('Stored fact differs from verified XML evidence/scope')
    # Even an all-header table has no preceding fact query to begin an implicit
    # transaction. Establish caller ownership before opening the stage savepoint.
    conn.execute('SELECT sec.lock_processing_filing(%s)', (scope.filing_id,))
    ids = {}
    with conn.transaction():
        for table in tables:
            tid = store_financial_table(conn, table)
            ids[table.table_key] = tid
            refs = {f['xml_id'] for row in table.structure['rows'] for cell in row
                    for f in cell['fact_references']}
            for xml_id in sorted(refs):
                link_table_fact(conn, scope.filing_id, tid, scope.xml_id_to_fact_id[xml_id])
            store_table_cells(conn, tid, table, scope.xml_id_to_fact_id)
    return ids
