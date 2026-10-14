"""Bind explicit cash-component disclosures to their source closing balances."""

from collections.abc import Mapping
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from lxml import etree

from .tables import TableExtractionError, _hidden, local, verify_inline


XBRLI = 'http://www.xbrl.org/2003/instance'
XSD = 'http://www.w3.org/2001/XMLSchema'
LINK = 'http://www.xbrl.org/2003/linkbase'
XLINK = 'http://www.w3.org/1999/xlink'
TOTAL = 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
RESTRICTED = {
    'RestrictedCash', 'RestrictedCashCurrent', 'RestrictedCashNoncurrent',
    'RestrictedCashAndCashEquivalents', 'RestrictedCashAndCashEquivalentsNoncurrent',
    'RestrictedCashAndCashEquivalentsAtCarryingValue',
}
HEADINGS = {
    'reconciliation of cash restricted cash and equivalents',
    'reconciliation of cash cash equivalents and restricted cash',
    'reconciliation of cash cash equivalents and restricted cash to the consolidated balance sheets',
    'reconciliation of cash flow information',
    'components of cash cash equivalents and restricted cash',
    'cash cash equivalents and restricted cash at end of period',
    'reconciliation of cash cash equivalents restricted cash and restricted cash equivalents reported within the consolidated balance sheets to the total amounts reported on the consolidated statements of cash flows',
}


def _aligned_amounts(values):
    """Compare sums as bounded integers, without Decimal context rounding."""
    parts = [value.as_tuple() for value in values]
    exponent = min(part.exponent for part in parts)
    if any(len(part.digits) + part.exponent - exponent > 4096 for part in parts):
        raise TableExtractionError('Cash reconciliation exponent span exceeds the exact arithmetic limit')
    integers = []
    for part in parts:
        coefficient = 0
        for digit in part.digits:
            coefficient = coefficient * 10 + digit
        integers.append((-1 if part.sign else 1) * coefficient * 10 ** (part.exponent - exponent))
    return integers


def _words(value):
    return ' '.join(re.sub(r'[^a-z0-9 ]', ' ', value.lower()).split())


def _gaap(concept, names):
    return (bool(re.fullmatch(r'\{https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^}]+\}[^}]+', concept))
            and concept.split('}', 1)[-1] in names)


def _scope(fact):
    unit, entity = fact.get('unit'), fact.get('entity')
    if (fact.get('dimensions') != [] or not isinstance(unit, Mapping)
            or unit.get('denominator') != [] or len(unit.get('numerator', [])) != 1
            or not re.fullmatch(r'\{http://www.xbrl.org/2003/iso4217\}[A-Z]{3}', unit['numerator'][0])
            or not isinstance(entity, Mapping) or not entity.get('scheme') or not entity.get('identifier')):
        raise TableExtractionError('Cash reconciliation requires one undimensioned source entity and currency')
    return {'entity': dict(entity), 'unit': dict(unit), 'dimensions': []}


def _entries(row, periods, facts, ns, html_ids):
    entries = {}
    for cell in row:
        nodes = [node for node in cell['_element'].iter() if local(node) == 'nonfraction' and not _hidden(node)]
        if cell['column'] == 0:
            if nodes:
                raise TableExtractionError('Cash reconciliation labels cannot contain numeric facts')
            continue
        if not nodes:
            value = re.sub(r'[\s\u200b]', '', cell['display_text'])
            if value and value not in {'$', '(', ')'}:
                raise TableExtractionError('Cash reconciliation has an untagged or unrelated component cell')
            continue
        if len(nodes) != 1:
            raise TableExtractionError('Cash reconciliation requires one explicit source fact per cell')
        node = nodes[0]
        fact = facts.get(node.get('id'))
        period = periods.get(cell['column'])
        if (fact is None or html_ids.get(node.get('id')) != 1 or fact.get('is_nil')
                or not isinstance(period, Mapping) or set(period) != {'startDate', 'endDate'}):
            raise TableExtractionError('Cash reconciliation lacks a unique fact or main duration column')
        verify_inline(node, fact, ns)
        if fact['period'] != {'instant': period['endDate']}:
            raise TableExtractionError('Cash reconciliation instant differs from its main column end date')
        amount = Decimal(fact['raw_value'])
        if not amount.is_finite() or amount < 0:
            raise TableExtractionError('Cash reconciliation requires finite nonnegative cash balances')
        axis = json.dumps(dict(period), sort_keys=True)
        if axis in entries:
            raise TableExtractionError('Cash reconciliation has ambiguous columns for the same duration')
        entries[axis] = {'fact': fact, 'cell': cell, 'amount': amount, 'scope': _scope(fact)}
    if not entries:
        raise TableExtractionError('Cash reconciliation row has no source facts')
    return entries


