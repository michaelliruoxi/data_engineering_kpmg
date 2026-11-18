"""Prove an explicitly continued assets/liabilities balance-sheet pair.

Core totals must share one undimensioned entity/currency. Detail dimensions are
retained on their original cells and never substituted for consolidated totals.
"""

from decimal import Decimal
import json
import re

from .source_locators import source_xpath


_GAAP = r'https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^/]+'


def _only_page_furniture_between(tree, left, right):
    """Nothing but footnotes, page furniture and the repeated title separates two tables.

    No table with a number or with more than a page footer's text, no title of
    another statement and no note heading may stand between them.
    """
    from .tables import _hidden, _statement_heading, local, text

    nodes = list(tree.iter())
    positions = {node: index for index, node in enumerate(nodes)}
    start, end = max(positions[node] for node in left.iter()) + 1, positions[right]
    if start > end:
        return False
    length = 0
    for node in nodes[start:end]:
        if not isinstance(node.tag, str) or _hidden(node):
            continue
        if local(node) == 'table' and (len(text(node)) > 240 or any(local(child) == 'nonfraction' for child in node.iter())):
            return False
        for piece in (node.text, node.tail):
            value = ' '.join((piece or '').split())
            length += len(value)
            if (_statement_heading(value) not in {None, 'balance_sheet'}
                    or re.match(r'^(?:note\s+\d+\b|notes to\b)', value, re.I)):
                return False
    return length <= 2000


def tied_balance_segments(tree, candidates):
    """Return an assets table and a liabilities-and-equity table tied by their totals.

    tables-v3 corroborated layouts only, for a balance sheet printed as two
    tables without the words `verified_balance_segments` needs. Both tables
    must carry the same title, report the same dates for one entity, and have
    only page furniture between them. The first holds the assets (and at most
    the liabilities), the second the liabilities and equity; and for every
    date the one undimensioned Assets fact of the first equals the one
    undimensioned LiabilitiesAndStockholdersEquity fact of the second, in one
    currency.
    """
    from .tables import StatementSegment

    if len(candidates) != 2:
        return None
    first, second = candidates
    if (not first['axes'] or first['axes'] != second['axes'] or first['continued']
            or any(set(json.loads(axis)) != {'instant'} for axis in first['axes'])
            or any(groups not in ({0}, {0, 1}) for groups in first['coverage'].values())
            or any(groups != {1, 2} for groups in second['coverage'].values())
            or not first['headings'].intersection(second['headings'])
            or not _only_page_furniture_between(tree, first['table'], second['table'])):
        return None
    core_scope, totals, groups = None, [], []
    for axis in first['axes']:
        period = json.loads(axis)
        tied = {}
        for name, candidate in (('Assets', first), ('LiabilitiesAndStockholdersEquity', second)):
            matches = [ref for ref in candidate['references'][axis] if not ref['dimensions']
                       and re.fullmatch(_GAAP, ref['concept'][1:].split('}')[0]) and ref['concept'].split('}')[-1] == name]
            if len(matches) != 1 or matches[0]['is_nil'] or matches[0]['period'] != period:
                return None
            scope = {key: matches[0][key] for key in ('entity', 'unit', 'dimensions')}
            if (scope['entity'].get('scheme') not in {'http://www.sec.gov/CIK', 'https://www.sec.gov/CIK'}
                    or not re.fullmatch(r'[0-9]{1,10}', scope['entity'].get('identifier', ''))
                    or not isinstance(scope['unit'], dict) or scope['unit'].get('denominator') != []
                    or len(scope['unit'].get('numerator', [])) != 1
                    or not re.fullmatch(r'\{http://www.xbrl.org/2003/iso4217\}[A-Z]{3}', scope['unit']['numerator'][0])):
                return None
            if core_scope is None:
                core_scope = scope
            elif scope != core_scope:
                return None
            tied[name] = matches[0]
        # Every cell of both tables belongs to that entity and date.
        if any(ref['entity'] != core_scope['entity'] or ref['period'] != period
               for ref in [*first['references'][axis], *second['references'][axis]]):
            return None
        amounts = [Decimal(ref['raw_value']) for ref in tied.values()]
        if any(not amount.is_finite() for amount in amounts) or amounts[0] != amounts[1]:
            return None
        totals.append({'period': period, 'facts': {name: ref['xml_id'] for name, ref in tied.items()},
                       'raw_values': {name: ref['raw_value'] for name, ref in tied.items()}})
        combined = {group: list(identities) for group, identities in first['evidence'][axis].items()}
        for group, identities in second['evidence'][axis].items():
            combined.setdefault(group, []).extend(identities)
        groups.append({'period': period, 'groups': combined})
    keys = ['balance_sheet.segment_1', 'balance_sheet.segment_2']
    common = {'group_key': 'balance_sheet', 'segment_count': 2, 'segment_keys': keys,
              'source_xpaths': [source_xpath(tree, candidate['table']) for candidate in candidates],
              'heading': sorted(first['headings'].intersection(second['headings']))[0],
              'column_periods': [json.loads(axis) for axis in first['axes']], 'scope': core_scope,
              'combined_concept_evidence': groups,
              'balance_continuation': {'verification_version': 'tied-balance-totals-v1', 'totals': totals,
                                       'detail_scopes': sorted(first['scopes'] | second['scopes'])}}
    return tuple(StatementSegment('balance_sheet', key, candidate['table'], common | {'segment_index': index + 1})
                 for index, (key, candidate) in enumerate(zip(keys, candidates)))


