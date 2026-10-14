"""Additive V2 recognition of an exact standard cash-balance concept."""

from collections import Counter
from datetime import date, timedelta
import json
import re

from .filing_sources import _dei
from .joint_statement_periods import additional_gaap_group, cash_alias_endpoint_period, GAAP_CASH_BALANCE_ALIASES
from .source_locators import inline_source_xpath, source_xpath
from .statement_headings import recover_headings
from .tables import (
    StatementSegment, TableExtractionError, _STATEMENT_CONCEPTS, _hidden,
    _column_periods, column_headers, _nearby_headings, _segment_candidate, _segment_facts, _select_statement_segments_v2, layout_grid, local,
)


POLICY = 'standard-cash-alias-v1'


def _ordinary_cash_endpoint_period(concept, row_heading, column_period):
    """Accept an explicit short cash label only for the approved native alias.

    The returned date remains subject to this module's exact source occurrence,
    scope, core-statement and unique opening/closing checks. Source labels are
    retained unchanged in the proof; no restricted-cash amount is inferred.
    """
    wanted = cash_alias_endpoint_period(concept, row_heading, column_period)
    if wanted is not None:
        return wanted
    if additional_gaap_group(concept) != ('cash_flow', 3) or not isinstance(row_heading, str):
        return None
    value = ' '.join(row_heading.casefold().split())
    match = re.fullmatch(r'cash and cash equivalents\s*,?\s+(beginning|end) of (?:the )?period', value)
    if match is None or not isinstance(column_period, dict) or set(column_period) != {'startDate', 'endDate'}:
        return None
    try:
        start, end = date.fromisoformat(column_period['startDate']), date.fromisoformat(column_period['endDate'])
    except (TypeError, ValueError):
        return None
    if start >= end or start == date.min:
        return None
    return {'instant': (start - timedelta(days=1)).isoformat() if match[1] == 'beginning' else end.isoformat()}


def _excluded_scope(table, tree):
    values = list(_nearby_headings(table))
    for row in layout_grid(table, tree)[0]:
        if any(local(node) == 'nonfraction' for cell in row for node in cell['_element'].iter()):
            break
        values.extend(cell['display_text'] for cell in row)
    for value in values:
        value = re.sub(r'([a-z])([A-Z])', r'\1 \2', value)
        value = ' '.join(re.sub(r'[^a-z0-9 ]', ' ', value.lower()).split())
        if re.search(r'\b(?:parent company|parent only|unconsolidated|selected financial)\b|^note\s*[0-9]|^notes to\b', value):
            return True
    return False


def _identity(tree, facts):
    ciks = [fact for fact in facts.values() if re.fullmatch(
        r'\{https?://xbrl\.sec\.gov/dei/(?:19|20)\d{2}\}EntityCentralIndexKey', fact['concept'])]
    if not ciks or any(not re.fullmatch(r'[0-9]{1,10}', fact['raw_value']) for fact in ciks):
        return None
    values = {fact['raw_value'].zfill(10) for fact in ciks}
    if len(values) != 1:
        return None
    cik = next(iter(values))
    primary = [fact for fact in ciks if fact['dimensions'] == [] and fact.get('entity', {}).get('identifier', '').zfill(10) == cik
               and fact['entity'].get('scheme') in {'http://www.sec.gov/CIK', 'https://www.sec.gov/CIK'}]
    entities = {json.dumps(fact['entity'], sort_keys=True) for fact in primary}
    if len(entities) != 1:
        return None
    inline = _dei(tree.getroot(), expected_cik=cik, inline=True)
    if {value.zfill(10) for value in inline.get('EntityCentralIndexKey', [])} != {cik}:
        return None
    return cik, json.loads(next(iter(entities)))