def _extension_declarations(xml_path):
    """Read only the local schema explicitly referenced by the archived XML."""
    xml_path = Path(xml_path)
    if not xml_path.is_file():
        return {}
    root = etree.parse(str(xml_path), etree.XMLParser(resolve_entities=False, no_network=True)).getroot()
    names = [node.get(f'{{{XLINK}}}href', '') for node in root.findall(f'{{{LINK}}}schemaRef')]
    if len(names) != 1 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.xsd', names[0]):
        return {}
    path = xml_path.parent / names[0]
    if not path.is_file() or path.resolve().parent != xml_path.parent.resolve():
        return {}
    data = path.read_bytes()
    schema = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
    target = schema.get('targetNamespace')
    if schema.tag != f'{{{XSD}}}schema' or not target:
        return {}
    declarations, duplicates, seen = {}, set(), set()
    for element in schema.findall(f'{{{XSD}}}element'):
        name = element.get('name', '')
        key = '{' + target + '}' + name
        if key in seen:
            duplicates.add(key)
        seen.add(key)
        prefix, _, local_type = element.get('type', '').partition(':')
        group_prefix, _, group = element.get('substitutionGroup', '').partition(':')
        if (not name.startswith('RestrictedCash') or local_type != 'monetaryItemType'
                or element.nsmap.get(prefix) != XBRLI or group != 'item'
                or element.nsmap.get(group_prefix) != XBRLI
                or element.get(f'{{{XBRLI}}}periodType') != 'instant'
                or element.get(f'{{{XBRLI}}}balance') != 'debit'
                or element.get('abstract', 'false') not in {'false', '0'}):
            continue
        declarations[key] = {
            'concept': key, 'schema_file': path.name, 'schema_sha256': hashlib.sha256(data).hexdigest(),
            'element_id': element.get('id'), 'period_type': 'instant', 'balance': 'debit',
            'type': '{' + XBRLI + '}monetaryItemType',
        }
    return {key: value for key, value in declarations.items() if key not in duplicates}


