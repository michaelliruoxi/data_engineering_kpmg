"""Statements the established selection rejects for their page layout.

tables-v3 `extended` selection only, and only for a statement kind the
established rules found "ambiguous or incomplete", so a filing they accept
keeps the selection it has. Two layouts are read here:

* a note repeats a statement's title above a table that holds none of the
  statement's totals (`without_note_tables`);
* a statement runs on to the next page in a table printed without a title,
  or under the statement's title without column dates (`page_continuation`);
* another statement stands under the same title, every number of which
  carries a dimension: the statement repeated for a business group, or that
  of a second registrant (`with_own_scope`);
* a complete cash flow statement is followed by pages that only supplement
  it (`statement_before_supplements`).
"""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import re

from .source_locators import source_xpath


_GAAP = r'\{https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^}]+\}'
_CIK = {'http://www.sec.gov/CIK', 'https://www.sec.gov/CIK'}
_ACTIVITIES = (('operating', 'NetCashProvidedByUsedInOperatingActivities'),
               ('investing', 'NetCashProvidedByUsedInInvestingActivities'),
               ('financing', 'NetCashProvidedByUsedInFinancingActivities'))
_NET_CHANGE = re.compile(r'(?:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents|CashAndCashEquivalents)'
                         r'PeriodIncreaseDecrease(?:IncludingExchangeRateEffect|ExcludingExchangeRateEffect)?')
_NET_INCOME = ('ProfitLoss', 'NetIncomeLoss', 'IncomeLossFromContinuingOperations',
               'IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest')
VERIFICATION_VERSION = 'page-continuation-v1'


def in_note(table):
    """The table lies inside a tagged text block: a note or a policy, never a primary statement."""
    from .tables import local

    return any(local(node) in {'nonnumeric', 'continuation'} for node in table.iterancestors())


def without_note_tables(candidates):
    """Leave out tables of a note that carry a statement's title and none of its totals."""
    return [candidate for candidate in candidates
            if candidate['continued'] or any(candidate['coverage'].values()) or not in_note(candidate['table'])]


def _name(ref):
    return ref['concept'].split('}', 1)[-1]


def _amount(ref):
    try:
        amount = Decimal(ref['raw_value'])
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() else None


def _core(refs):
    """Reported standard facts of the whole entity: no dimension, no nil, a finite amount."""
    return [ref for ref in refs if not ref['dimensions'] and not ref['is_nil'] and re.match(_GAAP, ref['concept'])
            and _amount(ref) is not None]


def _one(refs):
    """The fact a line reports, when every tag of that line gives the same amount."""
    return refs[0] if refs and len({_amount(ref) for ref in refs}) == 1 else None


def _cash_tie(period, first_refs, second_refs):
    """Opening cash plus the activity totals and the exchange-rate effect equals closing cash."""
    from .tables import _CASH_TOTAL_CONCEPTS

    opening_at = {'instant': (date.fromisoformat(period['startDate']) - timedelta(days=1)).isoformat()}
    closing_at = {'instant': period['endDate']}
    refs = _core([*first_refs, *second_refs])
    used = {}
    for label, concept in _ACTIVITIES:
        used[label] = (_one([ref for ref in refs if _name(ref) == concept and ref['period'] == period])
                       or _one([ref for ref in refs if _name(ref) == concept + 'ContinuingOperations'
                                and ref['period'] == period]))
        if used[label] is None:
            return None
    flows = sum(_amount(used[label]) for label, _ in _ACTIVITIES)
    effects = [ref for ref in refs if _name(ref).startswith('EffectOfExchangeRateOn') and ref['period'] == period]
    effect = Decimal(0)
    if effects:
        if _one(effects) is None:
            return None
        used['exchange_rate_effect'], effect = effects[0], _amount(effects[0])
    # Both balances under one total cash concept first. Only where no concept
    # gives both: the balance a period opens with may be reported under
    # another total cash concept than the one it closes with, as where a
    # disposal group held cash at one of the two dates only.
    pairs = [(concept, concept) for concept in _CASH_TOTAL_CONCEPTS]
    pairs += [(at_start, at_end) for at_start in _CASH_TOTAL_CONCEPTS for at_end in _CASH_TOTAL_CONCEPTS if at_start != at_end]
    for at_start, at_end in pairs:
        opening = _one([ref for ref in refs if _name(ref) == at_start and ref['period'] == opening_at])
        closing = _one([ref for ref in refs if _name(ref) == at_end and ref['period'] == closing_at])
        if opening is not None and closing is not None and _amount(opening) + flows + effect == _amount(closing):
            used['opening'], used['closing'] = opening, closing
            break
    else:
        return None
    # A printed net change must be the one these totals give.
    for ref in refs:
        if ref['period'] == period and _NET_CHANGE.fullmatch(_name(ref)):
            expected = flows if _name(ref).endswith('ExcludingExchangeRateEffect') else flows + effect
            if _amount(ref) != expected:
                return None
            used.setdefault('net_change', ref)
    return used


