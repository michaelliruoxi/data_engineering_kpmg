"""Conservative primary statement extraction from inline-XBRL filings.

No HTTP requests or database writes occur during extraction. Layout and XML
values are separate: a displayed cash outflow may have a positive XBRL value.
"""
from __future__ import annotations

from dataclasses import dataclass
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


def xml_evidence(path, *, include_entity=False):
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
                    r'|statements? of comprehensive income(?: loss)?|(?:income|operations|earnings) statements?)', value):
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


def _statement_heading_values(table):
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
    parent_contexts = {context.get('id') for context in tree.iter() if local(context) == 'context'
                       and any(local(member) == 'explicitmember' and text(member).split(':')[-1] == 'ParentCompanyMember'
                               for member in context.iter())}
    for table in tree.xpath('//table'):
        if _hidden(table) or table.xpath('.//table'):
            continue
        kinds = _statement_headings(table)
        consolidated = _statement_headings(table, consolidated=True)
        continued.update(_statement_headings(table, continued=True))
        concepts = set()
        for element in table.iter():
            if (local(element) != 'nonfraction' or _hidden(element)
                    or element.get('contextref') in parent_contexts):
                continue
            name = element.get('name', '')
            prefix, sep, concept = name.partition(':')
            namespace = _inline_namespaces(element).get(prefix, '')
            if sep and re.fullmatch(r'https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^/]+', namespace):
                concepts.add(concept)
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
    return ' '.join(c['display_text'] for c in row if c is value or (
        c['classification'] == 'formatting' and c['display_text']
        and ((c['column'] == value['column'] - 1 and re.fullmatch(r'[$€£¥(+−–—-]+', c['display_text']))
             or (c['column'] == value['column'] + value['colspan']
                 and re.fullmatch(r'[)%]+', c['display_text'])))))


def header_period(headers, *, context_period=None, advanced=False):
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
        return values, sources
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
    return revised, sources


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


def _column_periods(rows, first_data, facts, ns, html_ids, *, advanced=False):
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
            continue
        candidate = dict(observed.pop())
        resolved = header_period(headers[column], context_period=candidate, advanced=advanced)
        source_headers, calendar_sources = [], []
        if advanced and resolved != candidate:
            revised, source_headers = _v2_period_headers(rows, first_data, column, headers[column])
            resolved = header_period(revised, context_period=candidate, advanced=True)
            if resolved != candidate:
                table = next(cell for row in rows for cell in row)['_element'].xpath('ancestor::table[1]')[0]
                corroborated, calendar_sources = _v2_calendar_period(revised, candidate, table)
                if corroborated is not None:
                    resolved = corroborated
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
    return periods, provenance


@dataclass(frozen=True)
class StatementSegment:
    statement_type: str
    table_key: str
    element: etree._Element
    group: dict | None = None