def _core_scope(candidate, facts, entity, groups):
    scopes = set()
    for axis in candidate['axes']:
        if candidate['coverage'][axis] != set(range(len(groups))):
            return None
        for identifiers in candidate['evidence'][axis].values():
            core = [facts[identifier] for identifier in identifiers
                    if not facts[identifier]['dimensions'] and not facts[identifier]['is_nil']]
            if not core:
                return None
            for fact in core:
                if fact['entity'] != entity:
                    return None
                scopes.add(json.dumps({'entity': fact['entity'], 'unit': fact['unit'], 'dimensions': []}, sort_keys=True))
    if len(scopes) != 1:
        return None
    scope = json.loads(next(iter(scopes)))
    unit = scope['unit'] or {}
    if (unit.get('denominator') != [] or len(unit.get('numerator', [])) != 1
            or not re.fullmatch(r'\{http://www.xbrl.org/2003/iso4217\}[A-Z]{3}', unit['numerator'][0])):
        return None
    for values in candidate['references'].values():
        for fact in values:
            if fact['entity'] != entity or any(
                    dimension.get('dimension', '').endswith(('}LegalEntityAxis', '}ConsolidatedEntitiesAxis'))
                    or dimension.get('member', '').endswith('}ParentCompanyMember') for dimension in fact['dimensions']):
                return None
    return scope


def select_alias_statement_segments(tree, facts):
    """Return fully source-proven ordinary statements, or None if unsupported.

    This is called only after established V2 and combined-filing selection.
    It never mutates the legacy concept catalog or rewrites a native QName.
    """
    facts = _segment_facts(facts)
    identity = _identity(tree, facts)
    if identity is None:
        return None
    cik, entity = identity
    groups = {kind: tuple(set(names) for names in sets) for kind, sets in _STATEMENT_CONCEPTS.items()}
    groups['cash_flow'][-1].update(GAAP_CASH_BALANCE_ALIASES)
    try:
        selected = _select_statement_segments_v2(tree, facts, concept_groups=groups)
    except TableExtractionError:
        return None
    if len(selected) != 3 or {part.statement_type for part in selected} != set(groups) or any(part.group for part in selected):
        return None
    html_ids = Counter(node.get('id') for node in tree.iter() if local(node) == 'nonfraction' and node.get('id'))
    candidates, scopes = {}, set()
    for part in selected:
        if (_excluded_scope(part.element, tree)
                or any(local(node) in {'nonnumeric', 'continuation'} for node in part.element.iterancestors())):
            return None
        try:
            candidate = _segment_candidate(tree, part.element, part.statement_type, facts, html_ids,
                                           concept_groups=groups, heading_evidence=recover_headings(part.element))
        except TableExtractionError:
            return None
        if candidate is None or not candidate['axes'] or not candidate['headings']:
            return None
        scope = _core_scope(candidate, facts, entity, groups[part.statement_type])
        if scope is None:
            return None
        scopes.add(json.dumps(scope, sort_keys=True))
        candidates[part.statement_type] = candidate
    if len(scopes) != 1:
        return None
    cash = candidates['cash_flow']
    rows, _ = layout_grid(cash['table'], tree)
    source_cells = {node.get('id'): (row[0]['display_text'], cell, node) for row in rows for cell in row
                    for node in cell['_element'].iter() if local(node) == 'nonfraction' and not _hidden(node)}
    endpoint_periods, endpoint_sources, concepts = {}, {}, set()
    for axis, references in cash['references'].items():
        period = json.loads(axis)
        if set(period) != {'startDate', 'endDate'}:
            return None
        opening = closing = 0
        for fact in references:
            if additional_gaap_group(fact['concept']) != ('cash_flow', 3):
                continue
            identifier = fact['xml_id']
            label, cell, node = source_cells[identifier]
            wanted = _ordinary_cash_endpoint_period(fact['concept'], label, period)
            if (wanted is None or fact['period'] != wanted or fact['dimensions'] or fact['is_nil']
                    or fact['entity'] != entity or fact['unit'] != json.loads(next(iter(scopes)))['unit']):
                return None
            if wanted['instant'] == period['endDate']:
                closing += 1
            else:
                opening += 1
            concepts.add(fact['concept'])
            endpoint_periods[identifier] = wanted
            endpoint_sources[identifier] = {'row_heading': label, 'source_xpath': cell['source_xpath'],
                                             'inline_xpath': inline_source_xpath(tree, node)}
        if opening != 1 or closing != 1:
            return None
    if not endpoint_periods:
        return None
    proof = {'policy': POLICY, 'reporting_cik': cik, 'context_entity': entity, 'concepts': sorted(concepts),
             'endpoint_periods': endpoint_periods, 'endpoint_sources': endpoint_sources}
    key = 'cash_flow'
    group = {'group_key': key, 'segment_count': 1, 'segment_keys': [key], 'segment_index': 1,
             'source_xpaths': [source_xpath(tree, cash['table'])], 'heading': sorted(cash['headings'])[0],
             'column_periods': [json.loads(axis) for axis in cash['axes']], 'scope': json.loads(next(iter(scopes))),
             'combined_concept_evidence': [{'period': json.loads(axis), 'groups': {
                 name: sorted(set(identifiers)) for name, identifiers in sorted(cash['evidence'][axis].items())}}
                 for axis in cash['axes']], 'standard_cash_alias': proof}
    return tuple(StatementSegment(part.statement_type, part.table_key, part.element,
                                  group if part.statement_type == 'cash_flow' else None) for part in selected)


