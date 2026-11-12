"""Company tagging errors: a filer's XBRL tag that contradicts its own printed page.

Only the flagged V8 profile records them. Nothing is repaired or guessed: the
printed text and the tagged fact are both kept, the cell that shows the
number is not linked to the contradicting fact, and an explicit defect record
says what the page prints and what the tag claims. Every other failure keeps
rejecting the filing exactly as before.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import hashlib
import json

RULE_VERSION = 'company-tagging-defects-v1'
# A tagged date that contradicts the printed column or row.
CELL_CODES = frozenset({'fact_period_differs_from_column', 'opening_balance_date_not_prior_day',
                        'instant_context_under_duration_header'})
# An inline number tag that encloses only part of the printed number.
FACT_CODES = frozenset({'printed_number_outside_tag'})
CODES = CELL_CODES | FACT_CODES
PERIOD_REASON = 'Fact period differs from column heading'
_OBSERVED = ('period', 'concept', 'context_ref', 'unit_ref', 'unit', 'dimensions', 'raw_value', 'is_nil')


class TaggingDefectError(ValueError):
    """A claimed company tagging error is not proved by the original source."""


def defect_key(version, identity):
    payload = json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()
    return f'{version}:tagging-defect:' + hashlib.sha256(payload).hexdigest()


def _cell_marker(record):
    return {name: record[name] for name in ('defect_key', 'code', 'rule_version', 'inline_id')} | {
        'inline_xpath': record['evidence']['inline_xpath'], 'cell_tags': deepcopy(record['evidence']['cell_tags'])}


def _expected_period(cell, fact):
    """The period the printed page assigns to the cell's number."""
    from .source_cells_v4 import BEGINNING, ENDING, _words
    column = deepcopy(cell['column_period'])
    expected = {'period': column}
    if fact.period_kind == 'instant' and isinstance(column, dict) and set(column) == {'startDate', 'endDate'}:
        label = _words(cell.get('row_heading') or '')
        if BEGINNING.search(label):
            expected['instant'] = (date.fromisoformat(column['startDate']) - timedelta(days=1)).isoformat()
        elif ENDING.search(label):
            expected['instant'] = column['endDate']
    return expected


def _cell_tags(checked, table, cell):
    """Every visible numeric tag of an original anchor cell, each in exact HTML/XML correspondence.

    The same checks as ``ReviewSource.direct``, for cells that print one number
    under more than one tag (for example an outer and a nested duplicate tag).
    """
    from .source_cells_v3 import fact_reference
    from .source_cells_v4 import SourceCellError, _inline_number, _is_nil
    from .source_locators import inline_source_xpath, source_xpath
    from .tables import _hidden, column_headers, local

    node = checked.bindings.cell(cell)
    original_table, rows, first, periods = checked.layout(table)
    if next((parent for parent in node.iterancestors() if local(parent) == 'table'), None) is not original_table:
        raise SourceCellError('Flagged cell does not belong to its declared original table.')
    originals = [original for row in rows for original in row if original['_element'] is node]
    if len(originals) != 1:
        raise SourceCellError('Flagged cell is not an original anchor cell.')
    original = originals[0]
    if (any(cell.get(key) != original[key] for key in ('cell_id', 'row', 'column', 'rowspan', 'colspan', 'source_xpath',
                                                       'display_text'))
            or cell.get('row_heading') != rows[original['row']][0]['display_text']
            or cell.get('column_headings') != column_headers(rows, first, original['column'])
            or cell.get('column_period') != periods.get(original['column'])):
        raise SourceCellError('Flagged cell labels, coordinates or period differ from the original source.')
    nodes = [child for child in node.iter() if isinstance(child.tag, str)
             and child.tag.lower().split(':')[-1] in {'nonfraction', 'fraction'}]
    if not nodes or any(_hidden(item) or item.tag.lower().split(':')[-1] != 'nonfraction' for item in nodes):
        raise SourceCellError('A flagged cell needs visible numeric tags only.')
    expected_scope = table.structure.get('fact_scope')
    tags = []
    for inline in nodes:
        identifier = inline.get('id')
        fact = checked.facts.get(identifier)
        if (len(checked.bindings.ids.get(identifier, [])) != 1 or len(checked.numeric.get(identifier, [])) != 1
                or fact is None or fact.is_nil or fact.numeric_value is None or fact.unit is None):
            raise SourceCellError('A flagged tag must be a unique, non-nil numeric XML fact.')
        source = checked.numeric[identifier][0]
        prefix, _, name = source.get('name', '').partition(':')
        if ((source.nsmap.get(prefix), name) != (fact.concept_namespace, fact.concept_name)
                or source.get('contextRef') != fact.source_reference.get('context_ref')
                or source.get('unitRef') != fact.source_reference.get('unit_ref')
                or _is_nil(source) or _inline_number(source) != fact.numeric_value
                or table.filing_id != fact.filing_id
                or expected_scope != {'filing_id': str(fact.filing_id), 'xml_document_id': str(fact.source_document_id),
                                      'facts_version': fact.extraction_version}):
            raise SourceCellError('A flagged tag requires exact original HTML/XML correspondence.')
        tags.append((fact, {**fact_reference(fact), 'xml_id': identifier, 'scope': expected_scope,
                            'method': 'inline_id', 'inline_document_id': str(table.source_document_id),
                            'inline_xpath': inline_source_xpath(checked.bindings.tree, inline)},
                     source_xpath(checked.bindings.tree, inline)))
    return tags