def _segment_facts(facts):
    records = facts.values() if isinstance(facts, Mapping) else facts
    result = {}
    for record in records:
        if isinstance(record, FactInput):
            reference = record.source_reference
            period = ({'instant': record.instant_date.isoformat()} if record.period_kind == 'instant'
                      else {'startDate': record.period_start.isoformat(), 'endDate': record.period_end.isoformat()}
                      if record.period_kind == 'duration' else {'forever': ''})
            value = {'xml_id': reference.get('element_id'),
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


def _segment_candidate(tree, table, kind, facts, html_ids, *, inherited_periods=None, heading_evidence=(), concept_groups=None):
    concept_groups = _STATEMENT_CONCEPTS if concept_groups is None else concept_groups
    rows, _ = layout_grid(table, tree)
    first = next((index for index, row in enumerate(rows) if any(
        local(element) == 'nonfraction' for cell in row for element in cell['_element'].iter())), None)
    if first is None:
        return None
    periods, _ = _column_periods(rows, first, facts, {}, html_ids, advanced=True)
    if inherited_periods:
        for column, period in inherited_periods.items():
            if periods.get(column) is not None:
                raise TableExtractionError('Inherited headers must not replace a displayed column period')
            periods[column] = period
    axes, coverage, evidence, scopes, references = [], {}, {}, set(), {}
    for row in rows[first:]:
        for cell in row:
            if cell['column'] == 0:
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
                    return None
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


_RECONCILIATION_CASH_CONCEPTS = {
    'CashAndCashEquivalentsAtCarryingValue', 'RestrictedCash', 'RestrictedCashCurrent',
    'RestrictedCashAndCashEquivalents', 'RestrictedCashAndCashEquivalentsNoncurrent',
    'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
}


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
    if (not re.search(r'\bnet (?:increase|decrease|change)\b', labels)
            or not re.search(r'\bcash and cash equivalents at beginning of (?:the )?period\b', labels)
            or not re.search(r'\bcash and cash equivalents at end of (?:the )?period\b', labels)):
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


def _select_statement_segments_v2(tree, facts, *, concept_groups=None):
    """Select original physical tables; v2 currently groups cash-flow continuations.

    Complete v1 selections are preserved. Split statements need matching exact
    heading identities, source period axes, entity/unit/dimension scopes and
    document adjacency. A group cannot contain two complete statements.
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
    for table in tree.xpath('//table'):
        if _hidden(table) or table.xpath('.//table'):
            continue
        kinds = _statement_headings(table) | _statement_headings(table, continued=True)
        heading_evidence = ()
        if not kinds:
            heading_evidence = recover_headings(table)
            kinds = {item.kind for item in heading_evidence}
        for kind in kinds:
            if kind == 'cash_flow' and _standalone_cash_supplement(table):
                continue
            try:
                candidate = _segment_candidate(tree, table, kind, facts, html_ids, heading_evidence=heading_evidence,
                                               concept_groups=concept_groups)
            except TableExtractionError as exc:
                if kind != 'cash_flow' or len(choices[kind]) != 1 or str(exc) != 'Statement segment has an unresolved column period':
                    raise
                candidate = _cash_named_header_continuation(tree, choices[kind][0], table, facts, html_ids, heading_evidence)
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
        required = set(range(len(concept_groups[kind])))
        complete = [candidate for candidate in candidates if candidate['axes']
                    and all(groups == required for groups in candidate['coverage'].values())]
        if kind == 'balance_sheet' and not complete:
            balance_segments = _balance_header_continuation(tree, candidates, facts, html_ids)
            if balance_segments is None:
                from .balance_segments import verified_balance_segments
                balance_segments = verified_balance_segments(tree, candidates, facts)
            if balance_segments is not None:
                output.extend(balance_segments)
                continue
        if kind == 'cash_flow' and not complete:
            closing_segments = _cash_closing_continuation(tree, candidates, facts, html_ids)
            if closing_segments is not None:
                output.extend(closing_segments)
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
            raise TableExtractionError(f'{kind}: ambiguous or incomplete physical statement segments')
        candidates = _include_cash_reconciliations(tree, candidates, facts, html_ids)
        first = candidates[0]
        if first['continued'] or not first['axes'] or len(first['scopes']) != 1:
            raise TableExtractionError('Cash-flow segments lack a unique initial statement scope')
        if any(set(json.loads(axis)) != {'startDate', 'endDate'} for axis in first['axes']):
            raise TableExtractionError('Cash-flow statement segments require duration column headers')
        headings = set(first['headings'])
        previous_groups = set()
        combined = {axis: set() for axis in first['axes']}
        combined_evidence = {axis: {} for axis in first['axes']}
        for index, candidate in enumerate(candidates):
            is_reconciliation = candidate.get('role') == 'cash_reconciliation'
            if not is_reconciliation:
                headings &= candidate['headings']
            wanted_axes = ([json.dumps({'instant': json.loads(axis)['endDate']}, sort_keys=True)
                            for axis in first['axes']] if is_reconciliation else first['axes'])
            if (candidate['axes'] != wanted_axes or candidate['scopes'] != first['scopes']
                    or (index and not _segments_adjacent(tree, candidates[index - 1]['table'], candidate['table'], kind,
                                                        numbered_footer=bool(candidate.get('inherited_columns'))))):
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
        has_reconciliation = any(candidate.get('role') == 'cash_reconciliation' for candidate in candidates)
        if has_reconciliation:
            common['segment_roles'] = [candidate.get('role', 'statement') for candidate in candidates]
            common['segment_column_periods'] = [[json.loads(axis) for axis in candidate['axes']] for candidate in candidates]
        inherited_candidates = [candidate for candidate in candidates if candidate.get('inherited_columns')]
        if inherited_candidates:
            if len(candidates) != 2 or len(inherited_candidates) != 1:
                raise TableExtractionError('Inherited cash headers require exactly two physical segments')
            common['closure_evidence'] = inherited_candidates[0]['closure_evidence']
        output.extend(StatementSegment(kind, key, candidate['table'], common | {'segment_index': index + 1}
                      | ({'segment_role': candidate.get('role', 'statement')} if has_reconciliation else {})
                      | ({'header_inheritance': {'source_table_key': keys[0], 'source_xpath': source_xpath(tree, first['table']),
                                                'columns': candidate['inherited_columns']}} if candidate.get('inherited_columns') else {}))
                      for index, (key, candidate) in enumerate(zip(keys, candidates)))
    if len({segment.element for segment in output}) != len(output):
        raise TableExtractionError('Different statements cannot reuse the same physical table')
    return tuple(output)


def select_statement_segments(tree, facts):
    """Preserve established selections; resolve proved joint filers on fallback."""
    try:
        return _select_statement_segments_v2(tree, facts)
    except TableExtractionError:
        from .joint_statements import select_joint_statement_segments
        selected = select_joint_statement_segments(tree, facts)
        if selected is None:
            from .statement_aliases import select_alias_statement_segments
            selected = select_alias_statement_segments(tree, facts)
            if selected is None:
                raise
        return selected


def validate_statement_segments(tables, tree, facts, *, xml_path=None):
    """Independently reselect physical groups from unchanged source evidence."""
    expected = select_statement_segments(tree, facts)
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
        validate_cash_reconciliations(tables, tree, xml_path)
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
                   xml_document_id: UUID, facts_version='facts-v1', tables_version=VERSION):
    if html_document_id == xml_document_id:
        raise TableExtractionError('HTML and XML must have distinct source IDs')
    tree = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
    ns = {k[6:]: v for k, v in tree.getroot().attrib.items() if k.startswith('xmlns:')}
    if tables_version not in {'tables-v1', 'tables-v2'}:
        raise TableExtractionError('Unsupported statement extraction version')
    advanced = tables_version == 'tables-v2'
    facts = xml_evidence(xml_path, include_entity=advanced)
    source_facts = facts
    if advanced:
        selected = select_statement_segments(tree, facts)
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
        rows, grid = layout_grid(table, tree)
        first_data = next(i for i, row in enumerate(rows) if any(
            local(e) == 'nonfraction' for c in row for e in c['_element'].iter()))
        column_periods, period_provenance = _column_periods(rows, first_data, facts, ns, html_ids, advanced=advanced)
        inherited_headers = (segment.group or {}).get('header_inheritance')
        if inherited_headers:
            column_periods.update({int(column): value['period'] for column, value in inherited_headers['columns'].items()})
        joint_statement = (segment.group or {}).get('joint_statement')
        standard_cash_alias = (segment.group or {}).get('standard_cash_alias')
        if joint_statement:
            column_periods.update({int(column): period for column, period in joint_statement['column_periods'].items()})
        cash_bindings, cash_reconciliations = {}, []
        if advanced and kind == 'cash_flow' and not reconciliation_segment:
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
            row_refs = []
            for cell in row:
                element = cell['_element']
                inline = [e for e in element.iter() if local(e) == 'nonfraction']
                cell['row_heading'] = row_label
                cell['column_headings'] = column_headers(rows, first_data, cell['column'], advanced=advanced)
                cell['column_period'] = column_periods.get(cell['column'], header_period(cell['column_headings'], advanced=advanced))
                in_reconciliation = reconciliation is not None and ri >= reconciliation['start']
                if in_reconciliation:
                    cell['column_headings'] = column_headers(rows[reconciliation['start']:],
                        reconciliation['first'] - reconciliation['start'], cell['column'], advanced=advanced)
                    cell['column_period'] = reconciliation['periods'].get(cell['column'])
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
                        if cell['column'] > 0:
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
                                    if cell['cell_id'] in cash_bindings:
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
                                    elif re.search(r'\b(?:(?:beginning|start) of (?:the )?(?:period|year|quarter)|(?:beginning|opening) balances?)\b', label) and 'startDate' in header:
                                        wanted = {'instant': (date.fromisoformat(header['startDate'])
                                                              - timedelta(days=1)).isoformat()}
                                    elif re.search(r'\b(?:end of (?:the )?(?:period|year|quarter)|(?:ending|closing) balances?)\b', label) and 'endDate' in header:
                                        wanted = {'instant': header['endDate']}
                                    elif (supplemental_cash_disclosures and 'endDate' in header
                                          and re.fullmatch(r'dividends declared and not paid', label)
                                          and re.fullmatch(r'\{https?://fasb\.org/us-gaap/[^}]+\}DividendsPayableCurrentAndNoncurrent', ref['concept'])):
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
                elif not is_header and cell['column'] > 0 and re.fullmatch(
                        r'\(?\s*[\d,]+(?:\.\d+)?\s*\)?|[-–—]', value):
                    if any(re.fullmatch(r'notereferencenumbers?', re.sub(r'\s+', '', heading), re.I)
                           for heading in cell['column_headings']):
                        cell['classification'] = 'label'
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
        column_groups = (_presentation_column_groups(rows, first_data, value_columns, column_periods)
                         if advanced else [[column] for column in value_columns])
        structure['presentation_columns'] = [
            {'column': columns[0], 'headings': column_headers(rows, first_data, columns[0], advanced=advanced),
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
