"""Explicit, bounded Inline XBRL registry transformations used by source sets.

References: XBRL Transformation Registry 4/5 and EDGAR XBRL Guide section11.12.
Inputs outside the supported grammar fail rather than borrowing an XML value.
"""

import calendar
from datetime import date
from decimal import Decimal, localcontext
import re

from .fact_extraction import _small_english_number


XII = 'http://www.xbrl.org/inlineXBRL/transformation/'
MODERN = {XII + '2020-02-12', XII + '2022-02-16'}
OLDER = {XII + '2011-07-31', XII + '2015-02-26'}
ORIGINAL = XII + '2010-04-20'
SEC = {'http://www.sec.gov/inlineXBRL/transformation/2015-08-31',
       'https://www.sec.gov/inlineXBRL/transformation/2015-08-31'}
MONTHS = {name.lower(): month for month in range(1, 13)
          for name in (calendar.month_name[month], calendar.month_abbr[month])}
MONTHS['sept'] = 9

# EDGAR XBRL Guide section 11.12 maps state/province names to postal codes,
# unlike edgarprovcountryen (Ontario is ON here, not A6).
# https://www.sec.gov/files/edgar/filer-information/specifications/xbrl-guide-2026-08-14.pdf
# https://www.canadapost-postescanada.ca/cpc/en/support/articles/addressing-guidelines/symbols-and-abbreviations.page
STATE_PROVINCE_CODES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR', 'california': 'CA',
    'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE', 'district of columbia': 'DC',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL',
    'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA',
    'maine': 'ME', 'maryland': 'MD', 'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN',
    'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK', 'oregon': 'OR',
    'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC', 'south dakota': 'SD',
    'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA',
    'washington': 'WA', 'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
    'alberta': 'AB', 'british columbia': 'BC', 'manitoba': 'MB', 'new brunswick': 'NB',
    'newfoundland and labrador': 'NL', 'northwest territories': 'NT', 'nova scotia': 'NS',
    'nunavut': 'NU', 'ontario': 'ON', 'prince edward island': 'PE', 'quebec': 'QC',
    'saskatchewan': 'SK', 'yukon': 'YT',
}


class UnsupportedInlineTransform(ValueError):
    """The original inline value cannot be transformed under a supported rule."""


def _format(value):
    match = re.fullmatch(r'\{([^}]+)\}([^{}]+)', value or '')
    if match is None:
        raise UnsupportedInlineTransform('Missing or invalid expanded transformation name.')
    return match[1], match[2]


def validate_transform(format_name, kind):
    """Reject unknown transformations even when a nil occurrence has no value."""
    namespace, name = _format(format_name)
    numeric = ((namespace in MODERN and name in {'fixed-zero', 'num-dot-decimal'})
               or (namespace in OLDER and name in {'numdotdecimal', 'zerodash'})
               or (namespace == ORIGINAL and name == 'numdash')
               or (namespace in SEC and name == 'numwordsen'))
    nonnumeric = ((namespace in MODERN and name in {'fixed-true', 'fixed-false', 'fixed-empty',
                  'date-monthname-day-year-en', 'date-monthname-day-en', 'date-month-day-year', 'date-month-day'})
                  or (namespace in SEC and name in {'duryear', 'durmonth', 'durday', 'durwordsen',
                      'entityfilercategoryen', 'stateprovnameen', 'exchnameen', 'boolballotbox', 'yesnoballotbox'}))
    if not ((kind == 'numeric' and numeric) or (kind == 'nonnumeric' and nonnumeric)):
        raise UnsupportedInlineTransform('Unsupported registry transformation for the original inline kind: ' + format_name)


def validate_numeric_attributes(*, scale=None, sign=None):
    value = '0' if scale is None else scale
    if sign not in (None, '-') or not re.fullmatch(r'[+-]?[0-9]+', value):
        raise UnsupportedInlineTransform('Invalid inline numeric sign or scale.')
    if len(value) > 8 or abs(int(value)) > 100000:
        raise UnsupportedInlineTransform('Numeric scale exceeds the supported exact transformation bound.')
    return int(value)


def numeric_value(text, format_name=None, *, scale=None, sign=None):
    power = validate_numeric_attributes(scale=scale, sign=sign)
    if len(text) > 100000:
        raise UnsupportedInlineTransform('Numeric value exceeds the supported exact transformation bound.')
    shown = text.strip(' \t\r\n\u00a0')
    if format_name is None:
        if not re.fullmatch(r'\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)', shown):
            raise UnsupportedInlineTransform('Untransformed numeric content is not a nonnegative decimal.')
    else:
        namespace, name = _format(format_name)
        if namespace in MODERN and name == 'fixed-zero':
            shown = '0'
        elif namespace in SEC and name == 'numwordsen':
            shown = _small_english_number(shown)
            if shown is None:
                raise UnsupportedInlineTransform('Unsupported numeric-word phrase.')
        elif (namespace in MODERN and name == 'num-dot-decimal') or (namespace in OLDER and name == 'numdotdecimal'):
            if not re.fullmatch(r'(?:[0-9]+|[0-9]{1,3}(?:[,\s\u00a0][0-9]{3})+)(?:\.[0-9]*)?|\.[0-9]+', shown):
                raise UnsupportedInlineTransform('Invalid grouped dot-decimal input.')
            shown = re.sub(r'[,\s\u00a0]', '', shown)
        elif ((namespace in OLDER and name == 'zerodash') or (namespace == ORIGINAL and name == 'numdash')) and shown == '-':
            shown = '0'
        else:
            raise UnsupportedInlineTransform('Unsupported numeric registry transformation: ' + format_name)
    _, digits, exponent = Decimal(shown).as_tuple()
    return Decimal((int(sign == '-'), digits, exponent + power))