def bind_cash_reconciliations(rows, periods, facts, ns, html_ids, xml_path):
    """Return bindings only for complete, source-grounded component blocks.

    Values are never used to search for a fact. Every cell retains its own
    inline ID; sums only verify the relationship established by the heading,
    source concepts, physical rows, exact dates and common source scope.
    """
    bindings, groups = {}, []
    declarations = None
    for heading_index, heading_row in enumerate(rows):
        if not heading_row or _words(heading_row[0]['display_text']) not in HEADINGS:
            continue
        if any(local(node) == 'nonfraction' for cell in heading_row for node in cell['_element'].iter()):
            continue
        if any(cell['display_text'].strip(' \u200b') for cell in heading_row if cell['column'] > 0):
            continue
        # A component heading must immediately follow the statement's closing
        # balance, allowing only blank rows between the balance and heading.
        closing_index = heading_index - 1
        while closing_index >= 0 and not any(cell['display_text'].strip(' \u200b') for cell in rows[closing_index]):
            closing_index -= 1
        if closing_index < 0 or not rows[closing_index]:
            continue
        closing_label = _words(rows[closing_index][0]['display_text'])
        if 'cash' not in closing_label or not re.search(r'\bend of (?:the )?period\b', closing_label):
            continue
        closing = _entries(rows[closing_index], periods, facts, ns, html_ids)
        if any(not _gaap(entry['fact']['concept'], {TOTAL}) for entry in closing.values()):
            raise TableExtractionError('Cash reconciliation closing row lacks the standard cash total concept')
        if len({json.dumps(entry['scope'], sort_keys=True) for entry in closing.values()}) != 1:
            raise TableExtractionError('Cash reconciliation columns disagree on entity or currency')
        component_rows, total, total_index, schema_evidence = [], None, None, {}
        for index in range(heading_index + 1, min(heading_index + 11, len(rows))):
            row = rows[index]
            if not row or not any(cell['display_text'].strip(' \u200b') for cell in row):
                continue
            label = _words(row[0]['display_text'])
            is_total = label.startswith('total cash') or ('cash' in label and re.search(r'\bend of (?:the )?period\b', label))
            if not (is_total or label == 'cash and cash equivalents' or label.startswith('restricted cash')):
                raise TableExtractionError('An unrelated row interrupts the cash reconciliation')
            entries = _entries(row, periods, facts, ns, html_ids)
            if set(entries) != set(closing):
                raise TableExtractionError('Cash reconciliation rows disagree on physical period columns')
            for axis, entry in entries.items():
                if entry['scope'] != closing[axis]['scope']:
                    raise TableExtractionError('Cash reconciliation disagrees on entity, dimensions or currency')
                concept = entry['fact']['concept']
                if is_total:
                    valid = _gaap(concept, {TOTAL})
                elif label == 'cash and cash equivalents':
                    valid = _gaap(concept, {'CashAndCashEquivalentsAtCarryingValue'})
                elif _gaap(concept, RESTRICTED):
                    valid = True
                elif not concept.split('}', 1)[-1].startswith('RestrictedCash') or _gaap(concept, {concept.split('}', 1)[-1]}):
                    valid = False
                else:
                    if declarations is None:
                        declarations = _extension_declarations(xml_path)
                    valid = concept in declarations
                    if valid:
                        schema_evidence[concept] = declarations[concept]
                if not valid:
                    raise TableExtractionError('Cash reconciliation has an unsupported cash component concept')
            if is_total:
                total, total_index = entries, index
                break
            if not component_rows and label != 'cash and cash equivalents':
                raise TableExtractionError('Cash reconciliation must begin with cash and cash equivalents')
            if component_rows and label == 'cash and cash equivalents':
                raise TableExtractionError('Cash reconciliation repeats its cash component')
            component_rows.append(entries)
        if total is None or len(component_rows) < 2:
            raise TableExtractionError('Cash reconciliation lacks complete components and total')
        evidence = []
        heading = heading_row[0]
        for axis, closing_entry in closing.items():
            parts = [row[axis] for row in component_rows]
            concepts = [part['fact']['concept'] for part in parts]
            if len(set(concepts)) != len(concepts):
                raise TableExtractionError('Cash reconciliation repeats a component concept')
            aligned = _aligned_amounts([part['amount'] for part in parts] + [total[axis]['amount'], closing_entry['amount']])
            if sum(aligned[:-2]) != aligned[-2] or aligned[-2] != aligned[-1]:
                raise TableExtractionError('Cash reconciliation components or total differ from the source closing balance')
            period = dict(closing_entry['fact']['period'])
            row_entries = [*parts, total[axis]]
            for entry in row_entries:
                identifier = entry['cell']['cell_id']
                if identifier in bindings:
                    raise TableExtractionError('Cash reconciliation blocks overlap')
                bindings[identifier] = {'method': 'cash_reconciliation', 'period': period,
                                         'heading_source_xpath': heading['source_xpath']}
            evidence.append({'column_period': json.loads(axis), 'period': period, 'scope': closing_entry['scope'],
                             'closing_xml_id': closing_entry['fact']['xml_id'],
                             'component_xml_ids': [part['fact']['xml_id'] for part in parts],
                             'total_xml_id': total[axis]['fact']['xml_id'],
                             'cell_ids': [entry['cell']['cell_id'] for entry in row_entries]})
        groups.append({'heading_source_xpath': heading['source_xpath'], 'heading_text': heading['display_text'],
                       'first_data_row': min(entry['cell']['row'] for entry in component_rows[0].values()), 'last_data_row': total_index,
                       'periods': evidence, 'extension_concepts': [schema_evidence[key] for key in sorted(schema_evidence)]})
    from .bank_cash_components import bind_bank_cash_components
    bank_bindings, bank_groups = bind_bank_cash_components(rows, periods, facts, ns, html_ids)
    if set(bindings) & set(bank_bindings):
        raise TableExtractionError('Cash reconciliation blocks overlap')
    bindings.update(bank_bindings)
    groups.extend(bank_groups)
    return bindings, groups