def _income_tie(period, first_refs, second_refs):
    """Income before income taxes less income tax expense equals a net income line of the second table."""
    first = [ref for ref in _core(first_refs) if ref['period'] == period]
    tax = _one([ref for ref in first if _name(ref) == 'IncomeTaxExpenseBenefit'])
    if tax is None:
        return None
    for before in first:
        if not _name(before).startswith('IncomeLossFromContinuingOperationsBeforeIncomeTaxes'):
            continue
        for ref in _core(second_refs):
            if (ref['period'] == period and _name(ref) in _NET_INCOME
                    and _amount(before) - _amount(tax) == _amount(ref)):
                return {'before_income_taxes': before, 'income_tax': tax, 'net_income': ref}
    return None


def _scope(refs):
    """The one registrant and currency the tied facts share, or None."""
    scopes = {json.dumps({key: ref[key] for key in ('entity', 'unit', 'dimensions')}, sort_keys=True) for ref in refs}
    if len(scopes) != 1:
        return None
    scope = json.loads(next(iter(scopes)))
    entity, unit = scope['entity'], scope['unit']
    if (not isinstance(entity, dict) or entity.get('scheme') not in _CIK
            or not re.fullmatch(r'[0-9]{1,10}', entity.get('identifier') or '')
            or not isinstance(unit, dict) or unit.get('denominator') != [] or len(unit.get('numerator') or []) != 1
            or not re.fullmatch(r'\{http://www.xbrl.org/2003/iso4217\}[A-Z]{3}', unit['numerator'][0])):
        return None
    return scope


