"""Validate original cash continuations without mixing entity or period scopes."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import re

from .cash_reconciliation import _aligned_amounts
from .joint_statement_periods import GAAP_CASH_BALANCE_ALIASES
from .source_locators import source_xpath
from .tables import TableExtractionError, _hidden, _inline_namespaces, layout_grid, local, text, verify_inline


USD = {'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []}
TOTAL = 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
COMPONENTS = ('CashAndCashEquivalentsAtCarryingValue', 'RestrictedCashAndCashEquivalents', 'ReplacementReserveEscrow')
RECONCILIATION = ('the following table provides a reconciliation of cash cash equivalents and restricted cash '
                  'reported on the balance sheet to the amount shown in the statements of cash flows')


def _require(condition, message):
    if not condition:
        raise TableExtractionError(message)


def _gaap_name(fact):
    match = re.fullmatch(r'\{https?://(?:fasb\.org|xbrl\.us)/us-gaap/(?:19|20)\d{2}\}([^}]+)', fact['concept'])
    return match[1] if match else None


def _source_references(candidate):
    table = candidate['table']
    document_ids = {}
    for node in table.getroottree().iter():
        if node.get('id'):
            document_ids.setdefault(node.get('id'), []).append(node)
    visible = {node.get('id'): node for row in layout_grid(table, table.getroottree())[0] for cell in row
               if cell['column'] > 0 for node in cell['_element'].iter()
               if local(node) == 'nonfraction' and not _hidden(node)}
    references = {}
    for values in candidate['references'].values():
        for fact in values:
            identifier = fact['xml_id']
            _require(identifier not in references and identifier in visible
                     and len(document_ids.get(identifier, [])) == 1,
                     'Joint cash continuation must retain unique original numeric occurrences.')
            node = visible[identifier]
            prefix, separator, name = str(node.tag).partition(':')
            _require(separator and name.lower() == 'nonfraction'
                     and _inline_namespaces(node).get(prefix) in {
                         'http://www.xbrl.org/2013/inlineXBRL', 'http://www.xbrl.org/2008/inlineXBRL'},
                     'Joint cash continuation requires a standard inline numeric namespace.')
            verify_inline(node, fact, {})
            references[identifier] = fact
    _require(set(references) == set(visible), 'Joint cash continuation omits visible numeric source cells.')
    return references


def _reconciliation_source(table):
    node = table
    for _ in range(3):
        for previous in list(node.itersiblings(preceding=True))[:8]:
            value = text(previous)
            if local(previous) == 'table' or previous.xpath('.//table') or any(local(child) == 'nonfraction' for child in previous.iter()):
                return None
            normalized = ' '.join(re.sub(r'[^a-z0-9 ]', ' ', value.lower()).split())
            if normalized == RECONCILIATION:
                return {'source_xpath': source_xpath(table.getroottree(), previous), 'display_text': value}
            if normalized and not re.fullmatch(r'(?:unaudited|in millions|supplemental disclosure of cash flow information in millions)', normalized):
                return None
        node = node.getparent()
        if node is None or local(node) in {'body', 'html', 'td', 'th', 'table'}:
            break
    return None


def _amount(fact):
    _require(not fact['is_nil'], 'Joint cash reconciliation cannot infer an amount from nil.')
    try:
        value = Decimal(fact['raw_value'])
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TableExtractionError('Joint cash reconciliation has an invalid source amount.') from exc
    _require(value.is_finite() and value >= 0, 'Joint cash reconciliation needs finite nonnegative balances.')
    return value


def validate_cash_continuation(main, candidate):
    """Return a role/proof for a source-proven primary cash continuation."""
    _require(main['heading'].get('kind') == candidate['heading'].get('kind') == 'cash_flow',
             'Joint cash continuation must retain cash-flow statement headings.')
    _require(main['axes'] and len(main['axes']) == len(set(main['axes'])) and len(main['scopes']) == 1
             and all(set(main['evidence'].get(axis, {})) == {'0', '1', '2', '3'} for axis in main['axes']),
             'Joint cash continuation requires one complete primary cash statement.')
    scope = json.loads(next(iter(main['scopes'])))
    _require(scope['unit'] == USD and scope['dimensions'] == [] and bool(scope['entity']),
             'Joint cash continuation requires the primary undimensioned USD scope.')
    periods = [json.loads(axis) for axis in main['axes']]
    _require(all(set(period) == {'startDate', 'endDate'} for period in periods),
             'Primary cash statement requires explicit duration axes.')
    primary = _source_references(main)
    current = _source_references(candidate)
    for axis in main['axes']:
        for group in ('0', '1', '2'):
            identifiers = main['evidence'][axis][group]
            _require(identifiers and all(identifier in primary and primary[identifier]['entity'] == scope['entity']
                     and primary[identifier]['unit'] == USD and primary[identifier]['dimensions'] == []
                     and primary[identifier]['period'] == json.loads(axis) for identifier in identifiers),
                     'Primary cash activity evidence disagrees with its source scope or duration.')
    _require(candidate['heading'].get('continued') is True and not candidate.get('unresolved'),
             'Joint cash continuation requires an explicit complete continuation heading.')
    for fact in current.values():
        _require(fact['entity'] == scope['entity'] and fact['unit'] == USD,
                 'Joint cash continuation disagrees on source entity or currency.')
        _require(not any(dimension.get('dimension', '').endswith(('}LegalEntityAxis', '}ConsolidatedEntitiesAxis'))
                         or dimension.get('member', '').endswith('}ParentCompanyMember') for dimension in fact['dimensions']),
                 'Joint cash continuation contains a separate or unconsolidated legal-entity scope.')
    if candidate['axes'] == main['axes']:
        for axis, values in candidate['references'].items():
            period = json.loads(axis)
            start = date.fromisoformat(period['startDate'])
            allowed = (period, {'instant': period['endDate']}, {'instant': (start - timedelta(days=1)).isoformat()})
            _require(all(fact['period'] in allowed for fact in values),
                     'Joint cash continuation facts disagree with its main duration columns.')
        return {'segment_role': 'statement', 'column_periods': periods}
    instants = [{'instant': period['endDate']} for period in periods]
    _require(candidate['axes'] == [json.dumps(instant, sort_keys=True) for instant in instants],
             'Joint cash reconciliation dates do not equal the primary cash closing dates.')
    heading = _reconciliation_source(candidate['table'])
    _require(heading is not None, 'An instant cash continuation needs its original explicit reconciliation caption.')
    evidence = []
    for main_axis, axis, duration, instant in zip(main['axes'], candidate['axes'], periods, instants):
        values = candidate['references'][axis]
        _require(all(fact['dimensions'] == [] and fact['period'] == instant for fact in values),
                 'Joint cash reconciliation contains dimensions or inconsistent dates.')
        by_concept = {}
        for fact in values:
            name = _gaap_name(fact)
            _require(name in {*COMPONENTS, TOTAL} and name not in by_concept,
                     'Joint cash reconciliation has an unsupported or repeated component concept.')
            by_concept[name] = fact
        _require(set(by_concept) == {*COMPONENTS, TOTAL}, 'Joint cash reconciliation lacks a complete source component set.')
        closing = [primary[identifier] for identifier in main['evidence'][main_axis]['3']
                   if identifier in primary and _gaap_name(primary[identifier]) in {TOTAL, *GAAP_CASH_BALANCE_ALIASES}
                   and primary[identifier]['period'] == instant]
        _require(len(closing) == 1 and closing[0]['entity'] == scope['entity']
                 and closing[0]['unit'] == USD and closing[0]['dimensions'] == [],
                 'Joint cash reconciliation lacks a unique native primary closing occurrence.')
        components = [by_concept[name] for name in COMPONENTS]
        total = by_concept[TOTAL]
        amounts = _aligned_amounts([_amount(fact) for fact in [*components, total, closing[0]]])
        _require(sum(amounts[:-2]) == amounts[-2] == amounts[-1],
                 'Joint cash components or total do not equal the original primary closing balance.')
        evidence.append({'column_period': duration, 'period': instant, 'scope': scope,
                         'main_closing_xml_id': closing[0]['xml_id'],
                         'component_xml_ids': [fact['xml_id'] for fact in components], 'total_xml_id': total['xml_id']})
    return {'segment_role': 'cash_reconciliation', 'reconciliation_source': heading,
            'periods': evidence, 'verified_xml_ids': sorted(current)}