def validate_alias_cells(table, segment, tree, facts):
    """Recheck each supported alias against its own unchanged source cell."""
    proof = (segment.group or {}).get('standard_cash_alias')
    if not proof or proof.get('policy') != POLICY:
        raise TableExtractionError('Standard cash alias cells require the explicit source-proof policy.')
    rows, _ = layout_grid(segment.element, tree)
    first = next(index for index, row in enumerate(rows) if any(local(node) == 'nonfraction'
                 for cell in row for node in cell['_element'].iter()))
    ids = Counter(node.get('id') for node in tree.iter() if local(node) == 'nonfraction' and node.get('id'))
    periods, _ = _column_periods(rows, first, facts, {}, ids, advanced=True)
    source = {node.get('id'): (row[0]['display_text'], cell, node) for row in rows for cell in row
              for node in cell['_element'].iter() if local(node) == 'nonfraction' and not _hidden(node)}
    cells = {cell['cell_id']: cell for row in table.structure['rows'] for cell in row}
    if len(cells) != sum(len(row) for row in table.structure['rows']):
        raise TableExtractionError('Cash alias layout has repeated source cell identities.')
    for identifier, instant in proof['endpoint_periods'].items():
        if identifier not in source or identifier not in facts:
            raise TableExtractionError('Cash alias proof omits its original source occurrence.')
        label, original, node = source[identifier]
        cell = cells.get(original['cell_id'])
        expected_nodes = [part for part in original['_element'].iter() if local(part) == 'nonfraction']
        refs = cell.get('fact_references', []) if cell else []
        if (cell is None or cell.get('classification') != 'resolved' or cell.get('row_heading') != label
                or cell.get('source_xpath') != original['source_xpath']
                or cell.get('column_headings') != column_headers(rows, first, original['column'])
                or cell.get('column_period') != periods.get(original['column'])
                or facts[identifier]['period'] != instant
                or [ref.get('xml_id') for ref in refs] != [part.get('id') for part in expected_nodes]):
            raise TableExtractionError('Cash alias cell differs from its original headers or native occurrence.')
        for ref, part in zip(refs, expected_nodes):
            fact = facts[part.get('id')]
            if (ref.get('method') != 'inline_id' or ref.get('inline_xpath') != inline_source_xpath(tree, part)
                    or any(ref.get(key) != fact[key] for key in (
                        'concept', 'context_ref', 'unit_ref', 'unit', 'period', 'dimensions', 'raw_value', 'is_nil'))):
                raise TableExtractionError('Cash alias evidence substitutes an original XML fact.')
    return True
