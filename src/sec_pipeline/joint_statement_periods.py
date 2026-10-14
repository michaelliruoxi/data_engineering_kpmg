"""Narrow source-period and standard-concept helpers for joint statements.

The caller establishes statement identity and a unique corroborated XML period.
These helpers do not select issuers, infer missing dates, or bind amounts.
"""

from collections.abc import Mapping
import calendar
from datetime import date, timedelta
import re


GAAP_REVENUE_ALIASES = frozenset({'RegulatedAndUnregulatedOperatingRevenue'})
GAAP_CASH_BALANCE_ALIASES = frozenset({
    'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations',
})
_GAAP = re.compile(r'\{https?://(?:fasb\.org|xbrl\.us)/us-gaap/(?:19|20)\d{2}\}([^}]+)')
_MONTHS = {name.lower(): month for month in range(1, 13)
           for name in (calendar.month_name[month], calendar.month_abbr[month])}
_MONTHS['sept'] = 9
_MONTH_PATTERN = '|'.join(sorted(_MONTHS, key=len, reverse=True))
_MONTH_DAY = re.compile(r'\b(' + _MONTH_PATTERN + r')\.?\s+(\d{1,2})(?:\s*,?\s+((?:19|20)\d{2}))?\b')


def _text(values):
    value = ' '.join(values)
    value = re.sub(r'([a-z])([A-Z])', r'\1 \2', value)
    value = re.sub(r'([A-Za-z])(?=\d)', r'\1 ', value)
    return ' '.join(value.replace('\u200b', '').replace('\ufeff', '').lower().split())


def _duration(period):
    if not isinstance(period, Mapping) or set(period) != {'startDate', 'endDate'}:
        return None
    try:
        start, end = date.fromisoformat(period['startDate']), date.fromisoformat(period['endDate'])
    except (ValueError, TypeError):
        return None
    if start >= end or start == date.min:
        return None
    return start, end


def resolve_ytd_period(headers, context_period):
    """Confirm a calendar YTD caption against its actual source duration.

    Calendar-year starts are supported here. A fiscal YTD with another start
    needs additional explicit calendar evidence, outside this helper's scope.
    The input must already represent one column, including any physically
    corroborated shared caption; whole comparative captions remain ambiguous.
    """
    if not isinstance(headers, (list, tuple)) or any(not isinstance(value, str) for value in headers):
        return None
    period = _duration(context_period)
    if period is None:
        return None
    value = _text(headers)
    if not re.search(r'\byear[\s-]+to[\s-]+date\b', value):
        return None
    residual = re.sub(r'\byear[\s-]+to[\s-]+date\b', '', value)
    if re.search(r'\b(?:quarter|quarters|months?|weeks?|fiscal|years?)\b', residual):
        return None
    years = {int(year) for year in re.findall(r'\b(?:19|20)\d{2}\b', value)}
    matches = list(_MONTH_DAY.finditer(value))
    dates = {(_MONTHS[match[1]], int(match[2])) for match in matches}
    if len(years) != 1 or len(dates) != 1:
        return None
    # Numeric or ISO dates mixed with a named month are not silently ignored.
    if re.search(r'\b\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b', residual):
        return None
    year, (month, day) = next(iter(years)), next(iter(dates))
    try:
        displayed_end = date(year, month, day)
    except ValueError:
        return None
    start, end = period
    if start != date(year, 1, 1) or end != displayed_end:
        return None
    return dict(context_period)


def additional_gaap_group(concept):
    """Identify only explicitly supported standard-taxonomy aliases."""
    match = _GAAP.fullmatch(concept) if isinstance(concept, str) else None
    if match is None:
        return None
    if match[1] in GAAP_REVENUE_ALIASES:
        return 'income_statement', 0
    if match[1] in GAAP_CASH_BALANCE_ALIASES:
        return 'cash_flow', 3
    return None


def cash_alias_endpoint_period(concept, row_heading, column_period):
    """Return the period required by a supported opening/closing cash label.

    The caller must still compare this with the same occurrence's XML period.
    An explicit opening-date label denotes the beginning of that day, matching
    the XBRL balance at the end of the immediately preceding day.
    """
    if additional_gaap_group(concept) != ('cash_flow', 3) or not isinstance(row_heading, str):
        return None
    period = _duration(column_period)
    if period is None:
        return None
    value = re.sub(r'\s*\([1-9][0-9]?\)\s*$', '', _text([row_heading]))
    value = re.sub(r'[,;:]', ' ', value)
    value = ' '.join(value.split())
    match = re.fullmatch(r'cash (?:and )?cash equivalents (?:and )?restricted cash'
                         r'(?: and restricted cash equivalents)?(?: at)? (.+)', value)
    if match is None:
        return None
    label = match[1]
    start, end = period
    if re.fullmatch(r'(?:beginning|start) of (?:the )?(?:period|year|quarter)', label):
        return {'instant': (start - timedelta(days=1)).isoformat()}
    if re.fullmatch(r'end of (?:the )?(?:period|year|quarter)', label):
        return {'instant': end.isoformat()}
    named_date = _MONTH_DAY.fullmatch(label)
    if named_date is None:
        return None
    month, day = _MONTHS[named_date[1]], int(named_date[2])
    stated_year = int(named_date[3]) if named_date[3] else None
    candidates = []
    if (month, day) == (start.month, start.day) and stated_year in (None, start.year):
        candidates.append((start - timedelta(days=1)).isoformat())
    if (month, day) == (end.month, end.day) and stated_year in (None, end.year):
        candidates.append(end.isoformat())
    return {'instant': candidates[0]} if len(candidates) == 1 else None