def _convert(cell, record):
    cell.update(classification='source_defect', fact_references=[], unresolved_reason=None,
                tagging_defect=_cell_marker(record))
    for name in ('source_fact_references', 'quality_issues', 'quality_issue_keys'):
        cell.pop(name, None)


def flag_table_cells(tables, source, facts, *, version, boundary_records=()):
    """Return (tables, defect records) with every provable tagging error flagged on its cell.

    Converted: an unresolved cell whose visible tags passed every value check
    but exactly one of which contradicts the printed column; a V4 period-review
    conflict (opening cash dated the first day, instant under a duration
    header). A partially tagged number is only ever a fact-level defect.
    """
    from .source_cells_v4 import ReviewSource
    from .source_cells_v4 import SourceCellError

    checked = ReviewSource(source, facts, tagging_defects=True)
    boundary = {record['inline_id']: record for record in boundary_records}
    records, result = [], []
    for table in tables:
        structure = deepcopy(table.structure)
        for row in structure['rows']:
            for cell in row:
                record = None
                if cell.get('classification') == 'unresolved' and cell.get('unresolved_reason') == PERIOD_REASON:
                    try:
                        tags = _cell_tags(checked, table, cell)
                    except (SourceCellError, ValueError):
                        continue
                    if any(ref['xml_id'] in boundary for _, ref, _ in tags):
                        raise TaggingDefectError('A partially tagged number printed in a statement table is not supported.')
                    # Exactly one tag contradicts the printed column; any other
                    # tag of the same printed number agrees with it.
                    contradicting = []
                    for fact, ref, numeric_xpath in tags:
                        expected = _expected_period(cell, fact)
                        if ref['period'] not in (expected['period'], {'instant': expected.get('instant')}):
                            contradicting.append((fact, ref, numeric_xpath, expected))
                    if len(contradicting) != 1:
                        continue
                    fact, ref, numeric_xpath, expected = contradicting[0]
                    observed = {name: deepcopy(ref[name]) for name in _OBSERVED}
                    evidence = {'html_document_id': str(table.source_document_id),
                                'html_sha256': checked.bindings.sha256, 'xml_sha256': table.structure['xml_sha256'],
                                'cell_xpath': cell['source_xpath'], 'inline_xpath': ref['inline_xpath'],
                                'numeric_source_xpath': numeric_xpath, 'inline_id': ref['xml_id'],
                                'display_text': cell['display_text'], 'row_heading': cell.get('row_heading'),
                                'column_headings': deepcopy(cell.get('column_headings')),
                                'column_period_evidence': deepcopy((table.structure.get('column_period_evidence') or {})
                                                                   .get(str(cell['column']))),
                                'cell_tags': sorted(({'inline_id': other[1]['xml_id'], 'inline_xpath': other[1]['inline_xpath']}
                                                     for other in tags), key=lambda item: item['inline_id'])}
                    record = {'code': 'fact_period_differs_from_column', 'inline_id': ref['xml_id'],
                              'expected': expected, 'observed': observed, 'evidence': evidence}
                elif cell.get('classification') == 'source_conflict':
                    issues, refs = cell.get('quality_issues') or [], cell.get('source_fact_references') or []
                    if len(issues) != 1 or len(refs) != 1:
                        raise TaggingDefectError('A period conflict must have one issue and one source binding.')
                    if refs[0]['xml_id'] in boundary:
                        raise TaggingDefectError('A partially tagged number printed in a statement table is not supported.')
                    issue = issues[0]
                    record = {'code': issue['code'], 'inline_id': refs[0]['xml_id'],
                              'expected': deepcopy(issue['expected']), 'observed': deepcopy(issue['observed']),
                              'evidence': deepcopy(issue['evidence']) | {
                                  'inline_xpath': refs[0]['inline_xpath'], 'review_rule_version': issue['rule_version'],
                                  'cell_tags': [{'inline_id': refs[0]['xml_id'], 'inline_xpath': refs[0]['inline_xpath']}]}}
                elif cell.get('classification') == 'resolved' and any(
                        ref['xml_id'] in boundary for ref in cell.get('fact_references') or []):
                    # A statement cell cannot be flagged for a partially tagged
                    # number; the filing stays rejected as under every profile.
                    raise TaggingDefectError('A partially tagged number printed in a statement table is not supported.')
                if record is None:
                    continue
                identity = {'html_sha256': checked.bindings.sha256, 'cell_xpath': cell['source_xpath'],
                            'inline_id': record['inline_id'], 'code': record['code'], 'rule_version': RULE_VERSION}
                record = {'defect_key': defect_key(version, identity), 'rule_version': RULE_VERSION,
                          'table_key': table.table_key, 'cell_key': cell['cell_id'],
                          'inline_document_id': str(table.source_document_id), **record}
                _convert(cell, record)
                records.append(record)
        structure['inventory'] = dict(Counter(cell['classification'] for row in structure['rows'] for cell in row))
        result.append(replace(table, structure=structure))
    return tuple(result), tuple(records)