def page_continuation(tree, kind, first, table, facts, html_ids, concept_groups, *, spanned_cells=False):
    """Return a titled table and the untitled table that continues it on the next page.

    `first` is the one incomplete titled table of its kind and `table` the
    next table of the document, for which no reading finds a title, or which
    stands under the statement's title with columns no header dates. Both
    lie outside the notes and only page furniture separates them. The second
    table either prints the first table's column periods again, or prints no
    column headers and has the same columns, in which case each column takes
    the period the first table gives it. It has the same columns when both
    grids are equally wide, or when every column it prints a number in is one
    the first table prints a number in; a caption that begins in the label
    column and names no period is no column header. Both report one entity;
    together they hold every concept group, and (except for a liabilities
    total) no group twice. The statement's own arithmetic must then hold
    across the page break for every period:

    * balance sheet: the one total assets fact of the first table equals the
      one total liabilities and equity fact of the second
      (`balance_segments.tied_balance_segments`);
    * cash flow: opening cash plus the three activity totals and the
      exchange-rate effect equals closing cash, both balances under one total
      cash concept or, where none gives both, under two, and a tagged net
      change agrees with those totals;
    * income statement: income before income taxes less income tax expense,
      both in the first table, equals a net income line of the second.

    `spanned_cells` reads the second table as the very last selection
    attempts read every table (`tables._segment_candidate`).
    """
    from .tables import (StatementSegment, TableExtractionError, _column_header_cells, _column_periods,
                         _page_furniture_between, _period_caption, _segment_candidate, layout_grid, local)

    required = set(range(len(concept_groups[kind])))
    shape = {'instant'} if kind == 'balance_sheet' else {'startDate', 'endDate'}
    covered = set().union(*first['coverage'].values()) if first['coverage'] else set()
    if (not first['axes'] or not first['headings'] or first['continued'] or not covered or covered >= required
            or any(groups != covered for groups in first['coverage'].values())
            or any(set(json.loads(axis)) != shape for axis in first['axes'])
            or in_note(first['table']) or in_note(table)
            or not _page_furniture_between(tree, first['table'], table, kind)):
        return None

    def numeric(row):
        return any(local(node) == 'nonfraction' for cell in row for node in cell['_element'].iter())

    first_rows, first_grid = layout_grid(first['table'], tree)
    rows, grid = layout_grid(table, tree)
    first_data = next(index for index, row in enumerate(first_rows) if numeric(row))
    data = next((index for index, row in enumerate(rows) if numeric(row)), None)
    if data is None or not grid or not first_grid:
        return None
    columns = sorted({cell['column'] for row in rows[data:] for cell in row if cell['column'] > 0})

    def numbered(table_rows, start):
        return {cell['column'] for row in table_rows[start:] for cell in row
                if any(local(node) == 'nonfraction' for node in cell['_element'].iter())}

    def read(inherit):
        inherited = {}
        if inherit:
            periods, _ = _column_periods(first_rows, first_data, facts, {}, html_ids, advanced=True, corroborate=True)
            for column in columns:
                period = periods.get(column)
                if period is not None and set(period) == shape:
                    inherited[str(column)] = {'period': period, 'source_cells': [
                        {'source_xpath': cell['source_xpath'], 'display_text': cell['display_text'],
                         'row': cell['row'], 'column': cell['column'], 'colspan': cell['colspan']}
                        for row in first_rows[:first_data] for cell in row
                        if cell['display_text'] and cell['column'] <= column < cell['column'] + cell['colspan']]}
        try:
            # Inherited headers establish their own periods; corroboration never adds to them.
            candidate = _segment_candidate(
                tree, table, kind, facts, html_ids, concept_groups=concept_groups, corroborate=not inherited,
                inherited_periods={int(column): value['period'] for column, value in inherited.items()} or None,
                spanned_cells=spanned_cells)
        except TableExtractionError:
            return None, inherited
        return (candidate if candidate is not None and candidate['axes'] == first['axes'] else None), inherited

    # The second page has the first page's columns when both grids are equally
    # wide. Grids that differ only in empty columns are not: there every column
    # the second page prints a number in must be one the first page prints a
    # number in.
    same_columns = (len(first_grid[0]) == len(grid[0])
                    or numbered(rows, data) <= numbered(first_rows, first_data))
    headers = [cell for column in columns for cell in _column_header_cells(rows, data, column)]
    if headers:
        second, inherited = read(False)
        # A caption that begins in the label column and names no period, such
        # as the statement's title repeated inside the table, heads the page,
        # not a column: where it dates nothing, the page has no column headers.
        if (second is None and same_columns
                and all(cell['column'] == 0 and not _period_caption(cell['display_text']) for cell in headers)):
            second, inherited = read(True)
    elif same_columns:
        second, inherited = read(True)
    else:
        return None
    if second is None:
        return None
    entities = {json.dumps(ref['entity'], sort_keys=True) for candidate in (first, second)
                for refs in candidate['references'].values() for ref in refs}
    if len(entities) != 1:
        return None
    second = second | {'headings': set(first['headings']), 'continued': False}
    evidence = {'verification_version': VERIFICATION_VERSION, 'untitled_table': source_xpath(tree, table)}
    if kind == 'balance_sheet':
        from .balance_segments import tied_balance_segments

        tied = tied_balance_segments(tree, [first, second])
        if tied is None:
            return None
        common = {key: value for key, value in tied[0].group.items() if key != 'segment_index'}
    else:
        if any(first['coverage'][axis] & second['coverage'][axis]
               or first['coverage'][axis] | second['coverage'][axis] != required for axis in first['axes']):
            return None
        tie = _cash_tie if kind == 'cash_flow' else _income_tie
        ties, tied_refs, groups = [], [], []
        for axis in first['axes']:
            period = json.loads(axis)
            used = tie(period, first['references'][axis], second['references'][axis])
            if used is None:
                return None
            tied_refs.extend(used.values())
            ties.append({'period': period, 'facts': {name: ref['xml_id'] for name, ref in used.items()},
                         'raw_values': {name: ref['raw_value'] for name, ref in used.items()}})
            combined = {group: list(identities) for group, identities in first['evidence'][axis].items()}
            for group, identities in second['evidence'][axis].items():
                combined.setdefault(group, []).extend(identities)
            groups.append({'period': period, 'groups': combined})
        scope = _scope(tied_refs)
        if scope is None:
            return None
        keys = [f'{kind}.segment_1', f'{kind}.segment_2']
        common = {'group_key': kind, 'segment_count': 2, 'segment_keys': keys,
                  'source_xpaths': [source_xpath(tree, first['table']), source_xpath(tree, table)],
                  'heading': sorted(first['headings'])[0],
                  'column_periods': [json.loads(axis) for axis in first['axes']], 'scope': scope,
                  'combined_concept_evidence': groups}
        evidence |= {'ties': ties, 'detail_scopes': sorted(first['scopes'] | second['scopes'])}
    common['page_continuation'] = evidence
    inheritance = ({'header_inheritance': {'source_table_key': common['segment_keys'][0],
                                           'source_xpath': source_xpath(tree, first['table']), 'columns': inherited}}
                   if inherited else {})
    return (StatementSegment(kind, common['segment_keys'][0], first['table'], common | {'segment_index': 1}),
            StatementSegment(kind, common['segment_keys'][1], table, common | {'segment_index': 2} | inheritance))


