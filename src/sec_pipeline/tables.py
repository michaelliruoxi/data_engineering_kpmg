"""Conservative primary statement extraction from inline-XBRL filings.

No HTTP requests or database writes occur during extraction. Layout and XML
values are separate: a displayed cash outflow may have a positive XBRL value.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from collections.abc import Mapping
import calendar
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID

from lxml import etree, html

from .database import FactInput, FinancialTableInput, store_financial_table, link_table_fact
from .dei_namespaces import DEI_NAMESPACE
from .table_cells import TableCellError, relational_cells, store_table_cells
from .source_locators import inline_source_xpath, source_xpath

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


SEC_COVER_NAMESPACES = 'http://xbrl.sec.gov/'


def xml_evidence(path, *, include_entity=False, unidentified_cover_facts=False):
    """Read evidence for linking; this is not the team's FactInput extractor.

    Every fact needs an id, because a statement cell names its fact by it.
    `unidentified_cover_facts` (tables-v3 only): some filing agents give the
    cover-page tags no id. A fact without an id whose concept belongs to one
    of the SEC's own taxonomies (document and entity information and the like)
    is then read under the fact extractor's positional occurrence key
    (`xpath:/*/*[13]`), so the selection sees the same registrant, form and
    period facts as in a filing whose agent wrote the ids. No inline tag
    carries such a key, so no statement cell can name the fact. Any other
    fact without an id is refused as before.
    """
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
                # The same exclusive canonical XML as the fact extractor, so a
                # cell's reference states the dimension exactly as its stored
                # fact does. A plain serialization also carries every namespace
                # declared on the document root; such a reference never matched
                # its fact, and no filing with one was ever accepted.
                dims.append({'kind': 'typed', 'dimension': expanded(d.get('dimension'), d.nsmap),
                             'value': ''.join(etree.tostring(child, method='c14n', exclusive=True, with_comments=False).decode('utf-8')
                                              for child in d if isinstance(child.tag, str))})
        contexts[c.get('id')] = {'period': period, 'dimensions': dims}
        if include_entity:
            identifier = c.find(f'{{{XBRLI}}}entity/{{{XBRLI}}}identifier')
            if identifier is None:
                raise TableExtractionError('Statement grouping requires context entity identity')
            contexts[c.get('id')]['entity'] = {'scheme': identifier.get('scheme'), 'identifier': text(identifier)}
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
    position = 0
    for f in root:
        if not isinstance(f.tag, str):
            continue
        position += 1  # as the fact extractor counts: every element child of the root
        if not f.get('contextRef'):
            continue
        fid = f.get('id')
        if (unidentified_cover_facts and fid is None
                and (etree.QName(f).namespace or '').startswith(SEC_COVER_NAMESPACES)):
            fid = f'xpath:/*/*[{position}]'
        if not fid or fid in facts:
            raise TableExtractionError('Missing or duplicate XML fact ID')
        facts[fid] = {'xml_id': fid, 'concept': f.tag, 'context_ref': f.get('contextRef'),
                      'unit_ref': f.get('unitRef'), 'unit': units.get(f.get('unitRef')),
                      'raw_value': text(f), 'is_nil': f.get(f'{{{XSI}}}nil') in ('true', '1'),
                      **contexts[f.get('contextRef')]}
    return facts


def statement_fact_records(facts):
    """Fact records keyed as `xml_evidence(..., unidentified_cover_facts=True)` keys their XML."""
    return _segment_facts(facts, unidentified_cover_facts=True)


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
                          'source_xpath': source_xpath(tree, element), 'source_html_id': element.get('id'),
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


_STATEMENT_CONCEPTS = {
    'income_statement': (
        {'Revenues', 'SalesRevenueNet', 'SalesRevenueGoodsNet', 'SalesRevenueServicesNet',
         'RevenueFromContractWithCustomerExcludingAssessedTax',
         'RevenueFromContractWithCustomerIncludingAssessedTax',
         'InterestAndDividendIncomeOperating', 'InterestIncomeExpenseNet'},
        {'NetIncomeLoss', 'ProfitLoss', 'NetIncomeLossAvailableToCommonStockholdersBasic'},
        {'OperatingIncomeLoss', 'IncomeTaxExpenseBenefit', 'CostOfRevenue',
         'CostOfGoodsAndServicesSold', 'CostsAndExpenses',
         'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest'},
    ),
    'balance_sheet': (
        {'Assets'},
        {'Liabilities', 'LiabilitiesAndStockholdersEquity'},
        {'StockholdersEquity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest',
         'PartnersCapital', 'PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest'},
    ),
    'cash_flow': (
        {'NetCashProvidedByUsedInOperatingActivities', 'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'},
        {'NetCashProvidedByUsedInInvestingActivities', 'NetCashProvidedByUsedInInvestingActivitiesContinuingOperations'},
        {'NetCashProvidedByUsedInFinancingActivities', 'NetCashProvidedByUsedInFinancingActivitiesContinuingOperations'},
        {'CashAndCashEquivalentsAtCarryingValue', 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'},
    ),
}


# tables-v3 corroborated layouts only. A statement that reports discontinued
# operations or assets held for sale closes on the standard total that
# includes their cash. The strict alias selection (statement_aliases) accepts
# that concept under a few exact row labels; this catalog is tried after it,
# and each such cell must still pass `_endpoint_balance_period` or a labelled
# opening/closing rule when the statement is extracted.
_CORROBORATED_STATEMENT_CONCEPTS = {
    **_STATEMENT_CONCEPTS,
    'cash_flow': (*_STATEMENT_CONCEPTS['cash_flow'][:3], _STATEMENT_CONCEPTS['cash_flow'][3] | {
        'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations'}),
}
# Tried after the catalog above has declined a filing: the total operating
# revenue a regulated utility reports in place of `Revenues`.
_UTILITY_STATEMENT_CONCEPTS = {
    **_CORROBORATED_STATEMENT_CONCEPTS,
    'income_statement': (_STATEMENT_CONCEPTS['income_statement'][0] | {'RegulatedAndUnregulatedOperatingRevenue'},
                         *_STATEMENT_CONCEPTS['income_statement'][1:]),
}


def _inline_namespaces(element, fallback=None):
    namespaces = dict(fallback or {})
    for node in [*reversed(list(element.iterancestors())), element]:
        namespaces.update({key[6:]: value for key, value in node.attrib.items()
                           if key.startswith('xmlns:')})
    return namespaces


def _hidden(element):
    for node in [element, *element.iterancestors()]:
        style = re.sub(r'\s+', '', node.get('style', '').lower())
        if (local(node) == 'hidden' or 'hidden' in node.attrib
                or node.get('aria-hidden', '').lower() == 'true'
                or re.search(r'(?:^|;)(?:display:none|visibility:hidden)(?:!important)?(?:;|$)', style)):
            return True
    return False


def _statement_heading(value):
    # Exact headings exclude prose, note summaries, selected-data and TOC rows.
    value = re.sub(r'([a-z])([A-Z])', r'\1 \2', value)
    value = re.sub(r'\([^)]*\b(?:unaudited|audited)\b[^)]*\)', '', value, flags=re.I)
    value = re.sub(r'[^a-z0-9]+', ' ', value.lower()).strip()
    value = re.sub(r'^(?:(?:condensed|consolidated|combined|unaudited|audited)\s+)+', '', value)
    if re.fullmatch(r'(?:statements? of (?:income|operations|earnings)(?: (?:loss|and comprehensive (?:income|loss)))?'
                    r'(?: loss)?|statements? of comprehensive income(?: loss)?|(?:income|operations|earnings) statements?)', value):
        return 'income_statement'
    if re.fullmatch(r'(?:balance sheets?|statements? of financial (?:position|condition))', value):
        return 'balance_sheet'
    if re.fullmatch(r'(?:statements? of cash flows?|cash flows? statements?)', value):
        return 'cash_flow'
    return None


def _heading_qualifier(value):
    value = value.strip().strip('()').strip().lower()
    return not value or value in {'unaudited', 'audited'} or bool(re.fullmatch(
        r'(?:amounts |dollars )?in (?:thousands|millions|billions)(?:,? except [\w ,.-]+)?', value))


def _nearby_headings(node):
    values, sibling = [], node.getprevious()
    for _ in range(8):
        if sibling is None:
            break
        if local(sibling) == 'table' or sibling.xpath('.//table'):
            heading_only = (_statement_heading(text(sibling)) is not None
                            and not any(local(node) == 'nonfraction' for node in sibling.iter()))
            if not heading_only:
                break
        # Issuers often wrap separate company-name and statement-title blocks
        # in one preceding div; preserve each exact child heading as evidence.
        for child in sibling.iter():
            if not isinstance(child.tag, str) or len(text(child)) > 240:
                continue
            values.append(text(child))
            if child.xpath('.//br'):
                def fragments(element):
                    if element.text:
                        yield element.text
                    for part in element:
                        if local(part) == 'br':
                            yield '\n'
                        else:
                            yield from fragments(part)
                        if part.tail:
                            yield part.tail
                values.extend(' '.join(line.split()) for line in ''.join(fragments(child)).splitlines())
        sibling = sibling.getprevious()
    return values


_SELECTION_MEMO = None


def _memo(key, compute):
    """Reuse a pure reading of the document within one `select_statement_segments` call.

    Selection makes several attempts over the same unchanged document, and
    each asked every table for its headings again. `key` holds the element, so
    the element stays alive and its identity stays valid while the memo lives.
    """
    memo = _SELECTION_MEMO
    if memo is None:
        return compute()
    if key not in memo:
        memo[key] = compute()
    return memo[key]


def _statement_heading_values(table):
    return list(_memo(('heading_values', table), lambda: tuple(_statement_heading_values_uncached(table))))


def _statement_heading_values_uncached(table):
    values = _nearby_headings(table)
    # A heading may be a caption or an opening non-data row inside the table.
    values.extend(text(node) for node in table.xpath('./caption'))
    for row in table.xpath('./tr|./thead/tr|./tbody/tr|./tfoot/tr'):
        if any(local(node) == 'nonfraction' for node in row.iter()):
            break
        values.append(text(row))
    # Unwrap a layout container, but never borrow a heading across another table
    # or a preceding block inside the wrapper.
    node = table
    for _ in range(3):
        previous = node.getprevious()
        while previous is not None and _heading_qualifier(text(previous)) and not previous.xpath('.//table'):
            previous = previous.getprevious()
        if previous is not None:
            break
        node = node.getparent()
        if node is None or local(node) in {'body', 'html', 'td', 'th', 'table'}:
            break
        values.extend(_nearby_headings(node))
    return values


def _statement_headings(table, *, continued=False, consolidated=False):
    values = _statement_heading_values(table)
    if continued:
        stripped = [re.sub(r'[\s:–—-]*(?:\(\s*)?continued(?:\s*\))?\s*$', '', value, flags=re.I)
                    for value in values]
        values = [value for value, original in zip(stripped, values) if value != original]
    if consolidated:
        values = [value for value in values if re.search(r'\bconsolidated\b', value, re.I)]
    return {_statement_heading(value) for value in values} - {None}


def select_statements(tree, *, concept_groups=None):
    """Require a supported full heading and several independent GAAP concepts.

    Ambiguous, nested, hidden, untagged, and unsupported statement layouts fail
    closed. Recognition does not establish that every statement row was parsed.
    """
    concept_groups = _STATEMENT_CONCEPTS if concept_groups is None else concept_groups
    candidates = {kind: [] for kind in concept_groups}
    continued = set()
    contexts = {context.get('id'): context for context in tree.iter() if local(context) == 'context'}
    dimensional_contexts = {key for key, context in contexts.items()
                            if any(local(member) in {'explicitmember', 'typedmember'} for member in context.iter())}
    entities = {key: {(node.get('scheme'), text(node)) for node in context.iter()
                      if local(node) == 'identifier'} for key, context in contexts.items()}
    candidate_entities = {}
    for table in tree.xpath('//table'):
        if _hidden(table) or table.xpath('.//table'):
            continue
        kinds = _statement_headings(table)
        consolidated = _statement_headings(table, consolidated=True)
        continued.update(_statement_headings(table, continued=True))
        concepts, table_entities = set(), set()
        for element in table.iter():
            if (local(element) != 'nonfraction' or _hidden(element)
                    or element.get('contextref') in dimensional_contexts):
                continue
            name = element.get('name', '')
            prefix, sep, concept = name.partition(':')
            namespace = _inline_namespaces(element).get(prefix, '')
            if sep and re.fullmatch(r'https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^/]+', namespace):
                concepts.add(concept)
                table_entities.update(entities.get(element.get('contextref'), ()))
        candidate_entities[table] = table_entities
        for kind in kinds:
            if all(concepts.intersection(group) for group in concept_groups[kind]):
                candidates[kind].append((table, kind in consolidated))
    result = {}
    for kind, matches in candidates.items():
        if any(explicit for _, explicit in matches):
            matches = [(table, explicit) for table, explicit in matches if explicit]
        if kind in continued:
            raise TableExtractionError(f'{kind}: split statement continuation is unsupported')
        if len(matches) != 1:
            raise TableExtractionError(f'{kind}: expected one statement, found {len(matches)}')
        result[kind] = matches[0][0]
    if len({id(table) for table in result.values()}) != len(result):
        raise TableExtractionError('Different statements must resolve to distinct tables')
    selected_entities = set().union(*(candidate_entities[table] for table in result.values()))
    if len(selected_entities) > 1:
        raise TableExtractionError('Primary statements mix reporting entities')
    return result


def _period_caption(value):
    return bool(re.search(
        r'\b(?:19\d\d|20\d\d|january|february|march|april|may|june|july|august|'
        r'september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec|'
        r'years?|quarters?|months?|weeks?|ended|ending|as of)\b', value, re.I))


def _column_header_cells(rows, first_data_row, column, *, advanced=True):
    header_rows = rows[:first_data_row]
    width = max((c['column'] + c['colspan'] for row in header_rows for c in row), default=0)
    period_seen = False
    result = []
    for row in header_rows:
        nonempty = [c for c in row if c['display_text']]
        for cell in nonempty:
            value = cell['display_text']
            # A full-width row group (Assets, Revenue, Operating activities,
            # etc.) below the reporting dates labels rows, not every column.
            # Retain explicit dates, units, audit qualifiers and statement titles.
            row_group = (advanced and period_seen and len(nonempty) == 1
                         and cell['column'] == 0 and cell['colspan'] == width
                         and not _period_caption(value) and not _heading_qualifier(value)
                         and not re.search(r'\b(?:unaudited|audited|in (?:thousands|millions|billions))\b', value, re.I)
                         and _statement_heading(value) is None)
            if not row_group and cell['column'] <= column < cell['column'] + cell['colspan']:
                result.append(cell)
        period_seen = period_seen or any(_period_caption(c['display_text']) for c in nonempty)
    return result


def column_headers(rows, first_data_row, column, *, advanced=True):
    return [c['display_text'] for c in _column_header_cells(rows, first_data_row, column, advanced=advanced)]


def _presentation_column_groups(rows, first_data, columns, periods):
    """Merge alternate anchors only when the same printed header spans them."""
    groups = []
    for column in columns:
        headers = _column_header_cells(rows, first_data, column)
        signature = tuple(c['cell_id'] for c in headers)
        previous = groups[-1] if groups else None
        if (previous and signature and periods.get(column) is not None
                and signature == previous['signature'] and periods[column] == previous['period']
                and any(c['column'] > 0 and _period_caption(c['display_text']) for c in headers)
                and not any(sum(c['column'] in {*previous['columns'], column}
                                and c['classification'] in {'resolved', 'unresolved'} for c in row) > 1
                            for row in rows[first_data:])):
            previous['columns'].append(column)
        else:
            groups.append({'columns': [column], 'signature': signature, 'period': periods.get(column)})
    return [group['columns'] for group in groups]


def _presentation_value(row, columns):
    values = [c for c in row if c['column'] in columns and c['classification'] in {'resolved', 'unresolved'}]
    if not values:
        return ''
    value, = values
    # Currency and sign cells belong to this value; an adjacent value or row
    # label never does, even when the physical grid has no spacer columns.
    # Ignore token-internal spacing for recognition, retaining the source text.
    return ' '.join(c['display_text'] for c in row if c is value or (
        c['classification'] == 'formatting' and c['display_text']
        and ((c['column'] == value['column'] - 1 and re.fullmatch(r'[$€£¥(+−–—-]+', re.sub(r'\s+', '', c['display_text'])))
             or (c['column'] == value['column'] + value['colspan']
                 and re.fullmatch(r'[)%]+', re.sub(r'\s+', '', c['display_text']))))))


def header_period(headers, *, context_period=None, advanced=False, balance_dates=None, fiscal_year_offset=None):
    # Adjacent HTML spans often join "Ended" directly to the month name.
    s = re.sub(r'([a-z])([A-Z])', r'\1 \2', ' '.join(headers)).lower()
    if advanced:
        s = _period_text(s)
        if re.search(r'\b[1-4]\s+(?:and|or)\s+[1-4]\s+(?:fiscal\s+)?quarters?\b', s):
            return None
        # Ordinal quarters are one reporting quarter; cardinal quarters are
        # cumulative. Keep this grammar separate from the frozen V1 parser.
        s = re.sub(r'\b(?:first|second|third|fourth)\s+(?:fiscal\s+)?quarter\b', '3 months', s)
        s = re.sub(r'\b([1-4])\s+(?:fiscal\s+)?quarters?\b',
                   lambda match: str(int(match[1]) * 3) + ' months', s)
        s = re.sub(r'\bfiscal\s+quarter\b', '3 months', s)
    s = re.sub(r'\b(three|six|nine|twelve|3|6|9|12)\s+and\s+'
               r'((?:three|six|nine|twelve|3|6|9|12)\s+months?)', r'\1 months and \2', s)
    years = set(re.findall(r'\b((?:19|20)\d\d)\b', s))
    months = {name.lower(): month for month in range(1, 13)
              for name in (calendar.month_name[month], calendar.month_abbr[month])}
    months['sept'] = 9
    dates = {(months[name.rstrip('.')], int(day)) for name, day in re.findall(
        r'\b(' + '|'.join(sorted(months, key=len, reverse=True)) + r')\.?\s+(\d{1,2})\b', s)}
    if advanced:
        dates.update((int(month), int(day)) for month, day, _ in re.findall(
            r'\b(\d{1,2})/(\d{1,2})/((?:19|20)\d\d)\b', s))
    if len(years) != 1:
        return None
    if not dates and context_period is not None:
        # Some annual statements print only column years. Use exact dates from
        # corroborated source facts, never an assumed December 31 year-end.
        year = int(next(iter(years)))
        if re.search(r'\b(?:months?|weeks?|quarters?)\b', s):
            return None
        try:
            if set(context_period) == {'instant'}:
                end = date.fromisoformat(context_period['instant'])
                return dict(context_period) if end.year == year else None
            if set(context_period) == {'startDate', 'endDate'}:
                start = date.fromisoformat(context_period['startDate'])
                end = date.fromisoformat(context_period['endDate'])
                calendar_year = start == date(year, 1, 1) and end == date(year, 12, 31)
                fiscal_year = (end - start).days + 1 in {364, 371}
                fiscal_calendar_year = (advanced and end.day == calendar.monthrange(end.year, end.month)[1]
                    and start == date(end.year if end.month == 12 else end.year - 1, end.month % 12 + 1, 1))
                if end.year == year and (calendar_year or fiscal_year or fiscal_calendar_year):
                    return dict(context_period)
                # A fiscal year can be named for the calendar year in which it
                # begins ("2025" for the year ended January 31, 2026). Accept
                # that label only when the filing's own DEI facts declare the
                # same naming for the document period.
                if (advanced and fiscal_year_offset == 1 and end.year - 1 == year
                        and (fiscal_year or fiscal_calendar_year)):
                    return dict(context_period)
        except (TypeError, ValueError):
            return None
        return None
    if len(dates) != 1:
        return None
    month, day = dates.pop()
    try:
        end = date(int(years.pop()), month, day)
    except ValueError:
        return None
    if advanced and re.search(r'\b\d+\s+(?:and|or)\s+\d+\s+weeks?\b', s):
        return None
    weeks = re.findall(r'\b([1-9]|[1-4][0-9]|5[0-3])\s+weeks?\b' if advanced else
                       r'\b(13|26|39|52|53)\s+weeks?\b', s)
    if weeks:
        if len(set(weeks)) != 1:
            return None
        return {'startDate': (end - timedelta(weeks=int(weeks[0])) + timedelta(days=1)).isoformat(),
                'endDate': end.isoformat()}
    month_counts = {'three': 3, 'six': 6, 'nine': 9, 'twelve': 12,
                    '3': 3, '6': 6, '9': 9, '12': 12}
    counts = {month_counts[value] for value in re.findall(
        r'\b(three|six|nine|twelve|3|6|9|12)\s+months?\b', s)}
    if re.search(r'\b(?:fiscal\s+)?years?\s+ended\b', s):
        counts.add(12)
    if re.search(r'\bquarters?\s+ended\b', s):
        counts.add(3)
    if not counts:
        # Unknown duration wording must not become an apparently valid instant.
        if re.search(r'\b(?:ended|months?|weeks?|years?|quarters?)\b', s):
            return None
        return {'instant': end.isoformat()}
    if len(counts) != 1:
        return None
    count = counts.pop()
    # Fiscal 4-4-5 calendars can be labelled "months" or "years" on the
    # statement. Accept their exact dates only from corroborated source
    # contexts, a matching displayed end date, and the permitted week length.
    if context_period is not None and set(context_period) == {'startDate', 'endDate'}:
        try:
            start = date.fromisoformat(context_period['startDate'])
            context_end = date.fromisoformat(context_period['endDate'])
        except (ValueError, TypeError):
            return None
        allowed_weeks = {3: {13, 14}, 6: {26, 27}, 9: {39, 40}, 12: {52, 53}}[count]
        if context_end == end and (end - start).days + 1 in {7 * weeks for weeks in allowed_weeks}:
            return dict(context_period)
        # A calendar fiscal year whose quarters close on a fixed weekday (the
        # Friday or Sunday nearest the quarter end) has periods a few days
        # longer or shorter than whole months and not a whole number of weeks.
        # The printed end date must still be the tagged one, and the period
        # must begin on the day after a balance date this filing reports;
        # a context that merely starts a day early has no such balance.
        if (advanced and balance_dates is not None and context_end == end
                and (start - timedelta(days=1)).isoformat() in balance_dates):
            whole = start.year * 12 + start.month - 1 + count
            try:
                expected_end = date(whole // 12, whole % 12 + 1, start.day) - timedelta(days=1)
            except ValueError:
                expected_end = None
            if expected_end is not None and abs((end - expected_end).days) <= 7:
                return dict(context_period)
    # Without those contexts a non-calendar start date must remain unresolved.
    if day != calendar.monthrange(end.year, month)[1]:
        return None
    start_month = end.year * 12 + month - count
    start = date(start_month // 12, start_month % 12 + 1, 1)
    return {'startDate': start.isoformat(), 'endDate': end.isoformat()}


def _period_text(value):
    """Normalize duration words for matching, without changing source text."""
    value = re.sub(r'[\u200b\ufeff]', '', value.lower())
    value = re.sub(r'([a-z])(?=\d)', r'\1 ', value)
    # A cell that stacks "September 30" above "2026" joins them as "302026".
    value = re.sub(r'\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2})((?:19|20)\d\d)\b',
                   r'\1, \2', value)
    numbers = {1: 'one', 2: 'two', 3: 'three', 4: 'four', 6: 'six', 9: 'nine',
               12: 'twelve', 13: 'thirteen', 14: 'fourteen', 16: 'sixteen',
               24: 'twenty-four', 26: 'twenty-six', 27: 'twenty-seven',
               28: 'twenty-eight', 36: 'thirty-six', 39: 'thirty-nine',
               40: 'forty', 52: 'fifty-two', 53: 'fifty-three'}
    for number, word in sorted(numbers.items(), key=lambda pair: -len(pair[1])):
        value = re.sub(r'\b' + word.replace('-', r'[-\s]') + r'\b', str(number), value)
    return re.sub(r'(?<=\d)[-\s]+(?=weeks?\b)', ' ', value)


def _quarter_count(value):
    value = _period_text(value)
    # Distinguish "Second Quarter" from "Two Quarters" and reject mixed
    # quarter captions instead of silently taking the last duration.
    if re.search(r'\b[1-4]\s+(?:and|or)\s+[1-4]\s+(?:fiscal\s+)?quarters?\b', value):
        return None
    counts = {int(count) for count in re.findall(r'\b([1-4])\s+(?:fiscal\s+)?quarters?\b', value)}
    residual = re.sub(r'\b[1-4]\s+(?:fiscal\s+)?quarters?\b', '', value)
    if re.search(r'\bquarter\b', residual):
        counts.add(1)
    return next(iter(counts)) if len(counts) == 1 else None


def _full_header_dates(value):
    months = {name.lower(): month for month in range(1, 13)
              for name in (calendar.month_name[month], calendar.month_abbr[month])}
    months['sept'] = 9
    pattern = (r'\b(' + '|'.join(sorted(months, key=len, reverse=True)) +
               r')\.?\s+(\d{1,2})\s*,?\s+((?:19|20)\d\d)\b')
    result = []
    for match in re.finditer(pattern, _period_text(value)):
        try:
            result.append(date(int(match[3]), months[match[1]], int(match[2])))
        except ValueError:
            return []
    return result


def _nearby_period_captions(table):
    """Read short captions immediately above a table, never across data."""
    node, result = table, []
    for _ in range(4):
        sibling = node.getprevious()
        for _ in range(4):
            if sibling is None:
                break
            value = text(sibling)
            if sibling.xpath('.//table') or local(sibling) == 'table' or any(
                    local(child) == 'nonfraction' for child in sibling.iter()):
                return result
            normalized = _period_text(value)
            if re.fullmatch(r'(?:periods?|(?:3|6|9|12) months?) ended [a-z]+ \d{1,2},?', normalized):
                result.append(sibling)
            elif value and not re.fullmatch(r'\([^)]*(?:millions|thousands|unaudited)[^)]*\)', value, re.I):
                return result
            sibling = sibling.getprevious()
        if sibling is not None:
            return result
        node = node.getparent()
        if node is None or local(node) in {'body', 'html'}:
            break
    return result


def _period_source(node):
    return {'source_xpath': source_xpath(node.getroottree(), node), 'display_text': text(node)}


def _with_split_caption(rows, first_data, revised, sources):
    """Apply a duration caption that physically spans only the first value column.

    The centered caption can be printed in two cells ("Three months ended"
    above "March 31,") over bare-year columns, or as a duration alone over
    fully dated columns. The header must hold exactly one duration and at most
    one month-day cell, and otherwise only years, complete dates, units or row
    labels. This supplies wording only: the column is accepted when its tagged
    source facts carry exactly the resulting period.
    """
    printed = [value.strip() for value in revised if value.strip()]
    if not printed or any(re.search(r'\b(?:ended|ending|months?|weeks?|quarters?)\b', _period_text(value))
                          for value in printed):
        return revised, sources
    month = (r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?'
             r'|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)')
    header_cells = [cell for row in rows[:first_data] for cell in row if cell['display_text']]
    normalized = {id(cell): _period_text(cell['display_text']).strip() for cell in header_cells}
    durations = [cell for cell in header_cells if re.fullmatch(r'(?:3|6|9|12) months ended', normalized[id(cell)])]
    month_days = [cell for cell in header_cells if re.fullmatch(month + r'\.? \d{1,2},?', normalized[id(cell)])]

    def complete_date(value):
        # One printed date and no duration wording.
        return set(header_period([value], advanced=True) or ()) == {'instant'}

    def plain(cell):
        value = cell['display_text'].strip()
        return (cell['column'] == 0 or bool(_heading_qualifier(value))
                or bool(re.fullmatch(r'(?:19|20)\d\d', value)) or complete_date(value))

    bare_years = all(re.fullmatch(r'(?:19|20)\d\d', value) for value in printed)
    dated = len(printed) == 1 and complete_date(printed[0])
    if (len(durations) == 1 and len(month_days) == (1 if bare_years else 0) and (bare_years or dated)
            and all(cell in durations or cell in month_days or plain(cell) for cell in header_cells)):
        revised, sources = list(revised), list(sources)
        for cell in reversed(durations + month_days):
            revised.insert(0, cell['display_text'])
            sources.append(_period_source(cell['_element']))
    return revised, sources


def _v2_period_headers(rows, first_data, column, headers):
    """Disambiguate shared captions with physical column years and labels."""
    values, sources = list(headers), []
    table = next(cell for row in rows for cell in row)['_element'].xpath('ancestor::table[1]')[0]
    outside = _nearby_period_captions(table)
    if outside:
        values = [*(text(node) for node in outside), *values]
        sources.extend(_period_source(node) for node in outside)
    years = {value.strip() for value in values if re.fullmatch(r'(?:19|20)\d\d', value.strip())}
    if len(years) != 1:
        return _with_split_caption(rows, first_data, values, sources)
    year = int(next(iter(years)))
    # A caption can name both comparative dates and both reporting durations.
    # The year and duration in the narrower column headers must disambiguate it.
    revised = []
    for value in values:
        dates = _full_header_dates(value)
        if len(set(dates)) <= 1:
            revised.append(value)
            continue
        selected = {item for item in dates if item.year == year}
        if len(selected) != 1:
            return values, sources
        duration_text = re.sub(r'\b(3|6|9|12) and ((?:3|6|9|12) months?)\b',
                               r'\1 months and \2', _period_text(value))
        durations = set(re.findall(r'\b(?:3|6|9|12) months?\b', duration_text))
        child_durations = {duration for other in values if other != value
                           for duration in re.findall(r'\b(?:3|6|9|12) months?\b', _period_text(other))}
        if len(durations) > 1 and (len(child_durations) != 1 or not child_durations <= durations):
            return values, sources
        if len(durations) == 1 and child_durations and child_durations != durations:
            return values, sources
        end = next(iter(selected))
        revised.append((next(iter(durations)) + ' ended ' if len(durations) == 1 else '')
                       + end.strftime('%B %d, %Y'))
        sources.extend(_period_source(cell['_element']) for row in rows[:first_data] for cell in row
                       if cell['display_text'] == value)
    # A single fiscal caption can be visually centered outside a physical
    # colspan. Only a bare-year column and a unique caption qualify; the exact
    # dates still require the fiscal disclosure and corroborated source facts.
    if not any('quarter' in _period_text(value) for value in revised):
        captions = {cell['display_text']: cell['_element'] for row in rows[:first_data] for cell in row
                    if re.fullmatch(r'first (?:two|three|four|[234]) quarters of fiscal', cell['display_text'], re.I)}
        if len(captions) == 1 and all(not _period_text(value).strip() or re.fullmatch(r'(?:19|20)\d\d', value.strip()) for value in revised):
            caption, node = next(iter(captions.items()))
            revised.insert(0, caption)
            sources.append(_period_source(node))
    # The same centering occurs with an ordinary duration caption: the cell
    # "Three Months Ended June 30," physically spans the first year column
    # only. One such caption, in a header whose other cells are bare years,
    # units or row labels, applies to a bare-year column. It supplies the
    # printed wording only; the column is accepted when the tagged source
    # facts carry exactly the resulting period.
    printed = [value.strip() for value in revised if value.strip()]
    if printed and all(re.fullmatch(r'(?:19|20)\d\d', value) for value in printed):
        header_cells = [cell for row in rows[:first_data] for cell in row if cell['display_text']]
        captions = [cell for cell in header_cells if re.fullmatch(
            r'(?:3|6|9|12) months ended [a-z]+\.? \d{1,2},?', _period_text(cell['display_text']).strip())]
        if len(captions) == 1 and all(
                cell is captions[0] or cell['column'] == 0 or _heading_qualifier(cell['display_text'])
                or re.fullmatch(r'(?:19|20)\d\d', cell['display_text'].strip()) for cell in header_cells):
            revised.insert(0, captions[0]['display_text'])
            sources.append(_period_source(captions[0]['_element']))
            return revised, sources
    return _with_split_caption(rows, first_data, revised, sources)


def _v2_calendar_period(headers, period, table):
    """Require explicit calendar prose for nonstandard or date-free quarters."""
    values = ' '.join(headers)
    count = _quarter_count(values)
    if count is None:
        return None, []
    try:
        end = date.fromisoformat(period.get('endDate', period.get('instant')))
        days = ((end - date.fromisoformat(period['startDate'])).days + 1
                if 'startDate' in period else None)
    except (ValueError, TypeError):
        return None, []
    normalized_headers = _period_text(values)
    displayed_dates = set(_full_header_dates(values))
    displayed_years = set(re.findall(r'\b(?:19|20)\d\d\b', normalized_headers))
    if len(displayed_years) != 1 or len(displayed_dates) > 1:
        return None, []
    # Prose supplements an incomplete caption; it cannot override an explicit
    # date or duration already printed on the column.
    if displayed_dates and displayed_dates != {end}:
        return None, []
    dated_header = re.search(r'\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s*\d|\b\d{1,2}/', normalized_headers)
    if dated_header:
        date_only = re.sub(r'\b(?:ended|months?|weeks?|quarters?|years?)\b', '', normalized_headers)
        if header_period([date_only], advanced=True) != {'instant': end.isoformat()}:
            return None, []
        displayed_dates.add(end)
    explicit_weeks = {int(number) for number in re.findall(r'\b(\d+) weeks?\b', normalized_headers)}
    if explicit_weeks and (days is None or explicit_weeks != {days / 7}):
        return None, []
    if re.search(r'\bmonths?\b', normalized_headers) and header_period(
            headers, context_period=period, advanced=True) != period:
        return None, []
    root = table.getroottree()
    mapped_dates, mapped_sources, week_sources, declared_weeks = set(), [], [], set()
    # Keep candidates to short original paragraph blocks. An unrelated date
    # elsewhere in the report cannot supply an omitted header date.
    for node in root.xpath('//p|//div'):
        if _hidden(node) or node.xpath('./p|./div|.//table'):
            continue
        value = text(node)
        if len(value) > 2400 or 'quarter' not in value.lower():
            continue
        normalized = _period_text(value)
        # Date-free labels require an explicit paired fiscal-year/quarter-date
        # definition. The ordinal must agree with the displayed header.
        matches = re.finditer(r'\b(first|second|third|fourth) quarter of fiscal ((?:19|20)\d\d) and ((?:19|20)\d\d) ended on ([^.]+)\.', normalized)
        fiscal_label = re.fullmatch(
            r'(?:at the end of )?(?:(?:first|second|third|fourth) quarter|first [234] quarters) of fiscal (?:19|20)\d\d',
            ' '.join(_period_text(values).split()))
        for match in matches if fiscal_label else ():
            dates = _full_header_dates(match[4])
            years = {int(year) for year in re.findall(r'\b(?:19|20)\d\d\b', values)}
            ordinal = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4}[match[1]]
            header = _period_text(values)
            correct_quarter = (bool(re.search(r'\b' + match[1] + r' quarter\b', header))
                               if count == 1 else count == ordinal and 'first ' in header)
            if len(dates) == 2 and len(years) == 1 and correct_quarter:
                mapped = dict(zip((int(match[2]), int(match[3])), dates))
                mapped_end = mapped.get(next(iter(years)))
                if mapped_end is not None:
                    mapped_dates.add(mapped_end)
                    mapped_sources.append(_period_source(node))
        # Explicit end dates plus a narrative week length can substantiate a
        # 12-week quarter or a 28/36-week cumulative period. Require the same
        # end date in the calendar paragraph and a plausible quarter count.
        if days is None or days % 7:
            continue
        direct = header_period(headers, advanced=True)
        if end not in displayed_dates and not (direct and direct.get('endDate') == end):
            continue
        weeks = days // 7
        if not count * 12 <= weeks <= count * 13 + 3:
            continue
        for sentence in re.split(r'\.\s+|\.$', normalized):
            if end not in _full_header_dates(sentence):
                continue
            stated = {int(number) for number in re.findall(r'\b(\d{1,2}) weeks?\b', sentence)}
            paired = re.findall(r'\b(\d{1,2}) and (\d{1,2}) weeks?\b', sentence)
            stated.update(int(number) for pair in paired for number in pair)
            defines_quarters = re.search(r'\bquarters? referenced herein represent the\b', sentence)
            defines_statement = ('consolidated financial statements' in sentence
                                 and 'includes the results of operations' in sentence
                                 and 'periods then ended' in sentence)
            relevant = {number for number in stated if count * 12 <= number <= count * 13 + 3}
            if relevant and (defines_quarters or defines_statement):
                declared_weeks.update(relevant)
                week_sources.append(_period_source(node))
    if mapped_dates == {end} and (days is None or days in {7 * count * 13, 7 * (count * 13 + 1)}):
        return dict(period), mapped_sources
    if week_sources and declared_weeks == {days // 7}:
        return dict(period), week_sources
    return None, []


def verify_inline(element, evidence, ns):
    if expanded(element.get('name', ''), _inline_namespaces(element, ns)) != evidence['concept']:
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
    if fmt not in ('num-dot-decimal', 'numdotdecimal', 'zerodash', 'num-dash', 'fixed-zero', 'numwordsen', ''):
        raise TableExtractionError(f'Unsupported inline numeric format: {fmt}')
    lexical = re.sub(r'[\s,]', '', text(element))
    if fmt == 'numwordsen':
        from .fact_extraction import _small_english_number
        if expanded(element.get('format'), _inline_namespaces(element, ns)) != '{http://www.sec.gov/inlineXBRL/transformation/2015-08-31}numwordsen':
            raise TableExtractionError('Unsupported numeric-word transformation namespace')
        lexical = _small_english_number(text(element))
        if lexical is None:
            raise TableExtractionError('Unsupported numeric-word phrase')
    if fmt == 'fixed-zero':
        lexical = '0'
    if fmt in ('zerodash', 'num-dash') and lexical in ('-', '–', '—'):
        lexical = '0'
    try:
        value = Decimal(lexical) * Decimal(10) ** int(element.get('scale', '0'))
        if element.get('sign') == '-':
            value = -value
        expected_value = Decimal(evidence['raw_value'])
    except (InvalidOperation, ValueError) as exc:
        raise TableExtractionError('Unsupported numeric value') from exc
    if value != expected_value:
        raise TableExtractionError('Inline/XML value mismatch')


def _fiscal_calendar_evidence(facts):
    """Filing-wide evidence for fiscal calendars that follow neither months nor whole weeks.

    Returns the dates of the undimensioned balances the filing reports, and how
    its DEI facts name the fiscal year: 0 when "fiscal 2025" ends in 2025, 1 when
    it ends in 2026, None when the filing does not say.
    """
    balance_dates, focus, ends = set(), set(), set()
    for record in facts.values():
        if record.get('dimensions'):
            continue
        period = record.get('period') or {}
        if set(period) == {'instant'}:
            balance_dates.add(period['instant'])
        concept = record.get('concept') or ''
        if re.match(r'\{https?://xbrl\.sec\.gov/dei/', concept):
            if concept.endswith('}DocumentFiscalYearFocus'):
                focus.add((record.get('raw_value') or '').strip())
            elif concept.endswith('}DocumentPeriodEndDate'):
                ends.add((record.get('raw_value') or '').strip())
    offset = None
    if len(focus) == 1 and len(ends) == 1:
        try:
            offset = date.fromisoformat(next(iter(ends))).year - int(next(iter(focus)))
        except ValueError:
            offset = None
    return balance_dates, offset if offset in (0, 1) else None


def _tagging_defect_candidate(references, balance_dates, headers, *, advanced, fiscal_year_offset):
    """V8 only: the printed period of a column whose tags contradict it.

    Two company tagging errors are recognized, never anything looser:
    - one or two tags in the column carry another period than all the others
      (for example a prior-year dash tagged with the current quarter); the
      shared period must have at least two concepts and be confirmed by the
      printed header, and the outliers are then flagged against it;
    - every duration tag starts on a balance-sheet date, the last day of the
      prior period, and the period starting one day later is confirmed by the
      printed header and fiscal calendar; every tag is then flagged.
    Returns (printed period, method, tagged periods) or None.
    """
    counts = Counter(tuple(sorted(reference['period'].items())) for reference in references)
    if len(counts) > 1:
        (shared, count), (_, runner_up) = counts.most_common(2)
        outliers = len(references) - count
        majority = [reference for reference in references if tuple(sorted(reference['period'].items())) == shared]
        if (runner_up == count or outliers > 2 or outliers * 4 >= count
                or len({reference['concept'] for reference in majority}) < 2):
            return None
        printed = dict(shared)
        if header_period(headers, context_period=printed, advanced=advanced, balance_dates=balance_dates,
                         fiscal_year_offset=fiscal_year_offset) != printed:
            return None
        return printed, 'majority_column_contexts_tagging_defects', sorted(
            {json.dumps(dict(period), sort_keys=True) for period in counts if period != shared})
    tagged = dict(next(iter(counts)))
    if set(tagged) != {'startDate', 'endDate'} or not balance_dates or tagged['startDate'] not in balance_dates:
        return None
    printed = {'startDate': (date.fromisoformat(tagged['startDate']) + timedelta(days=1)).isoformat(),
               'endDate': tagged['endDate']}
    if header_period(headers, context_period=printed, advanced=advanced, balance_dates=balance_dates,
                     fiscal_year_offset=fiscal_year_offset) != printed:
        return None
    return printed, 'fiscal_calendar_start_tagging_defects', [json.dumps(tagged, sort_keys=True)]


_MONTH_NUMBERS = {name.lower(): month for month in range(1, 13)
                  for name in (calendar.month_name[month], calendar.month_abbr[month])}
_MONTH_NUMBERS['sept'] = 9
_MONTH_NAMES = '|'.join(sorted(_MONTH_NUMBERS, key=len, reverse=True))


def _printed_period_tokens(headers):
    """The years, dates and duration wording a column header prints.

    Only what is printed is collected; nothing is inferred. Durations are in
    months, except explicit week counts. Returns None for a header that names
    two different durations or week counts, which no single period can satisfy.
    """
    value = _period_text(re.sub(r'([a-z])([A-Z])', r'\1 \2', ' '.join(headers)))
    # "March 30,2025": a date printed without a space after its comma.
    value = re.sub(r'\b(\d{1,2}),(?=(?:19|20)\d\d\b)', r'\1, ', value)
    # "2024As Adjusted": a year printed without a space before the next word.
    value = re.sub(r'\b((?:19|20)\d\d)(?=[a-z])', r'\1 ', value)
    value = re.sub(r'\b(years?|months?|quarters?|weeks?|periods?)-(ended|ending)\b', r'\1 \2', value)
    value = re.sub(r'\b(\d+)[-\s]+(months?|weeks?|quarters?)\b', r'\1 \2', value)
    if re.search(r'\b\d{1,2}\s*(?:and|or|&|/)\s*\d{1,2} (?:months?|weeks?|quarters?)\b', value):
        return None
    full_dates = set(_full_header_dates(value))
    for month, day, year in re.findall(r'\b(\d{1,2})/(\d{1,2})/((?:19|20)\d\d)\b', value):
        try:
            full_dates.add(date(int(year), int(month), int(day)))
        except ValueError:
            return None
    month_days = {(_MONTH_NUMBERS[name], int(day)) for name, day in re.findall(
        r'\b(' + _MONTH_NAMES + r')\.?\s+(\d{1,2})\b', value)}
    month_only = {_MONTH_NUMBERS[name] for name in re.findall(
        r'\b(' + _MONTH_NAMES + r')\b\.?(?!\s+\d{1,2}\b)', value)}
    year_to_date = bool(re.search(r'\byear[-\s]to[-\s]date\b|\bytd\b', value))
    wording = re.sub(r'\byear[-\s]to[-\s]date\b|\bytd\b', ' ', value)
    months = {int(count) for count in re.findall(r'\b(\d{1,2}) months?\b', wording)}
    months.update(int(count) * 3 for count in re.findall(r'\b([1-4]) quarters?\b', wording))
    if re.search(r'\bquarters?\b', re.sub(r'\b[1-4] quarters?\b', ' ', wording)):
        months.add(3)
    if re.search(r'\b(?:first|1st) half\b|\bhalf[-\s]year\b', wording):
        months.add(6)
    if re.search(r'\b(?:fiscal )?years? (?:ended|ending)\b|\bfiscal years?\b', wording):
        months.add(12)
    weeks = {int(count) for count in re.findall(r'\b(\d{1,2}) weeks?\b', wording)}
    if len(months) > 1 or len(weeks) > 1:
        return None
    return {'years': {int(year) for year in re.findall(r'\b((?:19|20)\d\d)\b', value)},
            'full_dates': full_dates, 'month_days': month_days, 'month_only': month_only,
            'months': next(iter(months), None), 'weeks': next(iter(weeks), None), 'year_to_date': year_to_date}


def _labelled_years(end, fiscal_year_offset):
    """The years a header may print for a period that ends on `end`.

    The end year; the year before it when the filing's own DEI facts name the
    fiscal year that way; and the year before it for a period that ends in
    the first week of January, which lies in that earlier year but for those
    days ("fiscal 2022" of a 52-week year that ended on January 1, 2023).
    """
    years = {end.year, end.year - 1} if fiscal_year_offset == 1 else {end.year}
    if end.month == 1 and end.day <= 7:
        years.add(end.year - 1)
    return years


def _fiscal_year_boundaries(facts):
    """The days on which this filing itself shows a fiscal period to close.

    A day is one when an undimensioned fact covers 52 or 53 weeks or a whole
    year ending on it, or covers a period that begins on the day after it.
    """
    days = set()
    for record in facts.values():
        period = record.get('period') or {}
        if record.get('dimensions') or set(period) != {'startDate', 'endDate'}:
            continue
        try:
            start, end = date.fromisoformat(period['startDate']), date.fromisoformat(period['endDate'])
        except (TypeError, ValueError):
            continue
        days.add((start - timedelta(days=1)).isoformat())
        if 363 <= (end - start).days + 1 <= 372:
            days.add(end.isoformat())
    return days


def _printed_tokens_agree(tokens, period, fiscal_year_offset):
    """Whether everything the header prints is true of the tagged period.

    The year must be printed and be one `_labelled_years` allows: the period's
    end year, or the fiscal year the filing names for it. A printed date must be the end
    date, a printed month the end month, and a printed duration the period's
    length: within ten days for months, which admits 13- and 14-week quarters,
    and within four days for weeks. A fiscal period that starts on a fixed
    calendar day and ends on a Saturday is a few days longer or shorter than
    the whole weeks its header names; a 53-week year is never "52 weeks".
    """
    if tokens is None or not tokens['years']:
        return False
    end = date.fromisoformat(period.get('endDate', period.get('instant')))
    labelled = _labelled_years(end, fiscal_year_offset)
    if tokens['full_dates']:
        if tokens['full_dates'] != {end} or not tokens['years'] <= labelled:
            return False
    elif len(tokens['years']) != 1 or not tokens['years'] <= labelled:
        return False
    if tokens['month_days'] and tokens['month_days'] != {(end.month, end.day)}:
        return False
    if tokens['month_only']:
        previous = end.month - 1 or 12
        if tokens['month_only'] != {end.month} and not (end.day <= 7 and tokens['month_only'] == {previous}):
            return False
    if 'startDate' not in period:
        # A balance column: the printed date itself must be the tagged date.
        return tokens['full_dates'] == {end}
    days = (end - date.fromisoformat(period['startDate'])).days + 1
    if tokens['months'] is not None and abs(days - tokens['months'] * 30.4375) > 10:
        return False
    if tokens['weeks'] is not None and abs(days - 7 * tokens['weeks']) > 4:
        return False
    return True


def _caption_lines(table):
    """The header lines printed between a statement's title and its table.

    Only lines `statement_headings` reads as header metadata are collected
    (periods, dates, units, a company name, page furniture); the search ends
    at the title or at any other text, and never crosses a table or a number.
    """
    from .statement_headings import _metadata, _numeric, _visible_text
    lines, node = [], table
    for _ in range(5):
        sibling, blocks = node.getprevious(), 0
        while sibling is not None:
            if _numeric(sibling) or local(sibling) == 'table' or sibling.xpath('.//table'):
                return lines
            value = _visible_text(sibling)
            if value:
                blocks += 1
                if blocks > 8 or not _metadata(value, (), True):
                    return lines
                lines.append(value)
            sibling = sibling.getprevious()
        node = node.getparent()
        if node is None or local(node) in {'body', 'html', 'td', 'th', 'table'}:
            break
    return lines


def _caption_agrees(own, caption, period, fiscal_year_offset):
    """Whether a column's own header and a caption above it together print `period`.

    The caption is a period line shared by all columns ("For the quarters
    ended March 31, 2025 and 2024"); the column prints which year it is. The
    column must print exactly one year, the period's end year, and nothing
    that differs from the period. The caption must print the period's end,
    as a full date or as a month and day beside that year, and for a
    duration its length, which must match as in `_printed_tokens_agree`.
    """
    if own is None or caption is None or len(own['years']) != 1:
        return False
    end = date.fromisoformat(period.get('endDate', period.get('instant')))
    labelled = _labelled_years(end, fiscal_year_offset)
    if (not own['years'] <= labelled or not own['full_dates'] <= {end}
            or not own['month_days'] <= {(end.month, end.day)} or own['month_only'] - {end.month}):
        return False
    ends = set(caption['full_dates'])
    if len(caption['month_days']) == 1:
        month, day = next(iter(caption['month_days']))
        for year in caption['years']:
            try:
                ends.add(date(year, month, day))
            except ValueError:
                return False
    if end not in ends:
        return False
    months = {value for value in (own['months'], caption['months']) if value is not None}
    weeks = {value for value in (own['weeks'], caption['weeks']) if value is not None}
    if 'startDate' not in period:
        return not months and not weeks
    if len(months) > 1 or len(weeks) > 1 or not (months or weeks):
        return False
    days = (end - date.fromisoformat(period['startDate'])).days + 1
    return (all(abs(days - value * 30.4375) <= 10 for value in months)
            and all(abs(days - 7 * value) <= 4 for value in weeks))


def _without_markers(cells, values):
    """Header texts without superscript footnote markers ("2026" with a raised "1" reads "20261")."""
    def pieces(node):
        if local(node) == 'sup':
            return
        if node.text:
            yield node.text
        for child in node:
            yield from pieces(child)
            if child.tail:
                yield child.tail

    plain = {cell['display_text']: ' '.join(''.join(pieces(cell['_element'])).split())
             for cell in cells if any(local(node) == 'sup' for node in cell['_element'].iter())}
    return [plain.get(value, value) for value in values] if plain else values


def _document_period(facts):
    """The fiscal year the filing's cover facts name and the last day of the period it reports on, or None.

    Both must be stated exactly once, without a dimension:
    `DocumentFiscalYearFocus` and `DocumentPeriodEndDate`.
    """
    stated = {'DocumentFiscalYearFocus': set(), 'DocumentPeriodEndDate': set()}
    for record in facts.values():
        match = re.fullmatch(r'\{https?://xbrl\.sec\.gov/dei/[0-9]{4}\}(DocumentFiscalYearFocus|DocumentPeriodEndDate)',
                             record.get('concept') or '')
        if match and not record.get('dimensions'):
            stated[match[1]].add((record.get('raw_value') or '').strip())
    if any(len(values) != 1 for values in stated.values()):
        return None
    focus, last_day = next(iter(stated['DocumentFiscalYearFocus'])), next(iter(stated['DocumentPeriodEndDate']))
    try:
        date.fromisoformat(last_day)
    except ValueError:
        return None
    return (int(focus), last_day) if re.fullmatch(r'(?:19|20)\d\d', focus) else None


def _corroborate_columns(deferred, periods, provenance, balance_dates, fiscal_year_offset, captions=(), boundaries=None,
                         document=None):
    """Accept the tagged period of columns whose printed header cannot be parsed alone.

    Every tagged number in such a column already carries one period, shared by
    at least two concepts, and the header prints nothing that contradicts it
    (`_printed_tokens_agree`). That is not enough by itself: one of these must
    also hold.
    - The header prints the period's length ("Six Months", "First Quarter",
      "39 Weeks") and it matches.
    - The header says "Year to Date" and the period starts on the day after a
      balance date this filing reports.
    - Another column of the same table has an established period of the same
      length ending one year earlier or later: the comparative column of a
      caption that physically spans only its neighbour.
    - A balance column prints the tagged date in full.
    - A duration column prints the period's last day in full and nothing
      else, and the period is a whole fiscal year (52 or 53 weeks, or twelve
      months) that begins on the day after a balance date this filing reports.
    - A balance column prints only the fiscal year the filing's cover facts
      name, and the tagged date is the last day of the period those facts
      report on (`document`, a callable giving `_document_period`).
    - A balance column prints only a fiscal year, the tagged date falls in
      the first week of the January after it, and the filing itself shows a
      fiscal period to close on that date (`boundaries`, a callable giving
      `_fiscal_year_boundaries`).
    - A caption shared by the columns prints the period's end and length and
      the column prints its year (`_caption_agrees`). A caption is a header
      cell naming two or more years, or one of `captions`, the lines printed
      between the statement's title and the table.
    Nothing is read from the header that it does not print, and a column that
    meets none of these stays unresolved.
    """
    pending = {}
    for column, (candidate, references, headers) in deferred.items():
        tokens = _printed_period_tokens(headers)
        if _printed_tokens_agree(tokens, candidate, fiscal_year_offset):
            pending[column] = (candidate, references, tokens)
    accepted = {}

    def accept(column, anchor, **evidence):
        candidate, references, _ = pending.pop(column, None) or deferred[column]
        accepted[column] = candidate
        periods[column] = candidate
        provenance[str(column)] = {
            'method': 'corroborated_printed_header', 'anchor': anchor, 'period': candidate,
            'context_refs': sorted({reference['context_ref'] for reference in references}),
            'xml_ids': sorted({reference['xml_id'] for reference in references}), **evidence}

    for column in sorted(pending):
        candidate, _, tokens = pending[column]
        if 'startDate' not in candidate:
            accept(column, 'printed_balance_date')
        elif tokens['months'] is not None or tokens['weeks'] is not None:
            accept(column, 'printed_duration')
        elif (tokens['year_to_date'] and balance_dates is not None
              and (date.fromisoformat(candidate['startDate']) - timedelta(days=1)).isoformat() in balance_dates):
            accept(column, 'year_to_date_from_reported_balance')

    for column in sorted(deferred):
        candidate, _, headers = deferred[column]
        if column in accepted or set(candidate) != {'instant'} or boundaries is None:
            continue
        tokens = _printed_period_tokens(headers)
        end = date.fromisoformat(candidate['instant'])
        if (tokens is not None and tokens['years'] == {end.year - 1} and end.month == 1 and end.day <= 7
                and not (tokens['full_dates'] or tokens['month_days'] or tokens['month_only'] or tokens['year_to_date'])
                and tokens['months'] in (None, 12) and tokens['weeks'] is None
                and candidate['instant'] in boundaries()):
            accept(column, 'fiscal_year_end', printed_year=end.year - 1)

    # A column of percentages printed beside its amounts ("Percent to Sales")
    # carries no date of its own. Every tag in it must be a pure ratio, its
    # header must say percent and print no year or date, and the tagged
    # period must be the established period of the nearest dated column to
    # its left.
    pure = {'numerator': ['{http://www.xbrl.org/2003/instance}pure'], 'denominator': []}
    for column in sorted(deferred):
        candidate, references, headers = deferred[column]
        tokens = _printed_period_tokens(headers)
        if (column in accepted or tokens is None or tokens['years'] or tokens['full_dates'] or tokens['month_days']
                or not re.search(r'%|\bpercent', ' '.join(headers), re.I)
                or any(reference['unit'] != pure for reference in references)):
            continue
        dated = [other for other in periods if other < column and periods[other] is not None
                 and (other not in deferred or other in accepted)]
        if dated and periods[max(dated)] == candidate:
            accept(column, 'ratio_of_adjacent_column', amount_column=max(dated))

    def comparatives():
        changed = True
        while pending and changed:
            changed = False
            for column in sorted(pending):
                candidate = pending[column][0]
                start, end = (date.fromisoformat(candidate[key]) for key in ('startDate', 'endDate'))
                for other in sorted(periods):
                    established = periods[other]
                    if (other == column or other in pending or established is None
                            or set(established) != {'startDate', 'endDate'}):
                        continue
                    other_start, other_end = (date.fromisoformat(established[key]) for key in ('startDate', 'endDate'))
                    if (abs((other_end - other_start).days - (end - start).days) <= 7
                            and 357 <= abs((other_end - end).days) <= 373):
                        accept(column, 'comparative_column', comparative_column=other, comparative_period=dict(established))
                        changed = True
                        break

    comparatives()

    def several_years(header):
        return len(set(re.findall(r'\b(?:19|20)\d\d\b', header))) > 1

    own = {column: _printed_period_tokens([header for header in headers if not several_years(header)])
           for column, (_, _, headers) in deferred.items()}
    # A caption belongs to this table only if the table has a column for every year it names.
    column_years = {year for tokens in own.values() if tokens is not None for year in tokens['years']}
    column_years.update(date.fromisoformat(period.get('endDate', period.get('instant'))).year
                        for period in periods.values() if period is not None)
    # A column headed by its fiscal year alone still shows the year its tags end in.
    column_years.update(date.fromisoformat(candidate.get('endDate', candidate.get('instant'))).year
                        for candidate, _, _ in deferred.values())
    for column in sorted(deferred):
        if column in accepted:
            continue
        candidate, _, headers = deferred[column]
        for caption in [*(header for header in headers if several_years(header)), *captions]:
            tokens = _printed_period_tokens([caption])
            if (tokens is not None and tokens['years'] <= column_years
                    and _caption_agrees(own[column], tokens, candidate, fiscal_year_offset)):
                accept(column, 'caption_duration' if 'startDate' in candidate else 'caption_balance_date', caption=caption)
                break
    comparatives()

    # The readings below come last, so a column an earlier reading accepts keeps its evidence.
    # An annual statement headed by its year-end dates alone ("December 28,
    # 2024 / December 30, 2023"): the tags carry the fiscal year that ends on
    # the printed date and begins on the day after a balance date this filing
    # reports.
    for column in sorted(pending):
        candidate, _, tokens = pending[column]
        if (tokens['full_dates'] and balance_dates is not None and 'startDate' in candidate
                and 363 <= (date.fromisoformat(candidate['endDate']) - date.fromisoformat(candidate['startDate'])).days + 1 <= 372
                and (date.fromisoformat(candidate['startDate']) - timedelta(days=1)).isoformat() in balance_dates):
            accept(column, 'fiscal_year_ending_on_printed_date')
    comparatives()

    # A balance column that names only the fiscal period the filing reports on
    # ("As of / Second Quarter of / 2026"): the filing's own cover facts give
    # that period its last day, and the tags carry exactly that day.
    reported = document() if document is not None and any(
        column not in accepted and set(candidate) == {'instant'} for column, (candidate, _, _) in deferred.items()) else None
    if reported is not None:
        focus, last_day = reported
        for column in sorted(deferred):
            candidate, _, headers = deferred[column]
            tokens = _printed_period_tokens(headers)
            if (column not in accepted and candidate == {'instant': last_day} and tokens is not None and tokens['years'] == {focus}
                    and not (tokens['full_dates'] or tokens['month_days'] or tokens['month_only'] or tokens['year_to_date'])
                    and tokens['weeks'] is None):
                accept(column, 'document_period_end', fiscal_year_focus=focus)

    return accepted


def _endpoint_balance_period(reference, preceding_text, header):
    """A cash balance printed in a cash-flow column that covers a period.

    XBRL ties a balance to a date, so the components of the opening and
    closing cash balance ("Cash and cash equivalents", "Restricted cash",
    "Total") carry a date under the statement's "... ended" headers. Such a
    tag is accepted when its concept is a cash balance, and its date is the
    column's last day and the row names no opening balance, or the day before
    the column's first day and the row names no closing balance.

    Any other date stays unresolved. So does any other balance concept: a
    liability such as dividends payable on a supplemental row that reports a
    flow is a company tagging error, which only the flagged profile records.
    """
    if set(header) != {'startDate', 'endDate'} or set(reference['period']) != {'instant'}:
        return None
    if 'Cash' not in reference['concept'].rsplit('}', 1)[-1]:
        return None
    instant = reference['period']['instant']
    opening = (date.fromisoformat(header['startDate']) - timedelta(days=1)).isoformat()
    words = preceding_text.lower()
    if instant == header['endDate'] and not re.search(r'\b(?:beginning|begin|start|opening|prior|previous)\b', words):
        return {'instant': instant}
    if instant == opening and not re.search(r'\b(?:end|ending|closing)\b', words):
        return {'instant': instant}
    return None


_PERIOD_END_LABEL = re.compile(
    r'\b(?:at|as of) (?:the )?(?:(?:period|year|quarter)[- ]end|end of (?:the )?(?:fiscal )?(?:period|year|quarter))\b')


def _labelled_period_end(row_label, column_period):
    """Whether a row label dates its number at the last day of the column's period.

    tables-v3 corroborated layouts only. An income statement may close with
    a balance, usually the shares outstanding, in a row that says so:
    "Shares outstanding at March 31," or "Actual shares outstanding at period
    end". The label must name the period's end in those words or by the month
    and day of the column's last day, and the year too if it prints one. The
    caller still requires the tag's own date to be that day.
    """
    if set(column_period) != {'startDate', 'endDate'} or not isinstance(row_label, str):
        return False
    label = ' '.join(row_label.lower().split())
    if _PERIOD_END_LABEL.search(label):
        return True
    match = re.search(r'\b(?:at|as of) (' + _MONTH_NAMES + r')\.? (\d{1,2})(?:, ?((?:19|20)\d\d))?,?$', label)
    if match is None:
        return False
    end = date.fromisoformat(column_period['endDate'])
    return (_MONTH_NUMBERS[match[1]], int(match[2])) == (end.month, end.day) and match[3] in (None, str(end.year))


def _displayed_header_rows(rows, first_data):
    """The header rows placed where the page displays them, or None.

    A cell styled `display:none` takes no room on the page, so a row is
    displayed without the grid columns its hidden cells cover. A statement
    usually hides the same spacer cells in every row. When a header row hides
    fewer of them than the rows that carry the numbers, its captions are
    displayed further right than the physical grid places them: "Three Months
    Ended" stands above the two quarter columns on the page, but above the
    first of them and two hidden spacers in the grid.

    Each visible header cell is returned at the grid columns of the numbered
    rows it is displayed above. Nothing is returned unless the page itself
    lines the rows up: every numbered row hides the same columns and has the
    same width, no cell spans rows, and every header row has the displayed
    width of the numbered rows.
    """
    def hides(cell):
        style = re.sub(r'\s+', '', cell['_element'].get('style', '').lower())
        return bool(re.search(r'(?:^|;)display:none(?:!important)?(?:;|$)', style))

    def layout(row):
        hidden = {column for cell in row if hides(cell)
                  for column in range(cell['column'], cell['column'] + cell['colspan'])}
        return max((cell['column'] + cell['colspan'] for cell in row), default=0), hidden

    if any(cell['rowspan'] != 1 for row in rows for cell in row):
        return None
    numbered = [layout(row) for row in rows[first_data:]
                if any(local(node) == 'nonfraction' for cell in row for node in cell['_element'].iter())]
    if not numbered or any(item != numbered[0] for item in numbered):
        return None
    width, hidden = numbered[0]
    visible = [column for column in range(width) if column not in hidden]
    moved, result = False, []
    for row in rows[:first_data]:
        row_width, row_hidden = layout(row)
        if not row or (row_width, row_hidden) == (width, hidden):
            result.append(row)
            continue
        if all(cell['column'] == 0 or hides(cell) or not cell['display_text'].strip(_PRINTED) for cell in row):
            # A shorter row that prints nothing right of the label column (a
            # sizing row, a row of hidden cells, a section title) has no
            # caption that could stand above another column.
            result.append(row)
            continue
        if row_width - len(row_hidden) != len(visible):
            return None
        placed = []
        for cell in row:
            if hides(cell):
                continue
            start = cell['column'] - sum(column < cell['column'] for column in row_hidden)
            column = visible[start]
            placed.append({**cell, 'column': column, 'colspan': visible[start + cell['colspan'] - 1] - column + 1})
            moved = moved or (column, placed[-1]['colspan']) != (cell['column'], cell['colspan'])
        result.append(placed)
    return result if moved else None


_PRINTED = '​﻿\xa0 \t\r\n'
_NOTE_NUMBERS = r'[0-9]{1,2}[A-Za-z]?(?:\s*(?:,|&|and)\s*[0-9]{1,2}[A-Za-z]?)*'


def _label_columns(rows, first_data, periods):
    """Columns right of the first that hold the rows' labels (tables-v3 corroborated layouts only).

    A statement may leave its first grid column empty and print every row
    label in the next one. A label such as "Property, plant and equipment,
    net of accumulated depreciation of $5,909 and $5,788" then carries the
    tagged amounts of both balance dates in a column that has no period.

    A column qualifies when the header gives it no period and every cell
    printed in it below the header is the first printed cell of its row and
    reads as words. A column that prints a bare number anywhere, or anything
    after another printed cell, is not a label column.
    """
    printed = {}
    for row in rows[first_data:]:
        first = next((cell for cell in row if cell['display_text'].strip(_PRINTED)), None)
        for cell in row:
            if cell['column'] > 0 and cell['display_text'].strip(_PRINTED):
                printed.setdefault(cell['column'], []).append(
                    cell is first and bool(re.search(r'[A-Za-z]{3}', cell['display_text'])))
    return frozenset(column for column, labels in printed.items() if periods.get(column) is None and all(labels))


def _note_reference(cell, tagged_columns):
    """An untagged cell that only points to a note (tables-v3 corroborated layouts only).

    The cell stands in a column that the header gives no period and that
    holds no tagged number anywhere. It prints either the reference in words
    ("(Note 3)", "Notes 6 and 7") or, under a heading that is exactly "Note"
    or "Notes", the note numbers alone ("13", "4, 5").
    """
    if cell['column_period'] is not None or cell['column'] in tagged_columns:
        return False
    value = cell['display_text'].strip(_PRINTED)
    if re.fullmatch(r'\(?\s*(?:see\s+)?notes?\s+' + _NOTE_NUMBERS + r'\s*\)?', value, re.I):
        return True
    headings = [heading.strip(_PRINTED) for heading in cell['column_headings'] if heading.strip(_PRINTED)]
    return (bool(headings) and all(re.fullmatch(r'notes?', heading, re.I) for heading in headings)
            and bool(re.fullmatch(_NOTE_NUMBERS, value)))


def _balance_date_captions(row, period_captions):
    """Whether a body row prints nothing but full dates right of its label.

    tables-v3 corroborated layouts only. A cash flow statement may close with
    a reconciliation of cash to the balance sheets that is headed by the two
    balance dates. The row holds no tag, prints at least one cell right of
    the label, and each of those cells is one full date. A row that only
    restates the ends of its own columns (`_restated_period_captions`) is a
    caption of the statement itself and opens no such section.
    """
    cells = [cell for cell in row if cell['column'] > 0 and cell['display_text'].strip(_PRINTED)]
    if not cells or all(cell['cell_id'] in period_captions for cell in cells):
        return False
    if any(local(node) in ('nonfraction', 'nonnumeric') for cell in row for node in cell['_element'].iter()):
        return False
    return all(re.fullmatch(r'(' + _MONTH_NAMES + r')\.? ?(\d{1,2}), ?((?:19|20)\d\d)',
                            ' '.join(cell['display_text'].strip(_PRINTED).split()), re.I) for cell in cells)


def _dated_parentheticals(rows, first_data, periods, facts, html_ids):
    """Balances a row prints under their own month and year, left of the period columns.

    tables-v3 corroborated layouts only. A balance sheet may give the shares
    issued at each balance date a row of its own: "Common stock – shares
    issued –", "March 2026", "4,009.2", before the amounts under the dated
    columns or in a row without any. Left of the first column that has a
    period, every number such a row prints must be tagged, each tag must be an
    instant, and a cell left of the number must be exactly that instant's
    month and year. A column of amounts whose header could not be read has no
    such caption in its rows, so it is never taken for one of these. Returns
    the ids of the tagged cells and of the caption cells.
    """
    with_period = [column for column, period in periods.items() if period is not None]
    if not with_period:
        return frozenset(), frozenset()
    first_period = min(with_period)
    tagged, captions = set(), set()
    for row in rows[first_data:]:
        row_tagged, row_captions, named = [], [], set()
        for cell in row:
            if cell['column'] + cell['colspan'] > first_period:
                break
            text = ' '.join(cell['display_text'].strip(_PRINTED).split())
            numbers = [node for node in cell['_element'].iter() if local(node) == 'nonfraction']
            if numbers:
                dates = set()
                for node in numbers:
                    evidence = facts.get(node.get('id'))
                    if evidence is None or html_ids.get(node.get('id'), 0) != 1 or set(evidence['period']) != {'instant'}:
                        dates = None
                        break
                    instant = date.fromisoformat(evidence['period']['instant'])
                    dates.add((instant.month, instant.year))
                if not dates or not dates <= named or cell['column'] == 0:
                    row_tagged = None
                    break
                row_tagged.append(cell['cell_id'])
            else:
                caption = re.fullmatch(r'(' + _MONTH_NAMES + r')\.? ((?:19|20)\d\d)', text, re.I)
                if caption:
                    named.add((_MONTH_NUMBERS[caption[1].lower()], int(caption[2])))
                    if cell['column'] > 0:
                        row_captions.append(cell['cell_id'])
                elif cell['column'] > 0 and re.search(r'[0-9]', text):
                    row_tagged = None
                    break
        if row_tagged:
            tagged.update(row_tagged)
            captions.update(row_captions)
    return frozenset(tagged), frozenset(captions)


def _spacer_zero(element):
    """An untagged "0" set in type of one point or less (tables-v3 corroborated layouts only).

    A filing tool prints such a zero to keep an otherwise empty ruled row
    open; at that size it is no readable amount. The one "0" must be every
    printed character of the cell, and the nearest element around it that
    sets a font size must set at most one point.
    """
    if any((child.tail or '').strip(_PRINTED) for child in element.iter() if child is not element):
        return False
    printed = [node for node in element.iter() if isinstance(node.tag, str) and (node.text or '').strip(_PRINTED)]
    if len(printed) != 1 or printed[0].text.strip(_PRINTED) != '0':
        return False
    node = printed[0]
    while node is not None:
        size = re.search(r'(?:^|;)\s*font-size\s*:\s*([0-9]*\.?[0-9]+)\s*pt\s*(?:;|$)', node.get('style') or '', re.I)
        if size is not None:
            return float(size[1]) <= 1.0
        if node is element:
            return False
        node = node.getparent()
    return False


def _restated_period_captions(rows, first_data, periods):
    """Cells of body rows that only restate the end of their own columns' periods.

    tables-v3 corroborated layouts only. A cash flow statement often prints a
    second line of dates above the reconciliation of its closing cash ("June
    30," over "2026" and "2025", or "March 29, 2026" beside the caption). A
    row qualifies when it holds no tag and every cell right of the label that
    prints anything is such a caption: a full date equal to the end of every
    period column the cell spans, a month and day equal to those ends, or,
    directly below a row of month-and-day captions, the year of those ends.
    A printed amount is none of these, so no number can be taken for a
    caption.
    """
    captions, below_days = set(), False
    for row in rows[first_data:]:
        cells = [cell for cell in row if cell['column'] > 0 and cell['display_text'].strip(_PRINTED)]
        kinds = []
        if cells and not any(local(node) in ('nonfraction', 'nonnumeric') for cell in row for node in cell['_element'].iter()):
            for cell in cells:
                covered = [periods.get(column) for column in range(cell['column'], cell['column'] + cell['colspan'])]
                ends = [date.fromisoformat(period.get('endDate') or period['instant']) for period in covered
                        if period and (period.get('endDate') or period.get('instant'))]
                value = ' '.join(cell['display_text'].strip(_PRINTED).split())
                full = re.fullmatch(r'(' + _MONTH_NAMES + r')\.? ?(\d{1,2}), ?((?:19|20)\d\d)', value, re.I)
                day = re.fullmatch(r'(' + _MONTH_NAMES + r')\.? ?(\d{1,2}),?', value, re.I)
                if not ends:
                    kinds.append(None)
                elif full and all((end.year, end.month, end.day) == (int(full[3]), _MONTH_NUMBERS[full[1].lower()], int(full[2]))
                                  for end in ends):
                    kinds.append('date')
                elif day and all((end.month, end.day) == (_MONTH_NUMBERS[day[1].lower()], int(day[2])) for end in ends):
                    kinds.append('day')
                elif below_days and re.fullmatch(r'(?:19|20)\d\d', value) and all(end.year == int(value) for end in ends):
                    kinds.append('year')
                else:
                    kinds.append(None)
        if kinds and all(kinds):
            captions.update(cell['cell_id'] for cell in cells)
            below_days = all(kind == 'day' for kind in kinds)
        else:
            below_days = False
    return frozenset(captions)


def _column_periods(rows, first_data, facts, ns, html_ids, *, advanced=False, tagging_defects=False,
                    corroborate=False, displayed=False):
    balance_dates, fiscal_year_offset = _fiscal_calendar_evidence(facts) if advanced else (None, None)
    deferred, candidates = {}, {}
    columns = {cell['column'] for row in rows[first_data:] for cell in row if cell['column'] > 0}
    headers = {column: column_headers(rows, first_data, column, advanced=advanced) for column in columns}
    # A left-hand header such as "Year ended December 31" applies to the
    # adjacent year columns even when its physical colspan covers only labels.
    shared = [cell['display_text'] for row in rows[:first_data] for cell in row
              if cell['column'] == 0 and re.search(r'\b(?:ended|january|february|march|april|may|june|july|august|september|october|november|december)\b', cell['display_text'], re.I)]
    for column, values in headers.items():
        direct = header_period(values, advanced=advanced)
        if shared and (direct is None or set(direct) == {'instant'}):
            headers[column] = [*shared, *values]
    periods = {column: header_period(values, advanced=advanced) for column, values in headers.items()}
    provenance = {}
    for column in columns:
        references = []
        for row in rows[first_data:]:
            for cell in row:
                if cell['column'] == 0 or not cell['column'] <= column < cell['column'] + cell['colspan']:
                    continue
                for element in cell['_element'].iter():
                    if local(element) != 'nonfraction' or html_ids.get(element.get('id'), 0) != 1:
                        continue
                    evidence = facts.get(element.get('id'))
                    if evidence is None or set(evidence['period']) not in ({'startDate', 'endDate'}, {'instant'}):
                        continue
                    try:
                        verify_inline(element, evidence, ns)
                    except TableExtractionError:
                        continue
                    references.append(evidence)
        durations = [reference for reference in references if 'startDate' in reference['period']]
        references = durations or references
        observed = {tuple(sorted(reference['period'].items())) for reference in references}
        if len(observed) != 1 or len({reference['concept'] for reference in references}) < 2:
            if tagging_defects and advanced and len(references) >= 2:
                flagged = _tagging_defect_candidate(references, balance_dates, headers[column], advanced=advanced,
                                                    fiscal_year_offset=fiscal_year_offset)
                if flagged is not None and flagged[0] != periods[column]:
                    periods[column] = flagged[0]
                    provenance[str(column)] = {'method': flagged[1], 'period': flagged[0], 'tagged_periods': flagged[2],
                                               'xml_ids': sorted({reference['xml_id'] for reference in references})}
            continue
        candidate = candidates[column] = dict(observed.pop())
        resolved = header_period(headers[column], context_period=candidate, advanced=advanced,
                                 balance_dates=balance_dates, fiscal_year_offset=fiscal_year_offset)
        source_headers, calendar_sources = [], []
        if advanced and resolved != candidate:
            revised, source_headers = _v2_period_headers(rows, first_data, column, headers[column])
            resolved = header_period(revised, context_period=candidate, advanced=True,
                                     balance_dates=balance_dates, fiscal_year_offset=fiscal_year_offset)
            if resolved != candidate:
                table = next(cell for row in rows for cell in row)['_element'].xpath('ancestor::table[1]')[0]
                corroborated, calendar_sources = _v2_calendar_period(revised, candidate, table)
                if corroborated is not None:
                    resolved = corroborated
            # A column whose printed header already gives the tagged period
            # needs nothing, and must keep its recorded evidence unchanged.
            if corroborate and candidate not in (resolved, periods[column]):
                deferred[column] = (candidate, references, _without_markers(
                    _column_header_cells(rows, first_data, column, advanced=advanced), revised))
        if resolved is None and tagging_defects and advanced:
            flagged = _tagging_defect_candidate(references, balance_dates, headers[column], advanced=advanced,
                                                fiscal_year_offset=fiscal_year_offset)
            if flagged is not None and flagged[0] != periods[column]:
                periods[column] = flagged[0]
                provenance[str(column)] = {'method': flagged[1], 'period': flagged[0], 'tagged_periods': flagged[2],
                                           'xml_ids': sorted({reference['xml_id'] for reference in references})}
            continue
        if resolved is not None and resolved != periods[column]:
            periods[column] = resolved
            provenance[str(column)] = {
                'method': ('corroborated_column_contexts' if all(re.fullmatch(r'(?:19|20)\d\d', value.strip()) for value in headers[column])
                           else 'corroborated_fiscal_contexts'), 'period': resolved,
                'context_refs': sorted({reference['context_ref'] for reference in references}),
                'xml_ids': sorted({reference['xml_id'] for reference in references}),
            }
            if source_headers:
                provenance[str(column)]['header_sources'] = source_headers
            if calendar_sources:
                provenance[str(column)]['calendar_sources'] = calendar_sources
    if deferred:
        table = next(cell for row in rows for cell in row)['_element'].xpath('ancestor::table[1]')[0]
        captions = _memo(('caption_lines', table), lambda: tuple(_caption_lines(table)))
        _corroborate_columns(deferred, periods, provenance, balance_dates, fiscal_year_offset, captions,
                             boundaries=lambda: _fiscal_year_boundaries(facts), document=lambda: _document_period(facts))
    # A column whose tags all carry one period that its header, read from the
    # physical grid, still contradicts: read the header rows where the page
    # displays them. The reading is taken only when every tagged column of the
    # table then gets exactly its tagged period; otherwise nothing changes.
    if (corroborate and not displayed
            and any(periods[column] != candidate for column, (candidate, _, _) in deferred.items())):
        shown_rows = _displayed_header_rows(rows, first_data)
        if shown_rows is not None:
            shown_rows = [*shown_rows, *rows[first_data:]]
            shown_periods, shown_provenance = _column_periods(
                shown_rows, first_data, facts, ns, html_ids, advanced=advanced, tagging_defects=tagging_defects,
                corroborate=True, displayed=True)
            if all(shown_periods.get(column) == candidate for column, candidate in candidates.items()):
                for column in sorted(candidates):
                    physical = column_headers(rows, first_data, column, advanced=advanced)
                    shown = column_headers(shown_rows, first_data, column, advanced=advanced)
                    if physical != shown:
                        evidence = shown_provenance.setdefault(
                            str(column), {'method': 'displayed_header_alignment', 'period': shown_periods[column]})
                        evidence.update(physical_headings=physical, displayed_headings=shown)
                return shown_periods, shown_provenance
    return periods, provenance


@dataclass(frozen=True)
class StatementSegment:
    statement_type: str
    table_key: str
    element: etree._Element
    group: dict | None = None


def _segment_facts(facts, *, unidentified_cover_facts=False):
    records = facts.values() if isinstance(facts, Mapping) else facts
    result = {}
    for record in records:
        if isinstance(record, FactInput):
            reference = record.source_reference
            period = ({'instant': record.instant_date.isoformat()} if record.period_kind == 'instant'
                      else {'startDate': record.period_start.isoformat(), 'endDate': record.period_end.isoformat()}
                      if record.period_kind == 'duration' else {'forever': ''})
            identifier = reference.get('element_id')
            if (identifier is None and unidentified_cover_facts and record.concept_namespace.startswith(SEC_COVER_NAMESPACES)
                    and record.occurrence_key == f"xpath:{reference.get('xpath')}"):
                identifier = record.occurrence_key
            value = {'xml_id': identifier,
                     'concept': '{' + record.concept_namespace + '}' + record.concept_name,
                     'context_ref': reference.get('context_ref'), 'unit_ref': reference.get('unit_ref'),
                     'unit': record.unit, 'period': period, 'dimensions': record.dimensions,
                     'raw_value': '' if record.is_nil else record.raw_value, 'is_nil': record.is_nil,
                     'entity': reference.get('entity')}
        else:
            value = record
        key = value.get('xml_id')
        if not key or key in result:
            raise TableExtractionError('Statement grouping requires unique source fact IDs')
        result[key] = value
    return result


def _segment_heading_signatures(table, kind):
    signatures = set()
    for value in _statement_heading_values(table):
        value = re.sub(r'[\s:–—-]*(?:\(\s*)?continued(?:\s*\))?\s*$', '', value, flags=re.I)
        if _statement_heading(value) != kind:
            continue
        value = re.sub(r'\([^)]*\b(?:unaudited|audited)\b[^)]*\)', '', value, flags=re.I)
        value = re.sub(r'([a-z])([A-Z])', r'\1 \2', value).lower()
        signatures.add(re.sub(r'[^a-z0-9]+', ' ', value).strip())
    return signatures


def _segment_candidate(tree, table, kind, facts, html_ids, *, inherited_periods=None, heading_evidence=(), concept_groups=None,
                       corroborate=False, label_columns=False, parent_detail=False):
    concept_groups = _STATEMENT_CONCEPTS if concept_groups is None else concept_groups
    rows, _ = layout_grid(table, tree)
    first = next((index for index, row in enumerate(rows) if any(
        local(element) == 'nonfraction' for cell in row for element in cell['_element'].iter())), None)
    if first is None:
        return None
    periods, _ = _column_periods(rows, first, facts, {}, html_ids, advanced=True, corroborate=corroborate)
    if inherited_periods:
        for column, period in inherited_periods.items():
            if periods.get(column) is not None:
                raise TableExtractionError('Inherited headers must not replace a displayed column period')
            periods[column] = period
    # `label_columns` (tables-v3 `extended` selection only): the numbers a row
    # label carries are no statement column, as in the first grid column.
    labels = _label_columns(rows, first, periods) if label_columns else frozenset()
    parent_rows = False
    axes, coverage, evidence, scopes, references = [], {}, {}, set(), {}
    for row in rows[first:]:
        for cell in row:
            if cell['column'] == 0 or cell['column'] in labels:
                continue
            period = periods.get(cell['column'])
            elements = [element for element in cell['_element'].iter()
                        if local(element) == 'nonfraction' and not _hidden(element)]
            if not elements:
                continue
            if period is None:
                raise TableExtractionError('Statement segment has an unresolved column period')
            axis = json.dumps(period, sort_keys=True)
            if axis not in axes:
                axes.append(axis)
                coverage[axis], evidence[axis], references[axis] = set(), {}, []
            for element in elements:
                ref = facts.get(element.get('id'))
                if ref is None or html_ids.get(element.get('id')) != 1:
                    raise TableExtractionError('Statement segment lacks unique XML/inline evidence')
                verify_inline(element, ref, {})
                references[axis].append(ref)
                entity = ref.get('entity')
                if not isinstance(entity, dict) or not entity.get('scheme') or not entity.get('identifier'):
                    raise TableExtractionError('Statement segment lacks source entity identity')
                if any(dimension.get('member', '').endswith('}ParentCompanyMember') for dimension in ref['dimensions']):
                    if not parent_detail:
                        return None
                    parent_rows = True
                scopes.add(json.dumps({'entity': entity, 'unit': ref['unit'], 'dimensions': ref['dimensions']}, sort_keys=True))
                concept = ref['concept'].split('}', 1)[-1]
                if not re.fullmatch(r'\{https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^}]+\}[^}]+', ref['concept']):
                    continue
                for index, group in enumerate(concept_groups[kind]):
                    matching_period = ref['period'] == period
                    if kind == 'cash_flow' and index == 3:
                        matching_period = ref['period'] == {'instant': period.get('endDate')}
                    if concept in group and matching_period and not ref['is_nil']:
                        coverage[axis].add(index)
                        evidence[axis].setdefault(str(index), []).append(ref['xml_id'])
    # `parent_detail` (the last tables-v3 selection attempts only): rows of the
    # parent company are detail of a statement that also holds numbers without
    # any dimension. A table of parent-company numbers alone is the parent's
    # own statement and no candidate, as always.
    if parent_rows and all(json.loads(scope)['dimensions'] for scope in scopes):
        return None
    # Scope matching for split cash-flow segments is deliberately strict. It
    # must not join a parent, subsidiary, currency or dimensional statement.
    candidate = {'table': table, 'axes': axes, 'coverage': coverage, 'evidence': evidence, 'references': references,
                 'scopes': scopes, 'headings': _segment_heading_signatures(table, kind),
                 'continued': kind in _statement_headings(table, continued=True)}
    if heading_evidence:
        candidate.update(headings={item.signature for item in heading_evidence if item.kind == kind},
                         continued=any(item.continued for item in heading_evidence if item.kind == kind),
                         consolidated=any(item.consolidated for item in heading_evidence if item.kind == kind))
    return candidate


def _undated_candidate(table, kind, facts, heading_evidence, concept_groups):
    """A titled table whose columns no header dates (the last tables-v3 selection attempts only).

    Such a table used to stop the whole filing. It still does (None) when it
    could be the statement: its undimensioned standard facts name, by concept,
    every group the statement needs. A table none of whose numbers has the
    registrant's own scope is another registrant's and is left out (False).
    Any other table is kept as a candidate without periods: it can never be
    selected and adds nothing to a statement, but it still shares its title
    with the tables beside it, and the concept groups it holds still count
    against a page that may only supplement a statement.
    """
    names, scopes = set(), set()
    for element in table.iter():
        if local(element) != 'nonfraction' or _hidden(element):
            continue
        reference = facts.get(element.get('id'))
        if reference is None or not isinstance(reference.get('entity'), dict):
            return None
        scopes.add(json.dumps({'entity': reference['entity'], 'unit': reference['unit'], 'dimensions': reference['dimensions']},
                              sort_keys=True))
        if (not reference['dimensions'] and not reference['is_nil']
                and re.fullmatch(r'\{https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^}]+\}[^}]+', reference['concept'])):
            names.add(reference['concept'].split('}', 1)[-1])
    if not any(not json.loads(scope)['dimensions'] for scope in scopes):
        return False
    covered = {index for index, group in enumerate(concept_groups[kind]) if names & set(group)}
    if covered == set(range(len(concept_groups[kind]))):
        return None
    candidate = {'table': table, 'axes': [], 'coverage': {'undated': covered}, 'evidence': {}, 'references': {}, 'scopes': scopes,
                 'headings': _segment_heading_signatures(table, kind),
                 'continued': kind in _statement_headings(table, continued=True)}
    if heading_evidence:
        candidate.update(headings={item.signature for item in heading_evidence if item.kind == kind},
                         continued=any(item.continued for item in heading_evidence if item.kind == kind),
                         consolidated=any(item.consolidated for item in heading_evidence if item.kind == kind))
    return candidate


_RECONCILIATION_CASH_CONCEPTS = {
    'CashAndCashEquivalentsAtCarryingValue', 'RestrictedCash', 'RestrictedCashCurrent',
    'RestrictedCashAndCashEquivalents', 'RestrictedCashAndCashEquivalentsNoncurrent',
    'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
}
_CASH_TOTAL_CONCEPTS = (
    'CashAndCashEquivalentsAtCarryingValue', 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
    'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations',
)
_ENDPOINT_MONTHS = {name.lower(): month for month in range(1, 13)
                    for name in (calendar.month_name[month], calendar.month_abbr[month])} | {'sept': 9}
_NAMED_ENDPOINT = re.compile(
    r'\bcash\b.*(?:\bat|[,\u2013\u2014-])\s*(' + '|'.join(sorted(_ENDPOINT_MONTHS, key=len, reverse=True))
    + r')\.?\s+(\d{1,2})(?:\s*,?\s*((?:19|20)\d\d))?$')


def _named_cash_endpoint(concept, row_label, column_period):
    """Opening or closing cash balance labelled by its date, not by "beginning" or "end".

    "Cash and cash equivalents at January 1" and "... – September 30" name the
    balance by a day of the column's period. An opening-date label denotes the
    start of that day, which XBRL reports as the balance at the end of the
    preceding day. Only a total cash concept and a date that is exactly the
    first or the last day of the period qualify; the caller still requires the
    fact's own period to equal the result.
    """
    if (set(column_period) != {'startDate', 'endDate'} or not isinstance(row_label, str)
            or not re.fullmatch(r'\{https?://fasb\.org/us-gaap/[^}]+\}(?:' + '|'.join(_CASH_TOTAL_CONCEPTS) + ')', concept)):
        return None
    label = re.sub(r'\s*\([1-9][0-9]?\)\s*$', '', ' '.join(row_label.lower().split()))
    match = _NAMED_ENDPOINT.search(label)
    if match is None:
        return None
    try:
        start, end = date.fromisoformat(column_period['startDate']), date.fromisoformat(column_period['endDate'])
    except (TypeError, ValueError):
        return None
    named = (_ENDPOINT_MONTHS[match[1]], int(match[2]))
    year = int(match[3]) if match[3] else None
    candidates = []
    if named == (start.month, start.day) and year in (None, start.year):
        candidates.append((start - timedelta(days=1)).isoformat())
    if named == (end.month, end.day) and year in (None, end.year):
        candidates.append(end.isoformat())
    return {'instant': candidates[0]} if len(candidates) == 1 else None


def _summed_cash_components(rows, first_data, facts, html_ids):
    """Balances that add up to a total cash balance printed directly below them.

    tables-v3 corroborated layouts only. A cash flow statement may close with
    the components of its cash balance: "Cash and cash equivalents", "Funds
    held on behalf of clients", then the total. Such a component can be
    tagged with a concept whose name does not say cash. It is proved here by
    the page's own arithmetic instead: the rows directly above a row tagged
    with a total cash concept, each holding one balance per date of that
    row, add up exactly to the total for every date. A row of other dates, a
    flow, a heading or a second number in a cell ends the block, and fewer
    than two component rows prove nothing.

    Returns {component xml_id: total xml_id}.
    """
    def balances(row):
        """One balance per date in the row, as {date: (fact, xml ids)}; {} without numbers; None otherwise."""
        found = {}
        for cell in row:
            nodes = [node for node in cell['_element'].iter() if local(node) == 'nonfraction' and not _hidden(node)]
            if not nodes:
                continue
            if cell['column'] == 0:
                return None
            cell_facts = [facts.get(node.get('id')) for node in nodes]
            if any(fact is None or html_ids.get(fact['xml_id']) != 1 or fact['is_nil'] or set(fact['period']) != {'instant'}
                   for fact in cell_facts):
                return None
            first = cell_facts[0]
            # A cell may tag its one printed number twice; both tags must then say the same.
            if any(any(fact[key] != first[key] for key in ('concept', 'period', 'unit', 'dimensions', 'raw_value'))
                   for fact in cell_facts[1:]) or first['period']['instant'] in found:
                return None
            try:
                amount = Decimal(first['raw_value'])
            except (InvalidOperation, TypeError):
                return None
            if not amount.is_finite():
                return None
            found[first['period']['instant']] = (first, amount, [fact['xml_id'] for fact in cell_facts])
        return found

    proven = {}
    for index in range(first_data, len(rows)):
        totals = balances(rows[index])
        if not totals or any(fact['dimensions'] or not re.fullmatch(
                r'\{https?://fasb\.org/us-gaap/[^}]+\}(?:' + '|'.join(_CASH_TOTAL_CONCEPTS) + ')', fact['concept'])
                for fact, _, _ in totals.values()):
            continue
        parts = []
        for row in reversed(rows[first_data:index]):
            found = balances(row)
            if found == {} and not any(cell['display_text'].strip(_PRINTED) for cell in row):
                continue
            if (not found or set(found) != set(totals)
                    or any(fact['unit'] != totals[day][0]['unit'] or fact['dimensions'] for day, (fact, _, _) in found.items())):
                break
            parts.append(found)
        if len(parts) >= 2 and all(sum(part[day][1] for part in parts) == total for day, (_, total, _) in totals.items()):
            for part in parts:
                for day, (_, _, identities) in part.items():
                    proven.update({identity: totals[day][2][0] for identity in identities})
    return proven


def _cash_reconciliation_candidate(tree, table, facts, html_ids):
    rows, _ = layout_grid(table, tree)
    first = next((index for index, row in enumerate(rows) if any(local(node) == 'nonfraction'
                  for cell in row for node in cell['_element'].iter())), len(rows))
    labels = [re.sub(r'[^a-z ]', '', cell['display_text'].lower())
              for row in rows[:first] for cell in row if cell['column'] == 0]
    if not any(label.startswith('reconciliation of cash')
               and set(label.split()) <= {'reconciliation', 'of', 'cash', 'equivalents', 'and', 'restricted'} for label in labels):
        raise TableExtractionError('Intervening financial table is not an explicit cash reconciliation')
    candidate = _segment_candidate(tree, table, 'cash_flow', facts, html_ids)
    if candidate is None or not candidate['axes'] or len(candidate['scopes']) != 1:
        raise TableExtractionError('Cash reconciliation lacks a unique source scope')
    for axis, refs in candidate['references'].items():
        period = json.loads(axis)
        if set(period) != {'instant'}:
            raise TableExtractionError('Cash reconciliation requires explicit instant date headers')
        concepts = set()
        for ref in refs:
            concept = ref['concept'].split('}', 1)[-1]
            if (ref['period'] != period or ref['is_nil'] or concept not in _RECONCILIATION_CASH_CONCEPTS
                    or not re.fullmatch(r'\{https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^}]+\}[^}]+', ref['concept'])):
                raise TableExtractionError('Cash reconciliation facts disagree with dates or supported cash concepts')
            concepts.add(concept)
        if (len(concepts) < 3 or 'CashAndCashEquivalentsAtCarryingValue' not in concepts
                or 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents' not in concepts):
            raise TableExtractionError('Cash reconciliation lacks cash, restricted cash or total evidence')
    return candidate | {'role': 'cash_reconciliation'}


def _include_cash_reconciliations(tree, candidates, facts, html_ids):
    tables = [table for table in tree.xpath('//table') if not table.xpath('.//table') and not _hidden(table)]
    positions = {table: index for index, table in enumerate(tables)}
    expanded = []
    for index, candidate in enumerate(candidates):
        if index:
            previous = candidates[index - 1]
            for table in tables[positions[previous['table']] + 1:positions[candidate['table']]]:
                if any(local(node) == 'nonfraction' and not _hidden(node) for node in table.iter()):
                    expanded.append(_cash_reconciliation_candidate(tree, table, facts, html_ids))
        expanded.append(candidate)
    return expanded


def _balance_header_continuation(tree, candidates, facts, html_ids):
    """Recognize an immediately adjacent assets/liabilities split without guessing dates."""
    if len(candidates) != 1:
        return None
    first = candidates[0]
    if (not first['axes'] or len(first['scopes']) != 1
            or any(groups != {0} for groups in first['coverage'].values())
            or any(set(json.loads(axis)) != {'instant'} for axis in first['axes'])):
        return None
    scope = json.loads(next(iter(first['scopes'])))
    if scope['dimensions'] or scope['unit'] != {
            'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []}:
        return None
    tables = [table for table in tree.xpath('//table') if not _hidden(table) and not table.xpath('.//table')]
    position = tables.index(first['table'])
    if position + 1 == len(tables):
        return None
    table = tables[position + 1]
    left, right = first['table'].getparent(), table.getparent()
    immediate = (first['table'].getnext() is table or
                 (left.getnext() is right and list(left) == [first['table']] and list(right) == [table]))
    if not immediate or _statement_headings(table) or _statement_headings(table, continued=True):
        return None
    first_rows, first_grid = layout_grid(first['table'], tree)
    rows, grid = layout_grid(table, tree)
    first_data = next(index for index, row in enumerate(first_rows) if any(local(element) == 'nonfraction'
                      for cell in row for element in cell['_element'].iter()))
    data = next((index for index, row in enumerate(rows) if any(local(element) == 'nonfraction'
                 for cell in row for element in cell['_element'].iter())), None)
    if data is None or len(first_grid[0]) != len(grid[0]):
        return None
    labels = [re.sub(r'[^a-z ]', '', cell['display_text'].lower())
              for row in rows[:data] for cell in row if cell['column'] == 0]
    if not any(re.fullmatch(r'liabilities and (?:shareholders|stockholders) equity', label) for label in labels):
        return None
    columns = {cell['column'] for row in rows[data:] for cell in row if cell['column'] > 0}
    inherited = {}
    for column in columns:
        if column_headers(rows, data, column):
            raise TableExtractionError('A balance continuation must have empty displayed date headers')
        source_cells = [cell for row in first_rows[:first_data] for cell in row
                        if cell['display_text'] and cell['column'] <= column < cell['column'] + cell['colspan']]
        period = header_period([cell['display_text'] for cell in source_cells], advanced=True)
        if period is not None and set(period) == {'instant'}:
            inherited[str(column)] = {'period': period, 'source_cells': [
                {'source_xpath': cell['source_xpath'], 'display_text': cell['display_text'],
                 'row': cell['row'], 'column': cell['column'], 'colspan': cell['colspan']}
                for cell in source_cells]}
    second = _segment_candidate(tree, table, 'balance_sheet', facts, html_ids,
                                inherited_periods={int(column): value['period'] for column, value in inherited.items()})
    if (second is None or second['axes'] != first['axes'] or second['scopes'] != first['scopes']
            or any(groups != {1, 2} for groups in second['coverage'].values())):
        raise TableExtractionError('Balance continuation does not match the assets segment scope and periods')
    for axis in first['axes']:
        if any(ref['period'] != json.loads(axis) for ref in second['references'][axis]):
            raise TableExtractionError('Balance continuation facts disagree with the inherited source date')
        assets = [ref for ref in first['references'][axis] if ref['concept'].endswith('}Assets')]
        liabilities_equity = [ref for ref in second['references'][axis]
                              if ref['concept'].endswith('}LiabilitiesAndStockholdersEquity')]
        if (len(assets) != 1 or len(liabilities_equity) != 1 or assets[0]['is_nil'] or liabilities_equity[0]['is_nil']
                or Decimal(assets[0]['raw_value']) != Decimal(liabilities_equity[0]['raw_value'])):
            raise TableExtractionError('Assets do not equal the adjacent liabilities and equity total')
    keys = ['balance_sheet.segment_1', 'balance_sheet.segment_2']
    common = {'group_key': 'balance_sheet', 'segment_count': 2, 'segment_keys': keys,
              'source_xpaths': [source_xpath(tree, first['table']), source_xpath(tree, table)],
              'heading': sorted(first['headings'])[0], 'column_periods': [json.loads(axis) for axis in first['axes']],
              'scope': scope, 'combined_concept_evidence': [
                  {'period': json.loads(axis), 'groups': {**first['evidence'][axis], **second['evidence'][axis]}}
                  for axis in first['axes']]}
    inheritance = {'source_table_key': keys[0], 'source_xpath': source_xpath(tree, first['table']), 'columns': inherited}
    return (StatementSegment('balance_sheet', keys[0], first['table'], common | {'segment_index': 1}),
            StatementSegment('balance_sheet', keys[1], table, common | {'segment_index': 2, 'header_inheritance': inheritance}))


def _standalone_cash_supplement(table):
    # The nearest exact title wins. A separately titled supplemental disclosure
    # is not a continuation merely because a page header repeats the cash title.
    for value in _statement_heading_values(table):
        normalized = ' '.join(value.lower().split()).strip(':')
        if re.fullmatch(r'supplemental disclosures? of cash flows? information', normalized):
            return True
        title = re.sub(r'[\s:–—-]*(?:\(\s*)?continued(?:\s*\))?\s*$', '', value, flags=re.I)
        if _statement_heading(title) == 'cash_flow':
            return False
    return False


def _cash_closing_continuation(tree, candidates, facts, html_ids):
    if len(candidates) != 1:
        return None
    first = candidates[0]
    if (not first['axes'] or len(first['scopes']) != 1
            or any(groups != {0, 1, 2} for groups in first['coverage'].values())
            or any(set(json.loads(axis)) != {'startDate', 'endDate'} for axis in first['axes'])):
        return None
    scope = json.loads(next(iter(first['scopes'])))
    if scope['dimensions'] or scope['unit'] != {
            'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []}:
        return None
    tables = [table for table in tree.xpath('//table') if not _hidden(table) and not table.xpath('.//table')]
    position = tables.index(first['table'])
    if position + 1 == len(tables):
        return None
    table = tables[position + 1]
    left, right = first['table'].getparent(), table.getparent()
    immediate = (first['table'].getnext() is table or
                 (left.getnext() is right and list(left) == [first['table']] and list(right) == [table]))
    if not immediate or _statement_headings(table) or _statement_headings(table, continued=True):
        return None
    first_rows, first_grid = layout_grid(first['table'], tree)
    rows, grid = layout_grid(table, tree)
    first_data = next(index for index, row in enumerate(first_rows) if any(local(node) == 'nonfraction'
                      for cell in row for node in cell['_element'].iter()))
    data = next((index for index, row in enumerate(rows) if any(local(node) == 'nonfraction'
                 for cell in row for node in cell['_element'].iter())), None)
    if data is None or len(first_grid[0]) != len(grid[0]):
        return None
    labels = ' '.join(cell['display_text'].lower() for row in rows for cell in row if cell['column'] == 0)
    # Wording varies ("Net (decrease) increase", "Cash, cash equivalents and
    # restricted cash"). The labels only identify the candidate; the dated
    # opening, change and closing facts below must still reconcile exactly.
    cash = r'cash(?: and|,) cash equivalents(?:,? and restricted cash)?'
    if (not re.search(r'\bnet \(?(?:increase|decrease|change)\b', labels)
            or not re.search(r'\b' + cash + r' at beginning of (?:the )?period\b', labels)
            or not re.search(r'\b' + cash + r' at end of (?:the )?period\b', labels)):
        return None
    inherited = {}
    for column in {cell['column'] for row in rows[data:] for cell in row if cell['column'] > 0}:
        if column_headers(rows, data, column):
            raise TableExtractionError('Cash closing continuation must have empty displayed date headers')
        source_cells = [cell for row in first_rows[:first_data] for cell in row
                        if cell['display_text'] and cell['column'] <= column < cell['column'] + cell['colspan']]
        period = header_period([cell['display_text'] for cell in source_cells], advanced=True)
        if period is not None and set(period) == {'startDate', 'endDate'}:
            inherited[str(column)] = {'period': period, 'source_cells': [
                {'source_xpath': cell['source_xpath'], 'display_text': cell['display_text'],
                 'row': cell['row'], 'column': cell['column'], 'colspan': cell['colspan']}
                for cell in source_cells]}
    second = _segment_candidate(tree, table, 'cash_flow', facts, html_ids,
                                inherited_periods={int(column): value['period'] for column, value in inherited.items()})
    if (second is None or second['axes'] != first['axes'] or second['scopes'] != first['scopes']
            or any(groups != {3} for groups in second['coverage'].values())):
        raise TableExtractionError('Cash closing continuation disagrees with the primary statement scope')
    closure = []
    cash_name = 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
    change_name = cash_name + 'PeriodIncreaseDecreaseIncludingExchangeRateEffect'
    for axis in first['axes']:
        period = json.loads(axis)
        beginning = {'instant': (date.fromisoformat(period['startDate']) - timedelta(days=1)).isoformat()}
        ending = {'instant': period['endDate']}
        groups = {'opening': [], 'change': [], 'closing': []}
        for ref in second['references'][axis]:
            if ref['is_nil'] or not re.fullmatch(r'\{https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^}]+\}[^}]+', ref['concept']):
                raise TableExtractionError('Cash closing evidence needs reported standard cash concepts')
            name = ref['concept'].split('}', 1)[-1]
            key = ('opening' if name == cash_name and ref['period'] == beginning else
                   'closing' if name == cash_name and ref['period'] == ending else
                   'change' if name == change_name and ref['period'] == period else None)
            if key is None:
                raise TableExtractionError('Cash closing dates or concept differ from inherited header evidence')
            groups[key].append(ref)
        if any(len(values) != 1 for values in groups.values()):
            raise TableExtractionError('Cash closing needs one opening, change and closing value per period')
        activity_ids = [first['evidence'][axis].get(str(index), []) for index in range(3)]
        if any(len(values) != 1 for values in activity_ids):
            raise TableExtractionError('Cash closing needs unique primary activity totals')
        delta = Decimal(groups['change'][0]['raw_value'])
        opening, closing = Decimal(groups['opening'][0]['raw_value']), Decimal(groups['closing'][0]['raw_value'])
        activities = [Decimal(facts[values[0]]['raw_value']) for values in activity_ids]
        values = [opening, delta, closing, *activities]
        with localcontext() as arithmetic:
            arithmetic.prec = max(28, max(len(value.as_tuple().digits) for value in values)
                                  + max(value.adjusted() for value in values) - min(value.adjusted() for value in values) + 10)
            if opening + delta != closing or sum(activities) != delta:
                raise TableExtractionError('Cash opening, activity changes and closing balance do not reconcile')
        closure.append({'period': period, 'activity_xml_ids': [values[0] for values in activity_ids],
                        **{key + '_xml_id': values[0]['xml_id'] for key, values in groups.items()}})
    keys = ['cash_flow.segment_1', 'cash_flow.segment_2']
    common = {'group_key': 'cash_flow', 'segment_count': 2, 'segment_keys': keys,
              'source_xpaths': [source_xpath(tree, first['table']), source_xpath(tree, table)],
              'heading': sorted(first['headings'])[0], 'column_periods': [json.loads(axis) for axis in first['axes']],
              'scope': scope, 'closure_evidence': closure, 'combined_concept_evidence': [
                  {'period': json.loads(axis), 'groups': {**first['evidence'][axis], **second['evidence'][axis]}}
                  for axis in first['axes']]}
    inheritance = {'source_table_key': keys[0], 'source_xpath': source_xpath(tree, first['table']), 'columns': inherited}
    return (StatementSegment('cash_flow', keys[0], first['table'], common | {'segment_index': 1}),
            StatementSegment('cash_flow', keys[1], table, common | {'segment_index': 2, 'header_inheritance': inheritance}))


def _segments_adjacent(tree, left, right, kind, *, numbered_footer=False):
    nodes = list(tree.iter())
    positions = {node: index for index, node in enumerate(nodes)}
    start = max(positions[node] for node in left.iter()) + 1
    end = positions[right]
    if start > end:
        return False
    intervening = nodes[start:end]
    prose = []
    footer_count = 0
    for node in intervening:
        if not isinstance(node.tag, str) or _hidden(node):
            continue
        if local(node) == 'nonfraction':
            return False
        if local(node) == 'table':
            value = text(node)
            if numbered_footer:
                footer_count += 1
                if footer_count > 1 or not re.fullmatch(
                        r'(?:\d{1,4}\s*)?[A-Za-z][A-Za-z &.,\'-]{0,80}\s+Fiscal\s+(?:19|20)\d\d\s+Form\s*10-[KQ]\s*(?:\d{1,4})?', value, re.I):
                    return False
            elif value and (len(value) > 240 or not re.search(r'\bform\s*10-[kq]\b|table of contents', value, re.I)):
                return False
        if node.text:
            prose.append(node.text)
            value = ' '.join(node.text.split())
            other = _statement_heading(value)
            if (numbered_footer and value and not node.xpath('ancestor-or-self::table')
                    and value.lower() != 'table of contents' and other != kind):
                return False
            if other is not None and other != kind:
                return False
            if re.match(r'^(?:note\s+\d+\b|notes to\b)', value, re.I):
                return False
        if node.tail:
            prose.append(node.tail)
            value = ' '.join(node.tail.split())
            if (numbered_footer and value and not node.xpath('ancestor::table')
                    and value.lower() != 'table of contents' and _statement_heading(value) != kind):
                return False
    return len(' '.join(prose)) <= 2000


def _cash_named_header_continuation(tree, first, table, facts, html_ids, heading_evidence):
    """Inherit omitted headers only across one proven primary cash continuation."""
    if (not first['axes'] or len(first['scopes']) != 1
            or any(groups != {0, 1} for groups in first['coverage'].values())
            or any(set(json.loads(axis)) != {'startDate', 'endDate'} for axis in first['axes'])
            or not _segments_adjacent(tree, first['table'], table, 'cash_flow', numbered_footer=True)):
        return None
    scope = json.loads(next(iter(first['scopes'])))
    if scope['dimensions'] or scope['unit'] != {
            'numerator': ['{http://www.xbrl.org/2003/iso4217}USD'], 'denominator': []}:
        return None
    first_rows, first_grid = layout_grid(first['table'], tree)
    rows, grid = layout_grid(table, tree)
    if not first_grid or not grid or len(first_grid[0]) != len(grid[0]):
        return None
    first_data = next(index for index, row in enumerate(first_rows) if any(local(node) == 'nonfraction'
                      for cell in row for node in cell['_element'].iter()))
    data = next((index for index, row in enumerate(rows) if any(local(node) == 'nonfraction'
                 for cell in row for node in cell['_element'].iter())), None)
    if data is None:
        return None
    inherited = {}
    for column in {cell['column'] for row in rows[data:] for cell in row if cell['column'] > 0}:
        if column_headers(rows, data, column):
            return None
        source_cells = [cell for row in first_rows[:first_data] for cell in row
                        if cell['display_text'] and cell['column'] <= column < cell['column'] + cell['colspan']]
        period = header_period([cell['display_text'] for cell in source_cells], advanced=True)
        if period is not None and set(period) == {'startDate', 'endDate'}:
            inherited[str(column)] = {'period': period, 'source_cells': [
                {'source_xpath': cell['source_xpath'], 'display_text': cell['display_text'],
                 'row': cell['row'], 'column': cell['column'], 'colspan': cell['colspan']}
                for cell in source_cells]}
    second = _segment_candidate(tree, table, 'cash_flow', facts, html_ids,
                                inherited_periods={int(column): value['period'] for column, value in inherited.items()},
                                heading_evidence=heading_evidence)
    if (second is None or second['axes'] != first['axes'] or second['scopes'] != first['scopes']
            or not first['headings'].intersection(second['headings'])
            or any(groups != {2, 3} for groups in second['coverage'].values())):
        return None
    cash = 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
    names = {'opening': cash, 'closing': cash,
             'change': cash + 'PeriodIncreaseDecreaseIncludingExchangeRateEffect',
             'effect': 'EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'}
    closing_row = next((index for index, row in enumerate(rows[data:], data) if row
                        and re.search(r'\bcash\b', row[0]['display_text'], re.I)
                        and re.search(r'\bend of (?:the )?period\b', row[0]['display_text'], re.I)), None)
    if closing_row is None:
        return None
    closure = []
    for axis in first['axes']:
        period = json.loads(axis)
        beginning = {'instant': (date.fromisoformat(period['startDate']) - timedelta(days=1)).isoformat()}
        ending = {'instant': period['endDate']}
        if any(ref['period'] != period for ref in first['references'][axis]):
            raise TableExtractionError('Initial cash segment facts disagree with the displayed source duration')
        for ref in second['references'][axis]:
            if ref['period'] not in (period, beginning, ending):
                raise TableExtractionError('Cash continuation fact date differs from inherited source headers')
        found = {key: [] for key in names}
        for row in rows[data:closing_row + 1]:
            for cell in row:
                if inherited.get(str(cell['column']), {}).get('period') != period:
                    continue
                for node in cell['_element'].iter():
                    if local(node) != 'nonfraction':
                        continue
                    ref = facts[node.get('id')]
                    if not re.fullmatch(r'\{https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^}]+\}[^}]+', ref['concept']):
                        continue
                    name = ref['concept'].split('}', 1)[-1]
                    for key, wanted in names.items():
                        expected = beginning if key == 'opening' else ending if key == 'closing' else period
                        if name == wanted and ref['period'] == expected:
                            found[key].append(ref)
        if any(len(found[key]) != 1 for key in ('opening', 'change', 'closing')) or len(found['effect']) > 1:
            raise TableExtractionError('Cash continuation lacks unique source opening, change or closing evidence')
        activity_ids = [first['evidence'][axis].get('0', []), first['evidence'][axis].get('1', []), second['evidence'][axis].get('2', [])]
        if any(len(identities) != 1 for identities in activity_ids):
            raise TableExtractionError('Cash continuation lacks unique primary activity totals')
        ids = [identities[0] for identities in activity_ids]
        refs = [facts[identifier] for identifier in ids] + [values[0] for values in found.values() if values]
        if any(ref['is_nil'] for ref in refs):
            raise TableExtractionError('Cash continuation arithmetic cannot use nil facts')
        from .cash_reconciliation import _aligned_amounts
        amounts = [Decimal(facts[identifier]['raw_value']) for identifier in ids]
        effect = Decimal(found['effect'][0]['raw_value']) if found['effect'] else Decimal(0)
        opening, change, closing = (Decimal(found[key][0]['raw_value']) for key in ('opening', 'change', 'closing'))
        aligned = _aligned_amounts([*amounts, effect, opening, change, closing])
        if sum(aligned[:4]) != aligned[5] or aligned[4] + aligned[5] != aligned[6]:
            raise TableExtractionError('Cash continuation activity, currency effect and balances do not reconcile')
        closure.append({'period': period, 'activity_xml_ids': ids,
                        **{key + '_xml_id': values[0]['xml_id'] for key, values in found.items() if values}})
    second['inherited_columns'], second['closure_evidence'] = inherited, closure
    return second


def _continuation_detail_scopes(first, candidate, facts):
    """The dimensional detail a later cash flow page adds to the statement's own scope, or None.

    tables-v3 `extended` selection only. The established rule joins the
    pages of a cash flow statement only when every fact of every page has
    the scope of the first: one entity, one unit, the same dimensions. A
    later page often adds rows of detail (cash held by consolidated
    entities, by segment, by location), which carry dimensions. Those are
    accepted when the page also holds the statement's own scope, every other
    scope has the same entity and unit, at least one dimension and no other
    legal entity, and each concept group the page covers is evidenced by a
    fact of the statement's own scope.
    """
    if len(first['scopes']) != 1 or not first['scopes'] <= candidate['scopes']:
        return None
    own = json.loads(next(iter(first['scopes'])))
    added = sorted(candidate['scopes'] - first['scopes'])
    for value in added:
        scope = json.loads(value)
        if (scope['entity'] != own['entity'] or scope['unit'] != own['unit'] or not scope['dimensions']
                or any(dimension.get('dimension', '').endswith('}LegalEntityAxis') for dimension in scope['dimensions'])):
            return None
    for groups in candidate['evidence'].values():
        for identities in groups.values():
            if not any(facts[identity]['dimensions'] == own['dimensions'] for identity in identities):
                return None
    return added


_LINE_BREAKS = frozenset({'div', 'p', 'td', 'th', 'li', 'tr', 'table', 'body', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'})


def _begins_its_line(node, tail):
    """Whether a piece of text (`node.text`, or `node.tail`) is the first thing its line prints.

    "Note 3." at the start of a line is a note heading; after "For further
    details, see" in a footnote it is a reference to that note. A line starts
    with its paragraph, cell or block, and after a table or a line break.
    """
    if local(node) in _LINE_BREAKS:
        return True
    if tail and text(node):
        return False
    current = node
    while True:
        sibling = current.getprevious()
        while sibling is not None:
            if (sibling.tail or '').strip():
                return False
            if isinstance(sibling.tag, str):
                if local(sibling) in _LINE_BREAKS:
                    return True
                if text(sibling):
                    return False
            sibling = sibling.getprevious()
        parent = current.getparent()
        if parent is None:
            return True
        if (parent.text or '').strip():
            return False
        if local(parent) in _LINE_BREAKS:
            return True
        current = parent


def _page_furniture_between(tree, left, right, kind):
    """Nothing but footnotes, page furniture and the repeated title separates two tables.

    tables-v3 `extended` selection only; `_segments_adjacent` is the
    established rule. No table with a number or with more than a page
    footer's text, no title of another statement and no note heading may
    stand between the two. A footnote may refer to a note by its number.
    """
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
        for tail, piece in enumerate((node.text, node.tail)):
            value = ' '.join((piece or '').split())
            length += len(value)
            if _statement_heading(value) not in {None, kind} or (
                    re.match(r'^(?:note\s+\d+\b|notes to\b)', value, re.I) and _begins_its_line(node, bool(tail))):
                return False
    return length <= 2000


def _period_tables(tree, kind, candidates, complete):
    """One statement printed as a separate table for each period length.

    tables-v3 `extended` selection only. A quarterly report may print the
    income statement once for the quarter and once for the year to date, each
    a complete table under the same title. Every complete table must share
    that title, the reporting entity and the dates its columns end on; each
    must hold periods of one length, and no two tables the same length. Any
    other candidate must carry another title and none may be a continuation.
    """
    first = complete[0]
    if (len(complete) < 2 or not first['headings'] or any(candidate['continued'] for candidate in candidates)
            or any(candidate['headings'] != first['headings'] for candidate in complete)
            or any(candidate['headings'].intersection(first['headings']) for candidate in candidates
                   if all(candidate is not member for member in complete))):
        return None
    entities = {json.dumps(json.loads(scope)['entity'], sort_keys=True) for candidate in complete for scope in candidate['scopes']}
    if len(entities) != 1:
        return None
    ends, lengths = None, []
    for candidate in complete:
        periods = [json.loads(axis) for axis in candidate['axes']]
        if any(set(period) != {'startDate', 'endDate'} for period in periods):
            return None
        days = [(date.fromisoformat(period['endDate']) - date.fromisoformat(period['startDate'])).days for period in periods]
        if max(days) - min(days) > 7 or any(abs(min(days) - other) <= 31 for other in lengths):
            return None
        lengths.append(min(days))
        table_ends = sorted(period['endDate'] for period in periods)
        if ends is not None and table_ends != ends:
            return None
        ends = table_ends
    keys = [f'{kind}.segment_{index + 1}' for index in range(len(complete))]
    common = {'group_key': kind, 'segment_count': len(complete), 'segment_keys': keys,
              'source_xpaths': [source_xpath(tree, candidate['table']) for candidate in complete],
              'heading': sorted(first['headings'])[0],
              'period_tables': {'verification_version': 'period-tables-v1', 'entity': json.loads(next(iter(entities))),
                                'column_periods': [[json.loads(axis) for axis in candidate['axes']] for candidate in complete]},
              'combined_concept_evidence': [
                  {'period': json.loads(axis), 'groups': {group: sorted(set(identities))
                   for group, identities in sorted(candidate['evidence'][axis].items())}}
                  for candidate in complete for axis in candidate['axes']]}
    return tuple(StatementSegment(kind, key, candidate['table'], common | {'segment_index': index + 1})
                 for index, (key, candidate) in enumerate(zip(keys, complete)))


def _bare_titles(headings, registrants):
    """Title signatures without a leading registrant name.

    "raymond james financial inc and subsidiaries condensed consolidated
    statements of cash flows" is the title "condensed consolidated statements
    of cash flows". A signature that is only the name is kept as it is.
    """
    from .statement_headings import _registrant_prefix

    return {heading[_registrant_prefix(heading, registrants, spaced=True):].strip() or heading for heading in headings}


def _joined_cash_segments(tree, kind, candidates, facts, html_ids, required, extended, registrants):
    """Join the pages of one cash flow statement, or raise.

    The established rule, with the `extended` additions documented where they
    apply. `registrants` (the filing's own names) lets `extended` compare the
    titles of two pages when one prints the registrant's name before it.
    """
    candidates = _include_cash_reconciliations(tree, candidates, facts, html_ids)
    first = candidates[0]
    if first['continued'] or not first['axes'] or len(first['scopes']) != 1:
        raise TableExtractionError('Cash-flow segments lack a unique initial statement scope')
    if any(set(json.loads(axis)) != {'startDate', 'endDate'} for axis in first['axes']):
        raise TableExtractionError('Cash-flow statement segments require duration column headers')
    headings = set(first['headings'])
    bare_headings = _bare_titles(first['headings'], registrants) if extended else set()
    previous_groups = set()
    detail_scopes = set()
    combined = {axis: set() for axis in first['axes']}
    combined_evidence = {axis: {} for axis in first['axes']}
    for index, candidate in enumerate(candidates):
        is_reconciliation = candidate.get('role') == 'cash_reconciliation'
        if not is_reconciliation:
            headings &= candidate['headings']
            if extended:
                bare_headings &= _bare_titles(candidate['headings'], registrants)
        wanted_axes = ([json.dumps({'instant': json.loads(axis)['endDate']}, sort_keys=True)
                        for axis in first['axes']] if is_reconciliation else first['axes'])
        same_scopes = candidate['scopes'] == first['scopes']
        if not same_scopes and extended and index and not is_reconciliation:
            # A later page may add dimensional detail to the statement's own scope.
            added = _continuation_detail_scopes(first, candidate, facts)
            if added is not None:
                same_scopes = True
                detail_scopes.update(added)
        if (candidate['axes'] != wanted_axes or not same_scopes
                or (index and not _segments_adjacent(tree, candidates[index - 1]['table'], candidate['table'], kind,
                                                    numbered_footer=bool(candidate.get('inherited_columns')))
                    and not (extended and _page_furniture_between(tree, candidates[index - 1]['table'],
                                                                  candidate['table'], kind)))):
            raise TableExtractionError('Cash-flow segments disagree on periods, scope or adjacency')
        if is_reconciliation:
            continue
        groups = set().union(*candidate['coverage'].values())
        if index and not candidate['continued'] and (not groups or previous_groups & groups
                                                    or max(previous_groups, default=-1) >= min(groups)):
            raise TableExtractionError('Unmarked cash-flow segments do not establish ordered continuation')
        previous_groups |= groups
        for axis in combined:
            combined[axis] |= candidate['coverage'][axis]
            for group, identities in candidate['evidence'][axis].items():
                combined_evidence[axis].setdefault(group, []).extend(identities)
    if not headings:
        # `extended` only, where the titles differ as printed: one page may
        # print the registrant's name before the same title.
        headings = bare_headings
    if not headings or any(groups != required for groups in combined.values()):
        raise TableExtractionError('Cash-flow segments lack matching headings or complete combined evidence')
    for candidate in candidates:
        if candidate.get('role') != 'cash_reconciliation':
            continue
        for main_axis, reconciliation_axis in zip(first['axes'], candidate['axes']):
            total = [ref for ref in candidate['references'][reconciliation_axis]
                     if ref['concept'].endswith('}CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents')]
            main_ids = combined_evidence[main_axis]['3']
            if (len(total) != 1 or not any(facts[key]['concept'] == total[0]['concept'] for key in main_ids)
                    or any(Decimal(facts[key]['raw_value']) != Decimal(total[0]['raw_value']) for key in main_ids
                           if facts[key]['concept'] == total[0]['concept'])):
                raise TableExtractionError('Cash reconciliation total differs from the source cash-flow closing balance')
    keys = [f'{kind}.segment_{index + 1}' for index in range(len(candidates))]
    common = {'group_key': kind, 'segment_count': len(candidates), 'segment_keys': keys,
              'source_xpaths': [source_xpath(tree, candidate['table']) for candidate in candidates],
              'heading': sorted(headings)[0], 'column_periods': [json.loads(axis) for axis in first['axes']],
              'scope': json.loads(next(iter(first['scopes']))),
              'combined_concept_evidence': [
                  {'period': json.loads(axis), 'groups': {group: sorted(set(identities))
                   for group, identities in sorted(combined_evidence[axis].items())}}
                  for axis in first['axes']]}
    if detail_scopes:
        common['detail_scopes'] = sorted(detail_scopes)
    has_reconciliation = any(candidate.get('role') == 'cash_reconciliation' for candidate in candidates)
    if has_reconciliation:
        common['segment_roles'] = [candidate.get('role', 'statement') for candidate in candidates]
        common['segment_column_periods'] = [[json.loads(axis) for axis in candidate['axes']] for candidate in candidates]
    inherited_candidates = [candidate for candidate in candidates if candidate.get('inherited_columns')]
    if inherited_candidates:
        if len(candidates) != 2 or len(inherited_candidates) != 1:
            raise TableExtractionError('Inherited cash headers require exactly two physical segments')
        common['closure_evidence'] = inherited_candidates[0]['closure_evidence']
    return [StatementSegment(kind, key, candidate['table'], common | {'segment_index': index + 1}
            | ({'segment_role': candidate.get('role', 'statement')} if has_reconciliation else {})
            | ({'header_inheritance': {'source_table_key': keys[0], 'source_xpath': source_xpath(tree, first['table']),
                                      'columns': candidate['inherited_columns']}} if candidate.get('inherited_columns') else {}))
            for index, (key, candidate) in enumerate(zip(keys, candidates))]


def _select_statement_segments_v2(tree, facts, *, concept_groups=None, corroborate=False, extended=False,
                                  outside_notes=False):
    """Select original physical tables; v2 currently groups cash-flow continuations.

    Complete v1 selections are preserved. Split statements need matching exact
    heading identities, source period axes, entity/unit/dimension scopes and
    document adjacency. A group cannot contain two complete statements.
    `corroborate` (tables-v3 only) reads each candidate's column periods with
    `_corroborate_columns`, as extraction then does for the selected tables.
    `extended` (with `corroborate`, tried last) gives a table that still has
    no title the `header_lines` reading of `statement_headings.recover_headings`
    and accepts a balance sheet printed as two tables whose totals tie
    (`balance_segments.tied_balance_segments`).
    `outside_notes` (with `extended`, tried after it) looks for the statements
    outside the notes only: a table inside a tagged text block is no
    candidate, whatever title stands above it, and rows of the parent company
    are detail of a table that also holds numbers without a dimension.
    """
    concept_groups = _STATEMENT_CONCEPTS if concept_groups is None else concept_groups
    try:
        return tuple(StatementSegment(kind, kind, table) for kind, table in select_statements(tree, concept_groups=concept_groups).items())
    except TableExtractionError:
        pass
    from .statement_headings import recover_headings

    facts = _segment_facts(facts)
    html_ids = {}
    for node in tree.iter():
        if local(node) == 'nonfraction' and node.get('id'):
            html_ids[node.get('id')] = html_ids.get(node.get('id'), 0) + 1
    choices = {kind: [] for kind in concept_groups}
    untitled = set()
    undated_pages = {}
    registrants = spelled_registrants = ()
    if corroborate:
        from .statement_headings import registrant_names
        names = [record.get('raw_value') for record in facts.values()
                 if re.fullmatch(r'\{https?://xbrl\.sec\.gov/dei/[^}]+\}EntityRegistrantName', record.get('concept') or '')]
        registrants = registrant_names(names)
        spelled_registrants = registrant_names(names, spellings=True) if extended else ()
    if outside_notes:
        from .page_continuation import in_note
    for table in tree.xpath('//table'):
        if _hidden(table) or table.xpath('.//table'):
            continue
        if outside_notes and in_note(table):
            continue
        kinds = _statement_headings(table) | _statement_headings(table, continued=True)
        heading_evidence = ()
        if not kinds:
            heading_evidence = _memo(('recover', table), lambda: recover_headings(table))
            kinds = {item.kind for item in heading_evidence}
        if not kinds and corroborate:
            # Only a table no established rule recognizes gets the wider titles.
            heading_evidence = _memo(('recover_wide', table),
                                     lambda: recover_headings(table, wide=True, registrants=registrants))
            kinds = {item.kind for item in heading_evidence}
        if not kinds and corroborate and extended:
            heading_evidence = _memo(('recover_header_lines', table), lambda: recover_headings(
                table, wide=True, registrants=spelled_registrants, header_lines=True))
            kinds = {item.kind for item in heading_evidence}
        if not kinds:
            untitled.add(table)
        for kind in kinds:
            if kind == 'cash_flow' and _standalone_cash_supplement(table):
                continue
            try:
                candidate = _segment_candidate(tree, table, kind, facts, html_ids, heading_evidence=heading_evidence,
                                               concept_groups=concept_groups, corroborate=corroborate,
                                               label_columns=extended, parent_detail=outside_notes)
            except TableExtractionError as exc:
                undated = str(exc) == 'Statement segment has an unresolved column period'
                candidate = None
                if kind == 'cash_flow' and len(choices[kind]) == 1 and undated:
                    candidate = _cash_named_header_continuation(tree, choices[kind][0], table, facts, html_ids, heading_evidence)
                if candidate is None and outside_notes and undated:
                    # A table whose columns no header dates stops the filing
                    # only if it could be the statement.
                    candidate = _undated_candidate(table, kind, facts, heading_evidence, concept_groups)
                    if candidate is False:
                        continue
                if candidate is None and extended and undated and choices[kind]:
                    # Extended attempts only: a page under the statement's title
                    # whose columns no header dates may be the next page of the
                    # table above it. It is no candidate of its own. `recover`
                    # joins it to that table only when the statement's own
                    # arithmetic holds across the page break; if nothing joins
                    # it, the filing is rejected below exactly as before.
                    undated_pages.setdefault(kind, []).append((table, exc))
                    continue
                if candidate is None:
                    raise
            if candidate is not None:
                choices[kind].append(candidate)
            elif (kind in _statement_headings(table, continued=True)
                  or any(item.kind == kind and item.continued for item in heading_evidence)) and not any(
                    local(element) == 'nonfraction' for element in table.iter()):
                raise TableExtractionError('A continued statement lacks verifiable numeric source facts')
    output = []
    for kind, candidates in choices.items():
        consolidated = [candidate for candidate in candidates if candidate.get('consolidated',
                        any('consolidated' in heading.split() for heading in candidate['headings']))]
        candidates = consolidated or candidates
        if outside_notes:
            # Another registrant's or a business group's statement is no candidate either.
            from .page_continuation import with_own_scope
            candidates = with_own_scope(candidates)
        required = set(range(len(concept_groups[kind])))
        complete = [candidate for candidate in candidates if candidate['axes']
                    and all(groups == required for groups in candidate['coverage'].values())]
        if kind == 'balance_sheet' and not complete:
            balance_segments = _balance_header_continuation(tree, candidates, facts, html_ids)
            if balance_segments is None:
                from .balance_segments import tied_balance_segments, verified_balance_segments
                balance_segments = verified_balance_segments(tree, candidates, facts)
                if balance_segments is None and extended:
                    balance_segments = tied_balance_segments(tree, candidates)
            if balance_segments is not None:
                output.extend(balance_segments)
                continue
        if kind == 'cash_flow' and not complete:
            closing_segments = _cash_closing_continuation(tree, candidates, facts, html_ids)
            if closing_segments is not None:
                output.extend(closing_segments)
                continue
        if kind == 'income_statement' and extended and len(complete) > 1:
            period_tables = _period_tables(tree, kind, candidates, complete)
            if period_tables is not None:
                output.extend(period_tables)
                continue
        if (kind != 'cash_flow' and len(complete) == 1 and not complete[0]['continued']
                and all(candidate is complete[0] or (not candidate['continued']
                        and not candidate['headings'].intersection(complete[0]['headings'])) for candidate in candidates)):
            output.append(StatementSegment(kind, kind, complete[0]['table']))
            continue
        if len(candidates) == 1 and len(complete) == 1 and not candidates[0]['continued']:
            output.append(StatementSegment(kind, kind, candidates[0]['table']))
            continue
        if kind != 'cash_flow' or len(candidates) < 2 or len(complete) > 1:
            recovered = None
            if extended:
                # Only where every established rule gives up: a note table
                # under the statement's title, or an untitled next page.
                from .page_continuation import recover
                recovered = recover(tree, kind, candidates, untitled | {table for table, _ in undated_pages.get(kind, ())},
                                    facts, html_ids, concept_groups)
                if recovered is None and kind == 'balance_sheet':
                    from .balance_segments import whole_balance_sheet
                    recovered = whole_balance_sheet(candidates)
            if recovered is None:
                raise TableExtractionError(f'{kind}: ambiguous or incomplete physical statement segments')
            output.extend(recovered)
            continue
        try:
            output.extend(_joined_cash_segments(tree, kind, candidates, facts, html_ids, required, extended,
                                                spelled_registrants))
        except TableExtractionError:
            # Only where the pages cannot be joined: a complete statement
            # followed by pages that supplement it.
            whole = None
            if extended:
                from .page_continuation import statement_before_supplements
                whole = statement_before_supplements(tree, kind, candidates, required)
            if whole is None:
                raise
            output.append(StatementSegment(kind, kind, whole['table']))
    # An undated page that no statement took as its next page still stops the filing.
    selected = {segment.element for segment in output}
    for pages in undated_pages.values():
        for table, error in pages:
            if table not in selected:
                raise error
    if len({segment.element for segment in output}) != len(output):
        raise TableExtractionError('Different statements cannot reuse the same physical table')
    return tuple(output)


def _validate_primary_scope(segments, facts):
    """Core evidence must establish one whole reporting entity for each kind.

    Detail rows may legitimately use the same concepts with product/portfolio
    dimensions. They cannot supply the undimensioned primary-statement evidence.
    """
    entities, undimensioned = set(), set()
    for segment in segments:
        concepts = set().union(*_STATEMENT_CONCEPTS[segment.statement_type])
        for element in segment.element.iter():
            if local(element) != 'nonfraction' or _hidden(element):
                continue
            ref = facts.get(element.get('id'))
            if ref is None or ref['concept'].split('}', 1)[-1] not in concepts:
                continue
            if any(dimension.get('dimension', '').endswith('}LegalEntityAxis')
                   or dimension.get('member', '').endswith('}ParentCompanyMember')
                   for dimension in ref['dimensions']):
                raise TableExtractionError('Primary statement core facts use another registrant scope')
            entity = ref.get('entity')
            if not isinstance(entity, dict) or not entity.get('scheme') or not entity.get('identifier'):
                raise TableExtractionError('Primary statement core facts lack source entity identity')
            entities.add((entity['scheme'], entity['identifier']))
            if not ref['dimensions']:
                undimensioned.add(segment.statement_type)
    if undimensioned != {segment.statement_type for segment in segments}:
        raise TableExtractionError('Primary statements lack undimensioned core facts')
    if len(entities) != 1:
        raise TableExtractionError('Primary statements require one reporting entity')
    return segments


# Tables printed directly after a selected cash flow statement that belong to
# it: a reconciliation of the closing cash balance and the supplemental cash
# flow disclosures. Only the V8 profile extracts them as further segments; a
# table that does not match the statement exactly stays outside, where V7's
# omitted-continuation inventory reports it.
_FOLLOWER_RECONCILIATION_CONCEPTS = _RECONCILIATION_CASH_CONCEPTS | {'RestrictedCashNoncurrent'}
_FOLLOWER_CASH_TOTAL = 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'
_US_GAAP_CONCEPT = re.compile(r'\{https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^}]+\}[^}]+')


def _closing_cash_reconciliation_title(table):
    """The nearest exact title names a reconciliation of cash and restricted cash."""
    for value in _statement_heading_values(table):
        normalized = ' '.join(value.lower().split()).strip(':')
        if re.fullmatch(r'reconciliation of cash(?:,? cash equivalents)?,? and restricted cash(?: and cash equivalents)?',
                        normalized):
            return True
        title = re.sub(r'[\s:–—-]*(?:\(\s*)?continued(?:\s*\))?\s*$', '', value, flags=re.I)
        if _statement_heading(title) is not None:
            return False
    return False


def _statement_closing_cash(cash_segments, facts):
    """Closing total cash facts printed in the statement, by concept and date."""
    closing = {}
    for segment in cash_segments:
        for node in segment.element.iter():
            if local(node) != 'nonfraction' or _hidden(node):
                continue
            ref = facts.get(node.get('id'))
            if (ref is None or ref['is_nil'] or set(ref['period']) != {'instant'}
                    or not _US_GAAP_CONCEPT.fullmatch(ref['concept'])):
                continue
            concept = ref['concept'].split('}', 1)[-1]
            if concept in _CASH_TOTAL_CONCEPTS:
                closing.setdefault((concept, ref['period']['instant']), set()).add(Decimal(ref['raw_value']))
    return closing


def _cash_follower(tree, table, role, axes, scopes, closing, facts, html_ids):
    """Validate one follower table against the statement; raise if it does not fit exactly."""
    candidate = _segment_candidate(tree, table, 'cash_flow', facts, html_ids)
    if candidate is None or candidate['axes'] != axes or candidate['scopes'] != scopes:
        raise TableExtractionError('Cash flow follower periods or scope differ from the statement')
    for axis in axes:
        period, refs = json.loads(axis), candidate['references'][axis]
        if role == 'supplemental_disclosures':
            if candidate['coverage'][axis] or any(ref['period'] != period for ref in refs):
                raise TableExtractionError('Supplemental disclosures repeat statement totals or other periods')
            continue
        # A closing cash reconciliation: every fact is a cash balance at the
        # column's end date, the components add up to the total, and the total
        # equals the closing balance printed in the statement itself.
        if set(period) != {'startDate', 'endDate'} or candidate['coverage'][axis] != {3}:
            raise TableExtractionError('Cash reconciliation follower lacks a closing cash total')
        end, values = {'instant': period['endDate']}, {}
        for ref in refs:
            concept = ref['concept'].split('}', 1)[-1]
            if (ref['period'] != end or not _US_GAAP_CONCEPT.fullmatch(ref['concept'])
                    or concept not in _FOLLOWER_RECONCILIATION_CONCEPTS or concept in values):
                raise TableExtractionError('Cash reconciliation follower facts are not closing cash balances')
            values[concept] = None if ref['is_nil'] else Decimal(ref['raw_value'])
        total = values.pop(_FOLLOWER_CASH_TOTAL, None)
        if (total is None or values.get('CashAndCashEquivalentsAtCarryingValue') is None or len(values) < 2
                or sum(value or 0 for value in values.values()) != total
                or closing.get((_FOLLOWER_CASH_TOTAL, period['endDate'])) != {total}):
            raise TableExtractionError('Cash reconciliation follower does not reconcile to the closing balance')
    return candidate


def _with_cash_followers(tree, segments, facts):
    cash = [segment for segment in segments if segment.statement_type == 'cash_flow']
    if not cash or any((segment.group or {}).get(key) for segment in cash
                       for key in ('joint_statement', 'standard_cash_alias', 'joint_cash_continuation')):
        return segments
    tables = [table for table in tree.xpath('//table') if not _hidden(table) and not table.xpath('.//table')]
    if any(segment.element not in tables for segment in cash):
        return segments
    html_ids = {}
    for node in tree.iter():
        if local(node) == 'nonfraction' and node.get('id'):
            html_ids[node.get('id')] = html_ids.get(node.get('id'), 0) + 1
    group = cash[0].group
    if group is None:
        try:
            first = _segment_candidate(tree, cash[0].element, 'cash_flow', facts, html_ids)
        except TableExtractionError:
            return segments
        if first is None or len(first['scopes']) != 1 or not first['headings']:
            return segments
        axes, scopes, heading = first['axes'], first['scopes'], sorted(first['headings'])[0]
    else:
        axes = [json.dumps(period, sort_keys=True) for period in group['column_periods']]
        scopes, heading = {json.dumps(group['scope'], sort_keys=True)}, group['heading']
    closing = _statement_closing_cash(cash, facts)
    previous = max((segment.element for segment in cash), key=tables.index)
    followers = []
    for table in tables[tables.index(previous) + 1:]:
        if not any(local(node) == 'nonfraction' and not _hidden(node) for node in table.iter()):
            continue
        roles = [role for _, role in followers]
        if not followers and _closing_cash_reconciliation_title(table):
            role = 'closing_cash_reconciliation'
        elif 'supplemental_disclosures' not in roles and _standalone_cash_supplement(table):
            role = 'supplemental_disclosures'
        else:
            break
        try:
            if not _segments_adjacent(tree, previous, table, 'cash_flow'):
                break
            candidate = _cash_follower(tree, table, role, axes, scopes, closing, facts, html_ids)
        except TableExtractionError:
            break
        followers.append((candidate, role))
        previous = table
        if role == 'supplemental_disclosures':
            break
    if not followers:
        return segments
    if group is None:
        common = {'group_key': 'cash_flow', 'heading': heading, 'column_periods': [json.loads(axis) for axis in axes],
                  'scope': json.loads(next(iter(scopes)))}
        old = [StatementSegment('cash_flow', 'cash_flow.segment_1', cash[0].element, {'segment_index': 1})]
        roles = ['statement']
    else:
        common = {key: value for key, value in group.items()
                  if key not in {'segment_index', 'segment_role', 'header_inheritance'}}
        old = cash
        roles = list(common.get('segment_roles', ['statement'] * len(cash)))
    keys = [segment.table_key for segment in old] + [f'cash_flow.segment_{len(old) + index}'
                                                    for index in range(1, len(followers) + 1)]
    common.update(segment_count=len(keys), segment_keys=keys,
                  source_xpaths=[source_xpath(tree, segment.element) for segment in old]
                  + [source_xpath(tree, candidate['table']) for candidate, _ in followers],
                  segment_roles=roles + [role for _, role in followers])
    if 'segment_column_periods' in common:
        common['segment_column_periods'] = [*common['segment_column_periods'],
                                            *([json.loads(axis) for axis in axes] for _ in followers)]
    rebuilt = [StatementSegment('cash_flow', segment.table_key, segment.element,
                                common | {key: value for key, value in (segment.group or {}).items()
                                          if key in {'segment_index', 'header_inheritance'}} | {'segment_role': role})
               for segment, role in zip(old, roles)]
    for index, (candidate, role) in enumerate(followers, len(old) + 1):
        rebuilt.append(StatementSegment('cash_flow', keys[index - 1], candidate['table'],
                                        common | {'segment_index': index, 'segment_role': role}))
    return tuple(segment for segment in segments if segment.statement_type != 'cash_flow') + tuple(rebuilt)


def select_statement_segments(tree, facts, *, cash_followers=False, corroborated_layouts=False):
    """Resolve combined-filer identity before attempting ordinary selection.

    ``cash_followers`` (V8 only) adds the closing cash reconciliation and the
    supplemental disclosures printed directly after the cash flow statement.
    ``corroborated_layouts`` (tables-v3, and the second reading of V8) is
    described at `extract_tables`.

    The document is only read here, so the headings of each table and the
    visibility of each node are computed once for all attempts (`_memo`,
    `statement_headings.cached_visibility`). That changes no result.
    """
    from .statement_headings import cached_visibility
    global _SELECTION_MEMO
    outer = _SELECTION_MEMO
    if outer is None:
        _SELECTION_MEMO = {}
    try:
        with cached_visibility():
            return _select_statement_segments(tree, facts, cash_followers=cash_followers,
                                              corroborated_layouts=corroborated_layouts)
    finally:
        _SELECTION_MEMO = outer


def _printed_registrants(table, names):
    """The registrants named in a table's own header rows or in the lines printed directly above it.

    `names` maps each registrant's CIK to its DEI name. A line counts when
    the whole of it is that name, in any spelling `joint_statements._company`
    reads. The search ends at the first table or number above, two lines
    above the statement's title, or after twelve lines.
    """
    from .joint_statements import _company, _text_leaves
    from .statement_headings import statement_kind

    found, titled = set(), False

    def read(node):
        nonlocal titled
        for element in (node, *(_text_leaves(node) or ())):
            value = text(element)
            registrant = _company(value, names, spellings=True)
            if registrant:
                found.add(registrant)
            titled = titled or statement_kind(value, wide=True, header_lines=True) is not None

    for row in table.xpath('./tr|./thead/tr|./tbody/tr|./tfoot/tr')[:24]:
        if any(local(node) == 'nonfraction' for node in row.iter()):
            break
        for cell in row.xpath('./td|./th'):
            if text(cell):
                read(cell)
    node, blocks, beyond = table, 0, 0
    for _ in range(4):
        sibling = node.getprevious()
        while sibling is not None and blocks < 12 and beyond < 2:
            if text(sibling):
                if (local(sibling) == 'table' or sibling.xpath('.//table')
                        or any(local(child) == 'nonfraction' for child in sibling.iter())):
                    return found
                beyond += titled
                read(sibling)
                blocks += 1
            sibling = sibling.getprevious()
        node = node.getparent()
        if node is None or blocks >= 12 or beyond >= 2 or local(node) in {'body', 'html', 'td', 'th', 'table'}:
            break
    return found


def _parent_scope_segments(tree, facts, source_facts):
    """The reporting parent's statements in a combined filing, by the scope of their numbers, or None.

    tables-v3 corroborated layouts only, for a combined filing both joint
    readings reject. Those readings find the parent's statements by the
    registrant name printed with each title, and give up when a title prints
    no name they can read, when a statement runs over two pages, or when a row
    of the parent's own statement is tagged for another registrant (the share
    capital of a dual-listed company).

    The XBRL itself says which numbers are the parent's. Exactly one DEI
    `EntityCentralIndexKey` fact must carry no dimension and be the entity of
    its own context: the reporting parent. Its consolidated numbers carry no
    dimension, while each other registrant's are tagged with that
    registrant's member. The statements are then looked for outside the notes
    with tables of another scope left out (`outside_notes`), as in a filing
    of one registrant, and are accepted only if their core totals carry no
    dimension, belong to one entity (`_validate_primary_scope`) and that
    entity is the reporting parent. A table printed under the name of
    another registrant of the filing is never taken.
    """
    dei = r'\{' + DEI_NAMESPACE + r'\}'
    registrants = [record for record in source_facts.values()
                   if re.fullmatch(dei + 'EntityCentralIndexKey', record.get('concept') or '')
                   and isinstance(record.get('entity'), dict) and re.fullmatch(r'[0-9]{1,10}', (record.get('raw_value') or '').strip())]
    parents = {(record['entity'].get('scheme'), record['entity'].get('identifier')) for record in registrants
               if not record['dimensions']
               and (record['entity'].get('identifier') or '').zfill(10) == record['raw_value'].strip().zfill(10)}
    if len(parents) != 1:
        return None
    parent = next(iter(parents))
    names = {}
    for record in registrants:
        printed = {' '.join(other['raw_value'].split()) for other in source_facts.values()
                   if other['context_ref'] == record['context_ref'] and re.fullmatch(dei + 'EntityRegistrantName', other.get('concept') or '')}
        if len(printed) == 1:
            names[record['raw_value'].strip().zfill(10)] = next(iter(printed))
    parent_cik = (parent[1] or '').zfill(10)
    for concept_groups in (_CORROBORATED_STATEMENT_CONCEPTS, _UTILITY_STATEMENT_CONCEPTS):
        try:
            selected = _validate_primary_scope(
                _select_statement_segments_v2(tree, facts, concept_groups=concept_groups, corroborate=True, extended=True,
                                              outside_notes=True), source_facts)
        except TableExtractionError:
            continue
        for segment in selected:
            if _printed_registrants(segment.element, names) - {parent_cik}:
                return None
            concepts = set().union(*_STATEMENT_CONCEPTS[segment.statement_type])
            for element in segment.element.iter():
                reference = source_facts.get(element.get('id')) if local(element) == 'nonfraction' and not _hidden(element) else None
                if (reference is not None and reference['concept'].split('}', 1)[-1] in concepts
                        and (reference['entity'].get('scheme'), reference['entity'].get('identifier')) != parent):
                    return None
        return selected
    return None


def _select_statement_segments(tree, facts, *, cash_followers, corroborated_layouts):
    from .joint_statements import select_joint_statement_segments
    source_facts = _segment_facts(facts)
    try:
        selected = select_joint_statement_segments(tree, source_facts)
    except TableExtractionError:
        if not corroborated_layouts:
            raise
        # A combined filing the established reading rejects is read once
        # more; one it accepts keeps its exact segments and proof.
        try:
            selected = select_joint_statement_segments(tree, source_facts, header_lines=True)
        except TableExtractionError:
            selected = None
        if selected is None:
            # Last, the reporting parent's statements by the scope of their numbers.
            selected = _parent_scope_segments(tree, facts, source_facts)
            if selected is None:
                raise
    if selected is not None:
        return selected
    try:
        selected = _select_statement_segments_v2(tree, facts, corroborate=corroborated_layouts)
    except TableExtractionError:
        from .statement_aliases import select_alias_statement_segments
        selected = select_alias_statement_segments(tree, facts)
        if selected is None and corroborated_layouts:
            # Last, so a filing the two established selections accept keeps
            # its exact segments and proof.
            # Each later attempt reads more; the last two look outside the notes only.
            for concept_groups, extended, outside_notes in (
                    (_CORROBORATED_STATEMENT_CONCEPTS, False, False), (_CORROBORATED_STATEMENT_CONCEPTS, True, False),
                    (_UTILITY_STATEMENT_CONCEPTS, True, False), (_CORROBORATED_STATEMENT_CONCEPTS, True, True),
                    (_UTILITY_STATEMENT_CONCEPTS, True, True)):
                try:
                    selected = _select_statement_segments_v2(tree, facts, concept_groups=concept_groups, corroborate=True,
                                                             extended=extended, outside_notes=outside_notes)
                    break
                except TableExtractionError:
                    selected = None
        if selected is None:
            raise
    selected = _validate_primary_scope(selected, source_facts)
    return _with_cash_followers(tree, selected, source_facts) if cash_followers else selected


def validate_statement_segments(tables, tree, facts, *, xml_path=None, cash_followers=False, tagging_defects=False,
                                corroborated_layouts=False):
    """Independently reselect physical groups from unchanged source evidence."""
    expected = select_statement_segments(tree, facts, cash_followers=cash_followers,
                                         corroborated_layouts=corroborated_layouts)
    if len(tables) != len(expected):
        raise TableExtractionError('Stored statement segment count differs from the original sources')
    for table, segment in zip(tables, expected):
        if (table.extraction_version != 'tables-v2' or table.table_key != segment.table_key
                or table.statement_type != segment.statement_type
                or table.structure.get('source_xpath') != source_xpath(tree, segment.element)
                or table.structure.get('statement_group') != segment.group):
            raise TableExtractionError('Statement segment identity or group metadata differs from source evidence')
        inherited = (segment.group or {}).get('header_inheritance')
        if inherited:
            rows, _ = layout_grid(segment.element, tree)
            first = next(index for index, row in enumerate(rows) if any(local(node) == 'nonfraction'
                         for cell in row for node in cell['_element'].iter()))
            for row in table.structure['rows']:
                for cell in row:
                    descriptor = inherited['columns'].get(str(cell['column']))
                    if descriptor and (cell.get('column_period') != descriptor['period']
                                       or cell.get('column_headings') != column_headers(rows, first, cell['column'])):
                        raise TableExtractionError('Inherited dates or displayed headers differ from source evidence')
        joint = (segment.group or {}).get('joint_statement')
        if joint:
            from .joint_statements import validate_joint_cells
            validate_joint_cells(table, segment, tree, _segment_facts(facts))
        if (segment.group or {}).get('standard_cash_alias'):
            from .statement_aliases import validate_alias_cells
            validate_alias_cells(table, segment, tree, _segment_facts(facts))
    if xml_path is not None:
        from .cash_reconciliation import validate_cash_reconciliations
        validate_cash_reconciliations(tables, tree, xml_path, tagging_defects=tagging_defects,
                                      corroborated_layouts=corroborated_layouts)
    elif any(table.structure.get('cash_reconciliation_evidence') or any(
            cell.get('period_binding') for row in table.structure['rows'] for cell in row) for table in tables):
        raise TableExtractionError('Cash reconciliation validation requires its archived source XML')
    return True


def same_row_repeat_reference(cell, refs):
    """Return the unique same-row instant/value match used by extraction."""
    period = cell['column_period'] or {}
    end = period.get('instant') or period.get('endDate')
    semantic = {(f['concept'], json.dumps(f['unit'], sort_keys=True),
                 json.dumps(f['dimensions'], sort_keys=True)) for f in refs}
    candidates = {f['xml_id']: f for f in refs if f['period'] == {'instant': end}}
    if len(semantic) != 1 or len(candidates) != 1:
        return None
    reference = next(iter(candidates.values()))
    lexical = cell['display_text'].replace(',', '').strip()
    if lexical.startswith('(') and lexical.endswith(')'):
        lexical = '-' + lexical[1:-1].strip()
    try:
        displayed = Decimal(lexical)
    except InvalidOperation:
        return None
    if displayed * Decimal(10) ** int(reference['scale']) != Decimal(reference['raw_value']):
        return None
    return {**reference, 'method': 'same_row_semantic_repeat',
            'repeated_from_inline_xpath': reference['inline_xpath']}


def extract_tables(html_path, xml_path, *, filing_id: UUID, html_document_id: UUID,
                   xml_document_id: UUID, facts_version='facts-v1', tables_version=VERSION, cash_followers=False,
                   tagging_defects=False, corroborated_layouts=False):
    """Extract the selected primary statements.

    `corroborated_layouts` (tables-v3 always; V8 only when it reads a filing
    its own reading rejects a second time, see `filing_parser_v4._build`)
    accepts layouts that are
    otherwise left unresolved: a column whose header cannot be parsed alone
    but agrees with the one period all its tags carry (`_corroborate_columns`),
    a balance dated at an endpoint of a cash-flow column
    (`_endpoint_balance_period`), and header rows that give every tagged
    column its period only where the page displays them
    (`_displayed_header_rows`). It also reads row labels printed right of an
    empty first column (`_label_columns`), cells that only point to a note
    (`_note_reference`), a balance an income statement row dates at the
    period's end (`_labelled_period_end`), and the components of a cash
    balance that add up to the total printed below them
    (`_summed_cash_components`). It changes no cell that resolves without it.
    """
    if html_document_id == xml_document_id:
        raise TableExtractionError('HTML and XML must have distinct source IDs')
    tree = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
    ns = {k[6:]: v for k, v in tree.getroot().attrib.items() if k.startswith('xmlns:')}
    if tables_version not in {'tables-v1', 'tables-v2'}:
        raise TableExtractionError('Unsupported statement extraction version')
    advanced = tables_version == 'tables-v2'
    facts = xml_evidence(xml_path, include_entity=advanced, unidentified_cover_facts=corroborated_layouts)
    source_facts = facts
    if cash_followers and not advanced:
        raise TableExtractionError('Cash flow followers require the advanced statement selector')
    if advanced:
        selected = select_statement_segments(tree, facts, cash_followers=cash_followers,
                                             corroborated_layouts=corroborated_layouts)
        facts = {key: {name: value for name, value in record.items() if name != 'entity'} for key, record in facts.items()}
    else:
        selected = tuple(StatementSegment(kind, kind, table) for kind, table in select_statements(tree).items())
    html_ids = {}
    for element in tree.iter():
        if local(element) == 'nonfraction' and element.get('id'):
            html_ids[element.get('id')] = html_ids.get(element.get('id'), 0) + 1
    output = []
    for segment in selected:
        kind, table = segment.statement_type, segment.element
        reconciliation_segment = segment.group is not None and segment.group.get('segment_role') == 'cash_reconciliation'
        # V8 only: a reconciliation of the closing cash balance printed after
        # the statement, under the statement's own "... ended" headers.
        closing_reconciliation = (segment.group or {}).get('segment_role') == 'closing_cash_reconciliation'
        rows, grid = layout_grid(table, tree)
        first_data = next(i for i, row in enumerate(rows) if any(
            local(e) == 'nonfraction' for c in row for e in c['_element'].iter()))
        inherited_headers = (segment.group or {}).get('header_inheritance')
        joint_statement = (segment.group or {}).get('joint_statement')
        # Inherited and joint-statement headers establish their own periods
        # below; corroboration never adds evidence to those segments.
        column_periods, period_provenance = _column_periods(
            rows, first_data, facts, ns, html_ids, advanced=advanced, tagging_defects=tagging_defects,
            corroborate=corroborated_layouts and advanced and not inherited_headers and not joint_statement)
        # Columns whose periods were read from the header as displayed keep that
        # reading for the headings of their numbers; header cells themselves
        # keep the headings of their physical place.
        heading_rows = rows
        if any('displayed_headings' in evidence for evidence in period_provenance.values()):
            heading_rows = [*_displayed_header_rows(rows, first_data), *rows[first_data:]]
        if inherited_headers:
            column_periods.update({int(column): value['period'] for column, value in inherited_headers['columns'].items()})
        standard_cash_alias = (segment.group or {}).get('standard_cash_alias')
        if joint_statement:
            column_periods.update({int(column): period for column, period in joint_statement['column_periods'].items()})
        # tables-v3 corroborated layouts only: row labels printed right of an
        # empty first column, and the columns that hold a tagged number.
        label_columns = tagged_columns = frozenset()
        if corroborated_layouts and advanced:
            label_columns = _label_columns(rows, first_data, column_periods)
            tagged_columns = frozenset(cell['column'] for row in rows[first_data:] for cell in row
                                       if any(local(node) == 'nonfraction' for node in cell['_element'].iter()))
        # tables-v3 corroborated layouts only: body rows that restate the end
        # of their columns' periods, as above a reconciliation of closing cash.
        period_captions = (_restated_period_captions(rows, first_data, column_periods)
                           if corroborated_layouts and advanced else frozenset())
        # tables-v3 corroborated layouts only: balances a row prints under their
        # own month and year, left of the period columns, and those captions.
        dated_cells, dated_captions = (_dated_parentheticals(rows, first_data, column_periods, facts, html_ids)
                                       if corroborated_layouts and advanced else (frozenset(), frozenset()))
        # tables-v3 corroborated layouts only: components of a cash balance
        # proved by the total printed below them, and those a cell relied on.
        summed_cash = (_summed_cash_components(rows, first_data, facts, html_ids)
                       if corroborated_layouts and advanced and kind == 'cash_flow' else {})
        summed_used = {}
        undisplayed_nil = []
        cash_bindings, cash_reconciliations = {}, []
        if advanced and kind == 'cash_flow' and not reconciliation_segment and not closing_reconciliation:
            from .cash_reconciliation import bind_cash_reconciliations
            cash_bindings, cash_reconciliations = bind_cash_reconciliations(
                rows, column_periods, source_facts, ns, html_ids, xml_path)
        scope = {'filing_id': str(filing_id), 'xml_document_id': str(xml_document_id),
                 'facts_version': facts_version}
        supplemental_cash_disclosures = False
        reconciliation = None
        reconciliation_sections = []
        for ri, row in enumerate(rows):
            row_label = row[0]['display_text'] if row else ''
            if kind == 'cash_flow' and re.fullmatch(r'supplemental disclosures? of cash flow information\s*:?', row_label, re.I):
                supplemental_cash_disclosures = True
            if (kind == 'cash_flow' and row and row[0]['column'] == 0
                    and row[0]['colspan'] == len(grid[ri])
                    and re.search(r'\breconciliation of cash\b', row_label, re.I)
                    and re.search(r'\bconsolidated balance sheets\b', row_label, re.I)):
                start = ri + 1
                first = next((index for index in range(start, min(start + 8, len(rows)))
                              if any(local(element) == 'nonfraction' for cell in rows[index]
                                     for element in cell['_element'].iter())), None)
                if first is not None:
                    section_rows = rows[start:]
                    periods, _ = _column_periods(section_rows, first - start, facts, ns, html_ids, advanced=advanced)
                    known = [period for period in periods.values() if period is not None]
                    if known and all(set(period) == {'instant'} for period in known):
                        reconciliation = {'start': start, 'first': first, 'periods': periods}
                        reconciliation_sections.append({'header_start_row': start, 'first_data_row': first,
                                                        'column_periods': {str(column): period for column, period in periods.items()}})
            elif (corroborated_layouts and advanced and kind == 'cash_flow' and reconciliation is None and ri >= first_data
                    and _balance_date_captions(row, period_captions)):
                # tables-v3 corroborated layouts only: a reconciliation of cash to
                # the balance sheets headed by its own balance dates ("June 30,
                # 2026", "December 31, 2025") without the title above them. The
                # rows below are read against those dates exactly as in a titled
                # section: only cash balances of that date are accepted there.
                first = next((index for index in range(ri + 1, min(ri + 8, len(rows)))
                              if any(local(element) == 'nonfraction' for cell in rows[index]
                                     for element in cell['_element'].iter())), None)
                if first is not None:
                    periods, _ = _column_periods(rows[ri:], first - ri, facts, ns, html_ids, advanced=advanced)
                    known = [period for period in periods.values() if period is not None]
                    if known and all(set(period) == {'instant'} for period in known):
                        reconciliation = {'start': ri, 'first': first, 'periods': periods}
                        reconciliation_sections.append({'header_start_row': ri, 'first_data_row': first,
                                                        'column_periods': {str(column): period for column, period in periods.items()}})
            row_refs = []
            for cell in row:
                element = cell['_element']
                inline = [e for e in element.iter() if local(e) == 'nonfraction']
                cell['row_heading'] = row_label
                cell['column_headings'] = column_headers(heading_rows if ri >= first_data else rows, first_data,
                                                         cell['column'], advanced=advanced)
                cell['column_period'] = column_periods.get(cell['column'], header_period(cell['column_headings'], advanced=advanced))
                in_reconciliation = reconciliation is not None and ri >= reconciliation['start']
                if in_reconciliation:
                    cell['column_headings'] = column_headers(rows[reconciliation['start']:],
                        reconciliation['first'] - reconciliation['start'], cell['column'], advanced=advanced)
                    cell['column_period'] = reconciliation['periods'].get(cell['column'])
                if (corroborated_layouts and inline and cell['column_period'] is None and cell['colspan'] > 1
                        and cell['column'] > 0 and cell['column'] not in label_columns and cell['cell_id'] not in dated_cells):
                    # tables-v3 corroborated layouts only: a tagged number printed
                    # across grid columns, whose first column has no period of its
                    # own, stands under the period of the columns it spans when
                    # they give exactly one. Its tag must still carry that period.
                    # A spanned column no body cell starts in has only its header.
                    known = reconciliation['periods'] if in_reconciliation else column_periods
                    spanned = set()
                    for column in range(cell['column'], cell['column'] + cell['colspan']):
                        if column in known:
                            period = known[column]
                        elif in_reconciliation:
                            period = header_period(column_headers(
                                rows[reconciliation['start']:], reconciliation['first'] - reconciliation['start'], column,
                                advanced=advanced), advanced=advanced)
                        else:
                            period = header_period(column_headers(heading_rows if ri >= first_data else rows, first_data,
                                                                  column, advanced=advanced), advanced=advanced)
                        if period is not None:
                            spanned.add(json.dumps(period, sort_keys=True))
                    if len(spanned) == 1:
                        cell['column_period'] = json.loads(next(iter(spanned)))
                cell['fact_references'] = []
                cell['unresolved_reason'] = None
                if cell['cell_id'] in cash_bindings:
                    cell['period_binding'] = cash_bindings[cell['cell_id']]
                value = cell['display_text']
                is_header = ri < first_data or (in_reconciliation and ri < reconciliation['first'])
                cell['classification'] = ('blank' if not value else 'header' if is_header
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
                                'method': 'inline_id', 'inline_xpath': inline_source_xpath(tree, f),
                                'scale': f.get('scale', '0'), 'inline_sign': f.get('sign'),
                                'format': f.get('format')})
                        if cell['column'] > 0 and cell['column'] not in label_columns and cell['cell_id'] not in dated_cells:
                            header = cell['column_period']
                            if header is None:
                                raise TableExtractionError('Unresolved column period')
                            for ref in cell['fact_references']:
                                wanted = header
                                if kind == 'cash_flow' and 'instant' in ref['period']:
                                    label = row_label.lower()
                                    joint_endpoint = None
                                    if joint_statement:
                                        from .joint_statement_periods import cash_alias_endpoint_period
                                        joint_endpoint = cash_alias_endpoint_period(ref['concept'], row_label, header)
                                    if closing_reconciliation:
                                        if ('endDate' not in header or ref['concept'].split('}', 1)[-1]
                                                not in _FOLLOWER_RECONCILIATION_CONCEPTS):
                                            raise TableExtractionError('Unexpected instant in cash-flow row')
                                        wanted = {'instant': header['endDate']}
                                    elif cell['cell_id'] in cash_bindings:
                                        wanted = cash_bindings[cell['cell_id']]['period']
                                    elif standard_cash_alias and ref['xml_id'] in standard_cash_alias['endpoint_periods']:
                                        wanted = standard_cash_alias['endpoint_periods'][ref['xml_id']]
                                    elif joint_endpoint is not None:
                                        wanted = joint_endpoint
                                    elif joint_statement and reconciliation_segment and set(header) == {'instant'}:
                                        wanted = header
                                    elif (joint_statement and supplemental_cash_disclosures and 'endDate' in header
                                            and (segment.group or {}).get('joint_cash_continuation', {}).get('segment_role') == 'statement'
                                            and re.fullmatch(r'(?:preferred|common) dividends declared but not paid', label)
                                            and re.fullmatch(r'\{https?://fasb\.org/us-gaap/[^}]+\}DividendsPayableCurrentAndNoncurrent', ref['concept'])):
                                        wanted = {'instant': header['endDate']}
                                    elif (reconciliation_segment and set(header) == {'instant'}
                                            and ref['concept'].split('}', 1)[-1] in _RECONCILIATION_CASH_CONCEPTS):
                                        wanted = header
                                    elif (in_reconciliation and set(header) == {'instant'}
                                            and re.fullmatch(r'\{https?://fasb\.org/us-gaap/[^}]+\}(?:CashAndCashEquivalentsAtCarryingValue|RestrictedCashCurrent|RestrictedCashAndCashEquivalentsNoncurrent|CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents)', ref['concept'])):
                                        wanted = header
                                    elif re.search(r'\b(?:(?:beginning|start) of (?:the )?(?:fiscal )?(?:period|year|quarter)|(?:beginning|opening) balances?)\b', label) and 'startDate' in header:
                                        wanted = {'instant': (date.fromisoformat(header['startDate'])
                                                              - timedelta(days=1)).isoformat()}
                                    elif re.search(r'\b(?:end of (?:the )?(?:fiscal )?(?:period|year|quarter)|(?:ending|closing) balances?)\b', label) and 'endDate' in header:
                                        wanted = {'instant': header['endDate']}
                                    elif _named_cash_endpoint(ref['concept'], row_label, header) is not None:
                                        wanted = _named_cash_endpoint(ref['concept'], row_label, header)
                                    elif (supplemental_cash_disclosures and 'endDate' in header
                                          and re.fullmatch(r'dividends declared and not paid', label)
                                          and re.fullmatch(r'\{https?://fasb\.org/us-gaap/[^}]+\}DividendsPayableCurrentAndNoncurrent', ref['concept'])):
                                        wanted = {'instant': header['endDate']}
                                    elif corroborated_layouts and _endpoint_balance_period(
                                            ref, ' '.join(other['display_text'] for other in row
                                                          if other['column'] < cell['column']), header) is not None:
                                        wanted = dict(ref['period'])
                                    elif (ref['xml_id'] in summed_cash and ref['period'] == {
                                            'instant': header.get('endDate', header.get('instant'))}):
                                        wanted = dict(ref['period'])
                                        summed_used.setdefault(summed_cash[ref['xml_id']], []).append(ref['xml_id'])
                                    elif (corroborated_layouts and in_reconciliation and set(header) == {'instant'}
                                            and ref['period'] == header and 'Cash' in ref['concept'].rsplit('}', 1)[-1]):
                                        # tables-v3 corroborated layouts only, and only where every
                                        # reading above refused: any cash balance, as in
                                        # _endpoint_balance_period, here under the date its section prints.
                                        wanted = header
                                    else:
                                        raise TableExtractionError('Unexpected instant in cash-flow row')
                                elif (corroborated_layouts and kind == 'income_statement' and 'instant' in ref['period']
                                      and _labelled_period_end(row_label, header)):
                                    wanted = {'instant': header['endDate']}
                                if ref['period'] != wanted:
                                    raise TableExtractionError('Fact period differs from column heading')
                        cell['classification'] = ('resolved_label' if cell['column'] == 0 or cell['column'] in label_columns
                                                  or cell['cell_id'] in dated_cells else 'resolved')
                        row_refs.extend(cell['fact_references'])
                    except TableExtractionError as exc:
                        if (corroborated_layouts and not value.strip(_PRINTED) and cell['fact_references']
                                and all(ref['is_nil'] for ref in cell['fact_references'])
                                and str(exc) in ('Fact period differs from column heading', 'Unresolved column period')):
                            # tables-v3 corroborated layouts only: an empty cell that
                            # holds nothing but nil tags prints no number, so no number
                            # can stand under the wrong date. The cell is blank and is
                            # not linked; the nil facts stay facts of the filing.
                            undisplayed_nil.append({'cell_id': cell['cell_id'], 'column_period': cell['column_period'],
                                                    'xml_ids': [ref['xml_id'] for ref in cell['fact_references']]})
                            cell['fact_references'] = []
                        else:
                            cell['fact_references'] = []
                            cell['classification'], cell['unresolved_reason'] = 'unresolved', str(exc)
                elif not is_header and cell['column'] > 0 and (re.search(r'[0-9]', value)
                        or re.fullmatch(r'[-–—]', value)):
                    if any(re.fullmatch(r'notereferencenumbers?', re.sub(r'\s+', '', heading), re.I)
                           for heading in cell['column_headings']):
                        cell['classification'] = 'label'
                    elif cell['column'] in label_columns or (corroborated_layouts and _note_reference(cell, tagged_columns)):
                        # A row label that prints a digit ("... (Note 12)"), or a
                        # reference to a note. A label without a digit keeps the
                        # classification it always had in such a column.
                        cell['classification'] = 'label'
                    elif cell['cell_id'] in period_captions:
                        # A caption inside the body that restates its columns' period end.
                        cell['classification'] = 'header'
                    elif cell['cell_id'] in dated_captions:
                        # The month and year a row prints before a balance of that date.
                        cell['classification'] = 'label'
                    elif corroborated_layouts and _spacer_zero(element):
                        # A "0" of one point that only keeps a ruled row open.
                        cell['classification'] = 'formatting'
                    else:
                        cell['classification'], cell['unresolved_reason'] = 'unresolved', 'untagged_financial_cell'
                cell['is_total'] = row_label.lower().startswith(('total ', 'net '))
        # The supplied cash flow repeats an untagged end cash value. Resolve only
        # from uniquely matching SAME ROW concept/unit/dimensions and instant.
        for row in rows:
            refs = [f for c in row for f in c['fact_references']]
            for cell in row:
                if cell['unresolved_reason'] != 'untagged_financial_cell':
                    continue
                reference = same_row_repeat_reference(cell, refs)
                if reference is not None:
                    cell['fact_references'] = [reference]
                    cell['classification'], cell['unresolved_reason'] = 'resolved', None
        for row in rows:
            for c in row:
                del c['_element']
        counts = {}
        for row in rows:
            for cell in row:
                cls = cell['classification']
                counts[cls] = counts.get(cls, 0) + 1
        structure = {'schema_version': 1, 'table_key': segment.table_key, 'source_xpath': source_xpath(tree, table),
                     'heading_context': heading_before(table), 'fact_scope': scope,
                     'html_sha256': hashlib.sha256(Path(html_path).read_bytes()).hexdigest(),
                     'xml_sha256': hashlib.sha256(Path(xml_path).read_bytes()).hexdigest(),
                     'scale_qualifiers': [c['display_text'] for row in rows[:first_data] for c in row
                                          if 'millions' in c['display_text'].lower()],
                     'rows': rows, 'grid': grid, 'inventory': counts,
                     'following_text': text(table.getnext()) if table.getnext() is not None else ''}
        if period_provenance:
            structure['column_period_evidence'] = period_provenance
        if cash_reconciliations:
            structure['cash_reconciliation_evidence'] = cash_reconciliations
        if reconciliation_sections:
            structure['cash_reconciliation_sections'] = reconciliation_sections
        if undisplayed_nil:
            structure['undisplayed_nil_tags'] = undisplayed_nil
        if summed_used:
            structure['cash_component_sums'] = [
                {'verification_version': 'cash-component-sum-v1', 'total_xml_id': total, 'component_xml_ids': sorted(identities)}
                for total, identities in sorted(summed_used.items())]
        if segment.group is not None:
            structure['statement_group'] = segment.group
        # Keep physical columns, parentheses, dollars and empty cells in TSV.
        anchors = {c['cell_id']: c for row in rows for c in row}
        lines = []
        for ri, slots in enumerate(grid):
            values = [anchors[s]['display_text'] if s and anchors[s]['row'] == ri
                      and anchors[s]['column'] == ci else '' for ci, s in enumerate(slots)]
            lines.append('\t'.join(values))
        value_columns = sorted({c['column'] for row in rows for c in row
                                if c['classification'] in ('resolved', 'unresolved') and c['column'] > 0})
        column_groups = (_presentation_column_groups(heading_rows, first_data, value_columns, column_periods)
                         if advanced else [[column] for column in value_columns])
        structure['presentation_columns'] = [
            {'column': columns[0], 'headings': column_headers(heading_rows, first_data, columns[0], advanced=advanced),
             'period': column_periods[columns[0]], **({'source_columns': columns} if len(columns) > 1 else {})}
            for columns in column_groups]
        structure['presentation_rows'] = []
        last_period_heading = max((c['row'] for row in rows[:first_data] for c in row
            if c['column'] > 0 and re.search(r'20\d\d', c['display_text'])), default=first_data - 1)
        for row in rows[last_period_heading + 1:]:
            if not any(c['display_text'] for c in row):
                continue
            values = []
            for columns in column_groups:
                if advanced:
                    values.append(_presentation_value(row, columns))
                else:
                    parts = [c['display_text'] for c in row if columns[0] - 1 <= c['column'] <= columns[0] + 1]
                    values.append(' '.join(p for p in parts if p))
            structure['presentation_rows'].append({'label': row[0]['display_text'] if row else '',
                'values': values, 'source_row': row[0]['row'] if row else None})
        output.append(FinancialTableInput(filing_id=filing_id, source_document_id=html_document_id,
            extraction_version=tables_version, table_key=segment.table_key, statement_type=kind,
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


def load_tables(conn, tables, scope: FactScope, *, bulk=False):
    """Caller owns whole-import transaction; savepoint prevents partial stage writes.

    Verify actual stored records, not just labels attached to a dictionary.
    """
    if conn.autocommit:
        raise TableExtractionError('Caller-managed transaction required')
    tables = tuple(tables)
    referenced_ids = {}
    expected = {'filing_id': str(scope.filing_id), 'xml_document_id': str(scope.xml_document_id),
                'facts_version': scope.extraction_version}
    for table in tables:
        # Validate every anchor and classification, not only the inventory count.
        # A stale zero-unresolved inventory must not promote unresolved links.
        try:
            relational_cells(UUID(int=0), table)
        except TableCellError as exc:
            raise TableExtractionError(str(exc)) from exc
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
                    referenced_ids[fid] = None
    stored_facts = {}
    if bulk and referenced_ids:
        # Financial statements use only a subset of the filing's facts. Do not
        # retrieve unrelated footnote/text-block payloads from the facts stage.
        rows = conn.execute('''SELECT id, filing_id, source_document_id, extraction_version,
            concept_namespace, concept_name, raw_value, unit, period_kind, period_start,
            period_end, instant_date, dimensions, is_nil, numeric_value, occurrence_key, source_reference
            FROM sec.financial_facts WHERE id = ANY(%s)''',
            (list(referenced_ids),)).fetchall()
        stored_facts = {row[0]: row[1:] for row in rows}
    for table in tables:
        for row in table.structure['rows']:
            for cell in row:
                for ref in cell['fact_references']:
                    fid = scope.xml_id_to_fact_id[ref['xml_id']]
                    record = stored_facts.get(fid) if bulk else conn.execute('''SELECT filing_id, source_document_id, extraction_version,
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
            if bulk:
                from .bulk_storage import store_many
                memberships = [{'filing_id': scope.filing_id, 'table_id': tid,
                                'fact_id': scope.xml_id_to_fact_id[xml_id]} for xml_id in sorted(refs)]
                store_many(conn, 'financial_table_facts', memberships, ('table_id', 'fact_id'), returning='fact_id')
                store_table_cells(conn, tid, table, scope.xml_id_to_fact_id, bulk=True)
            else:
                for xml_id in sorted(refs):
                    link_table_fact(conn, scope.filing_id, tid, scope.xml_id_to_fact_id[xml_id])
                store_table_cells(conn, tid, table, scope.xml_id_to_fact_id)
    return ids