def verified_balance_segments(tree, candidates, facts):
    """Return two original segments only when source continuation is explicit."""
    from .tables import StatementSegment, _segments_adjacent, layout_grid, local
    from .cash_reconciliation import _aligned_amounts

    if len(candidates) != 2:
        return None
    first, second = candidates
    if (not first['axes'] or first['axes'] != second['axes'] or first['continued']
            or any(set(json.loads(axis)) != {'instant'} for axis in first['axes'])
            or any(groups != {0} for groups in first['coverage'].values())
            or any(groups != {1, 2} for groups in second['coverage'].values())
            or not first['headings'].intersection(second['headings'])
            or not _segments_adjacent(tree, first['table'], second['table'], 'balance_sheet')):
        return None
    nodes = list(tree.iter())
    positions = {node: index for index, node in enumerate(nodes)}
    start = max(positions[node] for node in first['table'].iter()) + 1
    stop = positions[second['table']]
    forward, continued = [], []
    for node in nodes[start:stop]:
        if not isinstance(node.tag, str):
            continue
        text = ' '.join((node.text or '').split())
        if text.lower().rstrip('.') == 'statement continues on the next page':
            forward.append({'source_xpath': source_xpath(tree, node), 'text': text})
        if re.fullmatch(r'\(?continued\)?\.?', text, re.I):
            continued.append({'source_xpath': source_xpath(tree, node), 'text': text})
    if not forward or not continued:
        return None
    layouts = [layout_grid(candidate['table'], tree) for candidate in candidates]
    if any(not grid for _, grid in layouts) or len(layouts[0][1][0]) != len(layouts[1][1][0]):
        return None
    for index, (rows, _) in enumerate(layouts):
        data = next((row_index for row_index, row in enumerate(rows) if any(
            local(node) == 'nonfraction' for cell in row for node in cell['_element'].iter())), None)
        if data is None:
            return None
        labels = [re.sub(r'[^a-z ]', '', cell['display_text'].lower()).strip()
                  for row in rows[:data] for cell in row if cell['column'] == 0]
        if ('assets' if index == 0 else 'liabilities') not in labels:
            return None

    core_scope = None
    totals = []
    groups = []
    for axis in first['axes']:
        period = json.loads(axis)
        all_refs = [*first['references'][axis], *second['references'][axis]]
        required = {}
        for name, candidate in [('Assets', first), ('LiabilitiesAndStockholdersEquity', second), ('Liabilities', second)]:
            matches = [ref for ref in candidate['references'][axis]
                       if re.fullmatch(r'https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^/]+', ref['concept'][1:].split('}')[0])
                       and ref['concept'].split('}')[-1] == name]
            if len(matches) != 1 or matches[0]['is_nil'] or matches[0]['period'] != period or matches[0]['dimensions']:
                return None
            required[name] = matches[0]
        equity = [ref for ref in second['references'][axis]
                  if ref['concept'].split('}')[-1] == 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest']
        if not equity:
            equity = [ref for ref in second['references'][axis] if ref['concept'].split('}')[-1] == 'StockholdersEquity']
        if len(equity) != 1 or equity[0]['is_nil'] or equity[0]['dimensions'] or equity[0]['period'] != period:
            return None
        required['Equity'] = equity[0]
        for ref in required.values():
            scope = {key: ref[key] for key in ('entity', 'unit', 'dimensions')}
            if (not re.fullmatch(r'https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^/]+', ref['concept'][1:].split('}')[0])
                    or scope['entity'].get('scheme') not in {'http://www.sec.gov/CIK', 'https://www.sec.gov/CIK'}
                    or not re.fullmatch(r'[0-9]{1,10}', scope['entity'].get('identifier', ''))
                    or not isinstance(scope['unit'], dict) or scope['unit'].get('denominator') != []
                    or len(scope['unit'].get('numerator', [])) != 1
                    or not re.fullmatch(r'\{http://www.xbrl.org/2003/iso4217\}[A-Z]{3}', scope['unit']['numerator'][0])):
                return None
            if core_scope is None:
                core_scope = scope
            elif scope != core_scope:
                return None
        # Dimensional detail may vary, but its entity, currency and date may not.
        if any(ref['entity'] != core_scope['entity'] or ref['unit'] != core_scope['unit'] or ref['period'] != period for ref in all_refs):
            return None
        amounts = {name: Decimal(ref['raw_value']) for name, ref in required.items()}
        if any(not value.is_finite() for value in amounts.values()):
            return None
        assets, total, liabilities, equity_amount = _aligned_amounts([amounts[name] for name in
            ('Assets', 'LiabilitiesAndStockholdersEquity', 'Liabilities', 'Equity')])
        if assets != total or liabilities + equity_amount != assets:
            return None
        totals.append({'period': period, 'facts': {name: ref['xml_id'] for name, ref in required.items()},
                       'raw_values': {name: ref['raw_value'] for name, ref in required.items()}})
        groups.append({'period': period, 'groups': {**first['evidence'][axis], **second['evidence'][axis]}})
    keys = ['balance_sheet.segment_1', 'balance_sheet.segment_2']
    common = {'group_key': 'balance_sheet', 'segment_count': 2, 'segment_keys': keys,
              'source_xpaths': [source_xpath(tree, candidate['table']) for candidate in candidates],
              'heading': sorted(first['headings'].intersection(second['headings']))[0],
              'column_periods': [json.loads(axis) for axis in first['axes']], 'scope': core_scope,
              'combined_concept_evidence': groups,
              'balance_continuation': {'verification_version': 'explicit-balance-continuation-v1',
                                       'forward_statements': forward, 'continued_markers': continued, 'totals': totals,
                                       'detail_scopes': sorted(first['scopes'] | second['scopes'])}}
    return tuple(StatementSegment('balance_sheet', key, candidate['table'], common | {'segment_index': index + 1})
                 for index, (key, candidate) in enumerate(zip(keys, candidates)))