def with_own_scope(candidates):
    """Leave out tables none of whose numbers has the registrant's own scope.

    A primary statement needs totals without any dimension. A table whose
    every number carries one is another statement under the same title: the
    statement repeated for a business group on a page marked as a
    continuation, or the statement of a second registrant such as the
    operating partnership of a real estate trust. It is left out when another
    candidate holds numbers without a dimension; the statement is then chosen
    among the rest as before.
    """
    own = [candidate for candidate in candidates
           if not candidate['scopes'] or any(not json.loads(scope)['dimensions'] for scope in candidate['scopes'])]
    return own if any(candidate['scopes'] for candidate in own) else list(candidates)


def statement_before_supplements(tree, kind, candidates, required):
    """A complete cash flow statement followed by pages that only supplement it, or None.

    A cash flow statement often runs on to a page of supplemental disclosures
    or a reconciliation of its closing cash balance, printed under the same
    title or marked as its continuation, that cannot be joined to it as a
    statement page: its columns are dated differently, or it is not marked.
    The first candidate must be complete, with every activity total and the
    closing cash balance, and not marked as a continuation. Every other
    candidate must stand after it under the same title, report the same
    entity, hold none of the three activity totals, and be separated from the
    table before it by nothing but page furniture. The statement is then the
    first table alone.
    """
    from .tables import _page_furniture_between

    if kind != 'cash_flow' or len(candidates) < 2:
        return None
    main, others = candidates[0], candidates[1:]
    if (main['continued'] or not main['axes'] or not main['headings'] or in_note(main['table'])
            or any(groups != required for groups in main['coverage'].values())):
        return None
    entities = {json.dumps(json.loads(scope)['entity'], sort_keys=True) for scope in main['scopes']}
    previous = main
    for other in others:
        if (not other['headings'].intersection(main['headings']) or in_note(other['table'])
                or any(groups & {0, 1, 2} for groups in other['coverage'].values())
                or {json.dumps(json.loads(scope)['entity'], sort_keys=True) for scope in other['scopes']} - entities
                or not _page_furniture_between(tree, previous['table'], other['table'], kind)):
            return None
        previous = other
    return main


CONTINUED_PAGES_VERSION = 'continued-pages-v1'


