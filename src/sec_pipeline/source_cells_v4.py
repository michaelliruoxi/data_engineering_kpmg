"""Preserve exact source bindings while exposing bounded period review issues.

These transformations do not repair the source's value, concept, period or
printed heading. Only a uniquely bound, non-core cash-flow cell can enter the
review tier. The runner must independently rebuild the original table layout
and prove complete document-set correspondence before sealing any output.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import hashlib
import json
import re

from lxml import etree

from .fact_extraction import _inline_number, _is_nil, _parse
from .source_cells_v3 import (
    INLINE_NAMESPACES, SourceBindings, SourceCellError, fact_reference,
)
from .source_locators import inline_source_xpath, source_xpath
from .tables import _hidden, _column_periods, column_headers, layout_grid, local


RULE_VERSION = 'cash-period-review-v1'
FLOW_GROUPS = (
    {'NetCashProvidedByUsedInOperatingActivities', 'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'},
    {'NetCashProvidedByUsedInInvestingActivities', 'NetCashProvidedByUsedInInvestingActivitiesContinuingOperations'},
    {'NetCashProvidedByUsedInFinancingActivities', 'NetCashProvidedByUsedInFinancingActivitiesContinuingOperations'},
)
CASH_CONCEPTS = {
    'CashAndCashEquivalentsAtCarryingValue',
    'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
    'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations',
}
BEGINNING = re.compile(r'\b(?:(?:beginning|start) of (?:the )?(?:period|year|quarter)|(?:beginning|opening) balances?)\b')
ENDING = re.compile(r'\b(?:end of (?:the )?(?:period|year|quarter)|(?:ending|closing) balances?)\b')


def _words(value):
    return ' '.join(re.sub(r'[^a-z0-9 ]', ' ', value.lower()).split())


def _gaap(fact, names=None):
    return (bool(re.fullmatch(r'https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^/]+', fact.concept_namespace))
            and (names is None or fact.concept_name in names))


def _scope(fact):
    entity = fact.source_reference.get('entity')
    unit = fact.unit
    if (not isinstance(entity, dict) or entity.get('scheme') not in {'http://www.sec.gov/CIK', 'https://www.sec.gov/CIK'}
            or not re.fullmatch(r'\d{1,10}', entity.get('identifier', ''))
            or fact.dimensions != [] or not isinstance(unit, dict)
            or unit.get('denominator') != [] or len(unit.get('numerator', [])) != 1
            or not re.fullmatch(r'\{http://www.xbrl.org/2003/iso4217\}[A-Z]{3}', unit['numerator'][0])):
        raise SourceCellError('Period review requires one undimensioned SEC entity and currency.')
    return {'entity': deepcopy(entity), 'unit': deepcopy(unit), 'dimensions': []}


class ReviewSource:
    def __init__(self, source, facts):
        self.bindings = SourceBindings(source, facts)
        self.facts = self.bindings.facts
        self.numeric = defaultdict(list)
        self.layouts = {}
        # Parse unchanged original XHTML separately to use namespace-aware,
        # exact transformation arithmetic; do not construct a synthetic DOM.
        root = _parse(source, 'V4 inline period-review source')
        for node in root.iter():
            if not isinstance(node.tag, str):
                continue
            qname = etree.QName(node)
            if qname.namespace in INLINE_NAMESPACES and qname.localname == 'nonFraction':
                self.numeric[node.get('id')].append(node)

    def layout(self, table):
        locator = table.structure.get('source_xpath')
        if locator not in self.layouts:
            selected = self.bindings.tree.xpath(locator)
            if (len(selected) != 1 or local(selected[0]) != 'table'
                    or source_xpath(self.bindings.tree, selected[0]) != locator):
                raise SourceCellError('Period review needs one original table locator.')
            rows, _ = layout_grid(selected[0], self.bindings.tree)
            first = next((index for index,row in enumerate(rows)
                          if any(local(node) == 'nonfraction' for cell in row for node in cell['_element'].iter())), None)
            if first is None:
                raise SourceCellError('Period review table has no direct numeric source data.')
            evidence = {identifier:{**fact_reference(fact),'xml_id':identifier}
                        for identifier,fact in self.facts.items()}
            namespaces = {key[6:]:value for key,value in self.bindings.tree.getroot().attrib.items() if key.startswith('xmlns:')}
            periods,_ = _column_periods(rows,first,evidence,namespaces,
                                       {key:len(value) for key,value in self.bindings.ids.items()},advanced=True)
            self.layouts[locator] = selected[0],rows,first,periods
        return self.layouts[locator]

    def direct(self, table, cell):
        if str(table.source_document_id) == table.structure.get('fact_scope', {}).get('xml_document_id'):
            raise SourceCellError('A period-review HTML source cannot be the target XML source.')
        node = self.bindings.cell(cell)
        original_table,rows,first,periods = self.layout(table)
        parent_table = next((parent for parent in node.iterancestors() if local(parent) == 'table'),None)
        if parent_table is not original_table:
            raise SourceCellError('Period-review cell does not belong to its declared original table.')
        originals = [original for row in rows for original in row if original['_element'] is node]
        if len(originals) != 1:
            raise SourceCellError('Period-review cell is not an original anchor cell.')
        original = originals[0]
        fields = ('cell_id','row','column','rowspan','colspan','source_xpath','display_text')
        if (any(cell.get(key) != original[key] for key in fields)
                or cell.get('row_heading') != rows[original['row']][0]['display_text']
                or cell.get('column_headings') != column_headers(rows,first,original['column'])
                or cell.get('column_period') != periods.get(original['column'])):
            raise SourceCellError('Period-review labels, coordinates or period differ from the original source.')
        candidates = [child for child in node.iter()
                      if isinstance(child.tag, str) and child.tag.lower().split(':')[-1] in {'nonfraction', 'fraction'}]
        if len(candidates) != 1 or _hidden(candidates[0]):
            raise SourceCellError('Period review requires one visible direct numeric occurrence.')
        inline = candidates[0]
        identifier = inline.get('id')
        if len(self.bindings.ids.get(identifier, [])) != 1 or len(self.numeric.get(identifier, [])) != 1:
            raise SourceCellError('Period review source ID is missing, foreign or ambiguous.')
        fact = self.facts.get(identifier)
        if fact is None or fact.is_nil or fact.numeric_value is None or fact.unit is None:
            raise SourceCellError('Period review cannot invent or change a numeric value.')
        original = self.numeric[identifier][0]
        prefix, _, name = original.get('name', '').partition(':')
        if ((original.nsmap.get(prefix), name) != (fact.concept_namespace, fact.concept_name)
                or original.get('contextRef') != fact.source_reference.get('context_ref')
                or original.get('unitRef') != fact.source_reference.get('unit_ref')
                or _is_nil(original) or _inline_number(original) != fact.numeric_value):
            raise SourceCellError('Period review requires exact original HTML/XML correspondence.')
        expected_scope = {'filing_id': str(fact.filing_id), 'xml_document_id': str(fact.source_document_id),
                          'facts_version': fact.extraction_version}
        if table.filing_id != fact.filing_id or table.structure.get('fact_scope') != expected_scope:
            raise SourceCellError('Period review cannot change filing or fact-source scope.')
        return fact, {
            **fact_reference(fact), 'xml_id': identifier, 'scope': expected_scope,
            'method': 'inline_id', 'inline_document_id': str(table.source_document_id),
            'inline_xpath': inline_source_xpath(self.bindings.tree, inline),
        }, source_xpath(self.bindings.tree, inline)


def _core_proof(table, column_period, scope, source):
    groups = [set() for _ in FLOW_GROUPS]
    endings = []
    for row in table.structure['rows']:
        for cell in row:
            if cell['classification'] != 'resolved' or cell.get('column_period') != column_period:
                continue
            if len(cell.get('fact_references', [])) != 1:
                continue
            try:
                fact, ref, _ = source.direct(table, cell)
                if _scope(fact) != scope:
                    continue
            except SourceCellError:
                continue
            if cell['fact_references'][0].get('xml_id') != ref['xml_id']:
                raise SourceCellError('Core evidence must retain its original source ID.')
            period = fact_reference(fact)['period']
            if period == column_period:
                for index, names in enumerate(FLOW_GROUPS):
                    if _gaap(fact, names):
                        groups[index].add(ref['xml_id'])
            elif (period == {'instant': column_period['endDate']}
                    and _gaap(fact, CASH_CONCEPTS) and ENDING.search(_words(cell['row_heading']))):
                endings.append((cell, fact, ref['xml_id']))
    if any(not group for group in groups) or not endings:
        raise SourceCellError('Review cannot rescue incomplete core cash-flow coverage.')
    return {'flow_xml_ids': [sorted(group) for group in groups],
            'closing_cash_xml_ids': sorted({entry[2] for entry in endings})}, endings


def _issue(table, cell, source):
    column = cell.get('column_period')
    if (table.statement_type != 'cash_flow' or cell['classification'] != 'unresolved'
            or cell.get('fact_references') or not cell.get('row_heading') or not cell.get('column_headings')
            or not isinstance(column, dict) or set(column) != {'startDate', 'endDate'}):
        return None
    start, end = date.fromisoformat(column['startDate']), date.fromisoformat(column['endDate'])
    if start >= end:
        return None
    fact, ref, numeric_xpath = source.direct(table, cell)
    if fact.period_kind != 'instant' or not _gaap(fact):
        return None
    scope = _scope(fact)
    proof, endings = _core_proof(table, column, scope, source)
    core_ids = {identifier for group in proof['flow_xml_ids'] for identifier in group} | set(proof['closing_cash_xml_ids'])
    if ref['xml_id'] in core_ids or any(_gaap(fact, group) for group in FLOW_GROUPS):
        return None
    label = _words(cell['row_heading'])
    expected = {'period': deepcopy(column)}
    if (BEGINNING.search(label) and _gaap(fact, CASH_CONCEPTS)
            and fact.instant_date == start
            and cell.get('unresolved_reason') == 'Fact period differs from column heading'):
        exact_endings = [entry for entry in endings if entry[0]['row'] > cell['row']
                         and entry[0]['column'] == cell['column']
                         and entry[1].concept_name == fact.concept_name]
        if len(exact_endings) != 1:
            return None
        code = 'opening_balance_date_not_prior_day'
        expected['opening_instant'] = (start - timedelta(days=1)).isoformat()
        proof['corresponding_closing_cash_xml_id'] = exact_endings[0][2]
    elif (fact.instant_date == end and not BEGINNING.search(label) and not ENDING.search(label)
          and not _gaap(fact, CASH_CONCEPTS)
          and cell.get('unresolved_reason') == 'Unexpected instant in cash-flow row'):
        code = 'instant_context_under_duration_header'
        expected['row_period_kind'] = 'duration'
    else:
        return None
    observed = {key: deepcopy(ref[key]) for key in ('period', 'concept', 'context_ref', 'unit_ref', 'unit', 'dimensions', 'raw_value', 'is_nil')}
    observed['entity'] = scope['entity']
    evidence = {'html_document_id': str(table.source_document_id),
                'html_sha256': source.bindings.sha256, 'xml_sha256': table.structure['xml_sha256'],
                'cell_xpath': cell['source_xpath'], 'inline_xpath': ref['inline_xpath'],
                'numeric_source_xpath': numeric_xpath, 'inline_id': ref['xml_id'],
                'display_text': cell['display_text'], 'row_heading': cell['row_heading'],
                'column_headings': deepcopy(cell['column_headings']), 'core_coverage': proof,
                'source_correspondence': 'exact_inline_xml'}
    identity = {'html_sha256': source.bindings.sha256, 'cell_xpath': cell['source_xpath'],
                'inline_id': ref['xml_id'], 'code': code, 'rule_version': RULE_VERSION}
    issue_key = 'period-review:' + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return ref, {'issue_key': issue_key, 'code': code, 'rule_version': RULE_VERSION,
                 'expected': expected, 'observed': observed, 'evidence': evidence}


def transform_tables(tables, source, facts):
    """Transform V3 layouts; unsupported source cases remain unresolved."""
    checked = ReviewSource(source, facts)
    result = []
    for table in tables:
        if table.extraction_version != 'tables-v3' or table.structure.get('schema_version') != 2:
            raise SourceCellError('V4 period review requires the original V3 source layout.')
        if table.structure.get('html_sha256') != checked.bindings.sha256:
            raise SourceCellError('V4 table source hash differs from original HTML.')
        structure = deepcopy(table.structure)
        structure.update(schema_version=3, semantic_review_policy=RULE_VERSION)
        for row in structure['rows']:
            for cell in row:
                try:
                    issue = _issue(table, cell, checked)
                except (SourceCellError, ValueError):
                    issue = None
                if issue is not None:
                    reference, record = issue
                    cell.update(classification='source_conflict', fact_references=[],
                                source_fact_references=[reference], quality_issue_keys=[record['issue_key']],
                                quality_issues=[record], unresolved_reason=None)
        structure['inventory'] = dict(Counter(cell['classification'] for row in structure['rows'] for cell in row))
        result.append(replace(table, extraction_version='tables-v4', structure=structure))
    return result


def validate_transformation(tables, original_tables, source, facts):
    """Compare to a fresh original source parse supplied by the V4 runner."""
    rebuilt = transform_tables(original_tables, source, facts)
    if (len(tables) != len(rebuilt) or len({table.table_key for table in tables}) != len(tables)
            or {table.table_key: table for table in tables} != {table.table_key: table for table in rebuilt}):
        raise SourceCellError('V4 source bindings or review issues differ from original source reconstruction.')
    return True