def validate_cash_reconciliations(tables, tree, xml_path):
    """Recompute original source groups and compare stored bindings and facts."""
    from .tables import _column_periods, column_headers, layout_grid, xml_evidence

    facts = xml_evidence(xml_path, include_entity=True)
    ns = {key[6:]: value for key, value in tree.getroot().attrib.items() if key.startswith('xmlns:')}
    html_ids = {}
    for node in tree.iter():
        if local(node) == 'nonfraction' and node.get('id'):
            html_ids[node.get('id')] = html_ids.get(node.get('id'), 0) + 1
    for table in tables:
        if table.statement_type != 'cash_flow':
            if table.structure.get('cash_reconciliation_evidence') or any(
                    cell.get('period_binding') for row in table.structure['rows'] for cell in row):
                raise TableExtractionError('A non-cash-flow table cannot contain cash reconciliation bindings')
            continue
        nodes = tree.xpath(table.structure['source_xpath'])
        if len(nodes) != 1 or local(nodes[0]) != 'table':
            raise TableExtractionError('Cash reconciliation table source is ambiguous')
        rows, _ = layout_grid(nodes[0], tree)
        first = next(index for index, row in enumerate(rows) if any(local(node) == 'nonfraction'
                     for cell in row for node in cell['_element'].iter()))
        periods, _ = _column_periods(rows, first, facts, ns, html_ids, advanced=True)
        group = table.structure.get('statement_group') or {}
        periods.update({int(column): value for column, value in group.get('joint_statement', {}).get('column_periods', {}).items()})
        inherited = group.get('header_inheritance') or {}
        periods.update({int(column): value['period'] for column, value in inherited.get('columns', {}).items()})
        if group.get('segment_role') == 'cash_reconciliation':
            expected, evidence = {}, []
        else:
            expected, evidence = bind_cash_reconciliations(rows, periods, facts, ns, html_ids, xml_path)
        if table.structure.get('cash_reconciliation_evidence', []) != evidence:
            raise TableExtractionError('Cash reconciliation metadata differs from the original source proof')
        actual = {cell['cell_id']: cell['period_binding'] for row in table.structure['rows'] for cell in row
                  if 'period_binding' in cell}
        if actual != expected:
            raise TableExtractionError('Cash reconciliation cell bindings differ from the original source proof')
        source_cells = {cell['cell_id']: cell for row in rows for cell in row}
        for row in table.structure['rows']:
            for cell in row:
                if cell['cell_id'] not in expected:
                    continue
                original = source_cells[cell['cell_id']]
                [node] = [node for node in original['_element'].iter() if local(node) == 'nonfraction' and not _hidden(node)]
                fact = facts[node.get('id')]
                refs = cell.get('fact_references', [])
                keys = ('xml_id', 'concept', 'context_ref', 'unit_ref', 'unit', 'period', 'dimensions', 'raw_value', 'is_nil')
                if (cell.get('classification') != 'resolved' or len(refs) != 1
                        or any(refs[0].get(key) != fact[key] for key in keys)
                        or refs[0].get('method') != 'inline_id'
                        or cell.get('source_xpath') != original['source_xpath']
                        or cell.get('column_period') != periods[original['column']]
                        or cell.get('column_headings') != column_headers(rows, first, original['column'])):
                    raise TableExtractionError('Cash reconciliation cell differs from its own inline/XML occurrence')
    return True