def continued_pages(tree, kind, candidates, required):
    """A complete income statement and the pages marked as its continuation, or None.

    An income statement often runs on to a page printed under the same title
    and marked "(continued)": the allocation of net income, earnings per
    share, or comprehensive income. The first candidate must be complete,
    with every concept group in every column, and not marked as a
    continuation. Every other candidate must be marked as one and share the
    first table's title, stand after the table before it with nothing but
    page furniture between them, have the first table's column periods,
    report the same entity and hold a number of the registrant's own scope.
    No table may lie inside a note. The statement is then all of these
    tables, in the order they are printed.
    """
    from .tables import StatementSegment, _page_furniture_between

    if kind != 'income_statement' or len(candidates) < 2:
        return None
    main, pages = candidates[0], candidates[1:]
    if (main['continued'] or not main['axes'] or not main['headings'] or in_note(main['table'])
            or any(groups != required for groups in main['coverage'].values())):
        return None

    def entities(candidate):
        return {json.dumps(json.loads(scope)['entity'], sort_keys=True) for scope in candidate['scopes']}

    entity = entities(main)
    if len(entity) != 1:
        return None
    titles = set(main['headings'])
    previous = main
    for page in pages:
        titles &= page['headings']
        if (not page['continued'] or not titles or in_note(page['table']) or page['axes'] != main['axes']
                or entities(page) != entity or not any(not json.loads(scope)['dimensions'] for scope in page['scopes'])
                or not _page_furniture_between(tree, previous['table'], page['table'], kind)):
            return None
        previous = page
    keys = [f'{kind}.segment_{index + 1}' for index in range(len(candidates))]
    groups = []
    for axis in main['axes']:
        combined = {}
        for candidate in candidates:
            for group, identities in candidate['evidence'][axis].items():
                combined.setdefault(group, []).extend(identities)
        groups.append({'period': json.loads(axis), 'groups': {group: sorted(set(identities))
                                                              for group, identities in sorted(combined.items())}})
    common = {'group_key': kind, 'segment_count': len(candidates), 'segment_keys': keys,
              'source_xpaths': [source_xpath(tree, candidate['table']) for candidate in candidates],
              'heading': sorted(titles)[0], 'column_periods': [json.loads(axis) for axis in main['axes']],
              'continued_pages': {'verification_version': CONTINUED_PAGES_VERSION, 'entity': json.loads(next(iter(entity)))},
              'combined_concept_evidence': groups}
    return tuple(StatementSegment(kind, key, candidate['table'], common | {'segment_index': index + 1})
                 for index, (key, candidate) in enumerate(zip(keys, candidates)))


def recover(tree, kind, candidates, untitled, facts, html_ids, concept_groups, *, spanned_cells=False):
    """Select a statement the established rules leave "ambiguous or incomplete", or return None.

    Tables none of whose numbers has the registrant's own scope are left out
    first (`with_own_scope`), then the note tables that hold none of the
    statement's totals (`without_note_tables`). If tables were left out, one
    complete table that no other candidate shares a title with is the
    statement, as the established rule has it.
    Otherwise a titled table that shares its title with no other candidate
    may continue on the next page (`page_continuation`); exactly one must.
    A cash flow statement is read this way only as the single candidate left.
    `untitled` holds the tables that may be such a page: those no reading
    finds a title for, and those under the statement's title whose columns no
    header dates.
    """
    from .tables import StatementSegment, _hidden, local

    required = set(range(len(concept_groups[kind])))
    listed = len(candidates)
    candidates = with_own_scope(candidates)
    kept = without_note_tables(candidates)

    def alone(candidate):
        return (not candidate['continued'] and (kind != 'cash_flow' or len(kept) == 1)
                and all(other is candidate or (not other['continued']
                        and not other['headings'].intersection(candidate['headings'])) for other in kept))

    complete = [candidate for candidate in kept
                if candidate['axes'] and all(groups == required for groups in candidate['coverage'].values())]
    if complete:
        if (len(kept) != listed and len(complete) == 1 and alone(complete[0])
                and not in_note(complete[0]['table'])):
            return (StatementSegment(kind, kind, complete[0]['table']),)
        return None
    tables = [table for table in tree.xpath('//table') if not _hidden(table) and not table.xpath('.//table')]
    positions = {table: index for index, table in enumerate(tables)}
    found = []
    for first in kept:
        if not alone(first):
            continue
        # The next table that holds a number; a layout table before it counts
        # as page furniture, which `page_continuation` checks.
        following = next((table for table in tables[positions[first['table']] + 1:]
                          if any(local(node) == 'nonfraction' for node in table.iter())), None)
        if following is None or following not in untitled:
            continue
        segments = page_continuation(tree, kind, first, following, facts, html_ids, concept_groups,
                                     spanned_cells=spanned_cells)
        if segments is not None:
            found.append(segments)
    return found[0] if len(found) == 1 else None