def _date_value(value, name):
    words = '|'.join(sorted(MONTHS, key=len, reverse=True))
    recurring = name in {'date-monthname-day-en', 'date-month-day'}
    named = name in {'date-monthname-day-en', 'date-monthname-day-year-en'}
    month = '(' + words + r')\.?' if named else r'([0-9]{1,2})'
    expression = month + r'[^0-9]+([0-9]{1,2})(?:st|nd|rd|th)?'
    if not recurring:
        expression += r'[^0-9]+([0-9]{1,2}|[0-9]{4})'
    match = re.fullmatch(expression, value, re.I)
    if match is None:
        raise UnsupportedInlineTransform('Unsupported date transformation input.')
    year = 2000 if recurring else int(match[3]) + (2000 if len(match[3]) < 4 else 0)
    try:
        result = date(year, MONTHS[match[1].lower()] if named else int(match[1]), int(match[2]))
    except ValueError as exc:
        raise UnsupportedInlineTransform('Inline date is not a calendar date.') from exc
    return result.strftime('--%m-%d') if recurring else result.isoformat()


def _duration_number(value, unit):
    if not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', value) or len(value) > 200:
        raise UnsupportedInlineTransform('Unsupported numeric duration input.')
    number = Decimal(value)
    negative, number = number < 0, number.copy_abs()
    whole = int(number)
    parts = []
    with localcontext() as context:
        context.prec = max(32, len(number.as_tuple().digits) + 10)
        fraction = number - whole
        if unit == 'year':
            months = fraction * 12
            days = (months - int(months)) * 30
            if days != int(days):
                raise UnsupportedInlineTransform('Fractional-day duration rounding is outside this supported grammar.')
            parts = [(whole, 'Y'), (int(months), 'M'), (int(days), 'D')]
        elif unit == 'month':
            days = fraction * 30
            if days != int(days):
                raise UnsupportedInlineTransform('Fractional-day duration rounding is outside this supported grammar.')
            parts = [(whole, 'M'), (int(days), 'D')]
        elif unit == 'day' and not fraction:
            parts = [(whole, 'D')]
        else:
            raise UnsupportedInlineTransform('Unsupported duration precision.')
    suffix = ''.join(str(count) + code for count, code in parts if count) or '0D'
    return ('-' if negative and number else '') + 'P' + suffix


def _duration_words(value):
    values = []
    remaining = value.strip()
    expression = r'([a-z0-9 -]+?)[ -]+(years?|months?|days?)\b'
    while remaining:
        match = re.match(expression, remaining, re.I)
        if match is None:
            raise UnsupportedInlineTransform('Unsupported duration-word phrase.')
        coefficient = match[1].strip()
        number = coefficient if re.fullmatch(r'[0-9]+', coefficient) else _small_english_number(coefficient)
        if number is None:
            raise UnsupportedInlineTransform('Unsupported duration-word coefficient.')
        unit = match[2][0].upper()
        if values and 'YMD'.index(unit) <= 'YMD'.index(values[-1][1]):
            raise UnsupportedInlineTransform('Duplicate or unordered duration-word components.')
        values.append((int(number), unit))
        remaining = re.sub(r'^\s*(?:,\s*|and\s+)?', '', remaining[match.end():])
    return 'P' + (''.join(str(number) + unit for number, unit in values if number) or '0D')


def nonnumeric_value(text, format_name=None):
    if format_name is None:
        return text
    namespace, name = _format(format_name)
    value = ' '.join(text.split())
    if namespace in MODERN:
        if name in {'fixed-true', 'fixed-false', 'fixed-empty'}:
            return {'fixed-true': 'true', 'fixed-false': 'false', 'fixed-empty': ''}[name]
        if name in {'date-monthname-day-year-en', 'date-monthname-day-en', 'date-month-day-year', 'date-month-day'}:
            return _date_value(value, name)
    if namespace in SEC:
        lowered = value.lower()
        if name in {'duryear', 'durmonth', 'durday'}:
            return _duration_number(value, name[3:])
        if name == 'durwordsen':
            return _duration_words(lowered)
        if name == 'entityfilercategoryen':
            categories = {'large accelerated filer': 'Large Accelerated Filer', 'accelerated filer': 'Accelerated Filer',
                          'non-accelerated filer': 'Non-accelerated Filer', 'non accelerated filer': 'Non-accelerated Filer'}
            if lowered in categories:
                return categories[lowered]
        if name == 'stateprovnameen':
            if lowered in STATE_PROVINCE_CODES:
                return STATE_PROVINCE_CODES[lowered]
        if name == 'exchnameen':
            exchange = re.sub(r'^the\s+|[,.]', '', lowered)
            exchange = re.sub(r'\s+(?:llc|incorporated|inc)$', '', exchange)
            exchanges = {'new york stock exchange': 'NYSE', 'nasdaq global select market': 'NASDAQ',
                         'nasdaq stock market': 'NASDAQ', 'nasdaq global market': 'NASDAQ',
                         'nasdaq capital market': 'NASDAQ'}
            if exchange in exchanges:
                return exchanges[exchange]
        if name in {'boolballotbox', 'yesnoballotbox'} and value in {'☐', '☑', '☒'}:
            return ('false' if value == '☐' else 'true') if name == 'boolballotbox' else ('No' if value == '☐' else 'Yes')
    raise UnsupportedInlineTransform('Unsupported nonnumeric registry transformation or input: ' + format_name)