def fact_boundary_records(raw_records, facts, *, version):
    """Defect records for inline number tags that enclose only part of the printed number."""
    from .source_cells_v3 import fact_reference

    by_key = {fact.occurrence_key: fact for fact in facts}
    records = []
    for raw in raw_records:
        fact = by_key.get(raw['inline_id'])
        if fact is None or fact.is_nil or fact.unit is None:
            raise TaggingDefectError('A partially tagged number requires its numeric XML fact.')
        observed = {name: deepcopy(value) for name, value in fact_reference(fact).items() if name in _OBSERVED}
        observed['tagged_number'] = raw['tagged_display']
        evidence = {name: raw[name] for name in ('inline_xpath', 'html_sha256', 'filename', 'inline_document_id')}
        identity = {'html_sha256': raw['html_sha256'], 'inline_xpath': raw['inline_xpath'],
                    'inline_id': raw['inline_id'], 'code': 'printed_number_outside_tag', 'rule_version': RULE_VERSION}
        records.append({'defect_key': defect_key(version, identity), 'rule_version': RULE_VERSION,
                        'code': 'printed_number_outside_tag', 'inline_id': raw['inline_id'],
                        'inline_document_id': raw['inline_document_id'], 'table_key': None, 'cell_key': None,
                        'expected': {'printed_number': raw['visible_display']}, 'observed': observed,
                        'evidence': evidence})
    return tuple(records)


def validate_records(records, tables):
    """Every record is unique, well formed and matches exactly one flagged cell when it names one."""
    cells = {(table.table_key, cell['cell_id']): cell for table in tables
             for row in table.structure['rows'] for cell in row}
    flagged = {key for key, cell in cells.items() if cell.get('classification') == 'source_defect'}
    named, keys = set(), set()
    for record in records:
        if (set(record) != {'defect_key', 'rule_version', 'code', 'inline_id', 'inline_document_id', 'table_key',
                            'cell_key', 'expected', 'observed', 'evidence'}
                or record['rule_version'] != RULE_VERSION or record['code'] not in CODES
                or record['defect_key'] in keys or not isinstance(record['expected'], dict)
                or not isinstance(record['observed'], dict) or not isinstance(record['evidence'], dict)
                or (record['table_key'] is None) != (record['cell_key'] is None)):
            raise TaggingDefectError('Tagging defect records must be unique and complete.')
        keys.add(record['defect_key'])
        if record['table_key'] is not None:
            cell = cells.get((record['table_key'], record['cell_key']))
            if cell is None or cell.get('tagging_defect') != _cell_marker(record) or record['code'] not in CELL_CODES:
                raise TaggingDefectError('A tagging defect must match its flagged cell exactly.')
            named.add((record['table_key'], record['cell_key']))
        elif record['code'] not in FACT_CODES:
            raise TaggingDefectError('Only partially tagged numbers may be flagged without a cell.')
    if named != flagged:
        raise TaggingDefectError('Every flagged cell needs exactly one tagging defect record.')
    return True
