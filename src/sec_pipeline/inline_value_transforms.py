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
CANADIAN_PROVINCES = {'alberta', 'british columbia', 'manitoba', 'new brunswick', 'newfoundland and labrador',
                      'northwest territories', 'nova scotia', 'nunavut', 'ontario', 'prince edward island',
                      'quebec', 'saskatchewan', 'yukon'}
# EDGAR state and country codes (edgarprovcountryen): US states keep postal codes;
# provinces and countries use EDGAR's own codes. Values not listed fail explicitly.
EDGAR_PROVINCE_COUNTRY_CODES = {
    **{name: code for name, code in STATE_PROVINCE_CODES.items() if name not in CANADIAN_PROVINCES},
    'alberta': 'A0', 'british columbia': 'A1', 'manitoba': 'A2', 'new brunswick': 'A3',
    'newfoundland and labrador': 'A4', 'newfoundland': 'A4', 'nova scotia': 'A5', 'ontario': 'A6',
    'prince edward island': 'A7', 'quebec': 'A8', 'saskatchewan': 'A9', 'yukon': 'B0',
    'bermuda': 'D0', 'cayman islands': 'E9', 'ireland': 'L2', 'jersey': 'Y9', 'liberia': 'N0',
    'luxembourg': 'N4', 'netherlands': 'P7', 'the netherlands': 'P7', 'singapore': 'U0',
    'switzerland': 'V8', 'united kingdom': 'X0',
}
# ISO 3166-1 alpha-2 codes (countrynameen).
COUNTRY_CODES = {
    'united states': 'US', 'united states of america': 'US', 'usa': 'US', 'u.s.a.': 'US', 'u.s.': 'US',
    'united kingdom': 'GB', 'england': 'GB', 'ireland': 'IE', 'switzerland': 'CH', 'bermuda': 'BM',
    'netherlands': 'NL', 'the netherlands': 'NL', 'singapore': 'SG', 'canada': 'CA', 'jersey': 'JE',
    'luxembourg': 'LU', 'cayman islands': 'KY', 'liberia': 'LR', 'panama': 'PA', 'germany': 'DE',
    'france': 'FR', 'japan': 'JP', 'china': 'CN', 'india': 'IN', 'israel': 'IL', 'australia': 'AU',
    'mexico': 'MX', 'belgium': 'BE', 'sweden': 'SE', 'spain': 'ES', 'italy': 'IT', 'puerto rico': 'PR',
}
EXCHANGE_CODES = {
    'new york stock exchange': 'NYSE', 'nyse': 'NYSE', 'nyse american': 'NYSEAMER', 'nyse arca': 'NYSEArca',
    'nasdaq': 'NASDAQ', 'nasdaq global select market': 'NASDAQ', 'nasdaq stock market': 'NASDAQ',
    'nasdaq global market': 'NASDAQ', 'nasdaq capital market': 'NASDAQ', 'chicago stock exchange': 'CHX',
}
QUARTER_ENDS = {'1': '03-31', '2': '06-30', '3': '09-30', '4': '12-31'}
MODERN_DATES = frozenset({
    'date-monthname-day-year-en', 'date-monthname-day-en', 'date-month-day-year', 'date-month-day',
    'date-monthname-year-en', 'date-day-monthname-en', 'date-day-monthname-year-en',
    'date-year-month', 'date-year-month-day',
})
# Hyphen-like characters that filers render in place of an ASCII hyphen.
HYPHENS = str.maketrans(dict.fromkeys((0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212), '-'))
# EDGAR converts fractional months to days with the mean month (365.25 / 12 days).
DAYS_PER_MONTH = Decimal('30.4375')


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
    nonnumeric = ((namespace in MODERN and name in {'fixed-true', 'fixed-false', 'fixed-empty', *MODERN_DATES})
                  or (namespace in OLDER and name in {'booleantrue', 'booleanfalse', 'datemonthdayyearen'})
                  or (namespace in SEC and name in {'duryear', 'durmonth', 'durweek', 'durday', 'durwordsen',
                      'entityfilercategoryen', 'stateprovnameen', 'exchnameen', 'boolballotbox', 'yesnoballotbox',
                      'datequarterend', 'countrynameen', 'edgarprovcountryen'}))
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
    if name == 'date-monthname-year-en':
        # 'January 2028', 'December, 2026', 'Jan-24' and 'Sept-24' -> gYearMonth.
        match = re.fullmatch('(' + words + r')\.?[^0-9a-z]*([0-9]{2}|[0-9]{4})', value, re.I)
        if match is None:
            raise UnsupportedInlineTransform('Unsupported date transformation input.')
        return f'{int(match[2]) + (2000 if len(match[2]) == 2 else 0):04d}-{MONTHS[match[1].lower()]:02d}'
    if name in {'date-year-month', 'date-year-month-day'}:
        expression = r'([0-9]{4})[^0-9]+([0-9]{1,2})' + (r'[^0-9]+([0-9]{1,2})' if name.endswith('-day') else '')
        match = re.fullmatch(expression, value)
        if match is None:
            raise UnsupportedInlineTransform('Unsupported date transformation input.')
        if not name.endswith('-day'):
            if not 1 <= int(match[2]) <= 12:
                raise UnsupportedInlineTransform('Inline month is not a calendar month.')
            return f'{match[1]}-{int(match[2]):02d}'
        try:
            return date(int(match[1]), int(match[2]), int(match[3])).isoformat()
        except ValueError as exc:
            raise UnsupportedInlineTransform('Inline date is not a calendar date.') from exc
    if name in {'date-day-monthname-en', 'date-day-monthname-year-en'}:
        # '31 December' -> --12-31 and '31 March 2026' -> 2026-03-31.
        expression = r'([0-9]{1,2})(?:st|nd|rd|th)?[^0-9a-z]+(' + words + r')\.?'
        if name.endswith('-year-en'):
            expression += r'[^0-9]+([0-9]{2}|[0-9]{4})'
        match = re.fullmatch(expression, value, re.I)
        if match is None:
            raise UnsupportedInlineTransform('Unsupported date transformation input.')
        year = 2000 if not name.endswith('-year-en') else int(match[3]) + (2000 if len(match[3]) == 2 else 0)
        try:
            result = date(year, MONTHS[match[2].lower()], int(match[1]))
        except ValueError as exc:
            raise UnsupportedInlineTransform('Inline date is not a calendar date.') from exc
        return result.isoformat() if name.endswith('-year-en') else result.strftime('--%m-%d')
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


def _quarter_end(value):
    match = re.fullmatch(r'q([1-4])[\s,]*([0-9]{4})', value, re.I)
    if match is None:
        raise UnsupportedInlineTransform('Unsupported quarter-end input.')
    return f'{match[2]}-{QUARTER_ENDS[match[1]]}'


def _duration_number(value, unit):
    """Match EDGAR's numeric durations, truncating each smaller component.

    Years keep a year component ('0.5' -> P0Y6M), months a month component
    ('0' -> P0M) and days a day component; other zero components are omitted.
    Fractional months use 30.4375 days and fractional days whole hours.
    """
    if not re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', value) or len(value) > 200:
        raise UnsupportedInlineTransform('Unsupported numeric duration input.')
    number = Decimal(value)
    negative, number = number < 0, number.copy_abs()
    whole = int(number)
    with localcontext() as context:
        context.prec = max(32, len(number.as_tuple().digits) + 10)
        fraction = number - whole
        if unit == 'year':
            months = fraction * 12
            parts = [(whole, 'Y', True), (int(months), 'M', False),
                     (int((months - int(months)) * DAYS_PER_MONTH), 'D', False)]
        elif unit == 'month':
            parts = [(whole, 'M', True), (int(fraction * DAYS_PER_MONTH), 'D', False)]
        elif unit == 'week' and not fraction:
            parts = [(whole * 7, 'D', True)]
        elif unit == 'day':
            parts = [(whole, 'D', True), (int(fraction * 24), 'H', False)]
        else:
            raise UnsupportedInlineTransform('Unsupported duration precision.')
    suffix = ''.join(('T' if code == 'H' else '') + str(count) + code
                     for count, code, required in parts if count or required)
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
        if name in MODERN_DATES:
            return _date_value(value, name)
    if namespace in OLDER:
        # Registry 2/3 booleans are fixed values, whatever text the filer shows.
        if name in {'booleantrue', 'booleanfalse'}:
            return 'true' if name == 'booleantrue' else 'false'
        if name == 'datemonthdayyearen':
            return _date_value(value, 'date-monthname-day-year-en')
    if namespace in SEC:
        lowered = value.lower().translate(HYPHENS)
        if name in {'duryear', 'durmonth', 'durweek', 'durday'}:
            return _duration_number(value, name[3:])
        if name == 'durwordsen':
            return _duration_words(lowered)
        if name == 'datequarterend':
            return _quarter_end(value)
        if name == 'entityfilercategoryen':
            categories = {'large accelerated filer': 'Large Accelerated Filer', 'accelerated filer': 'Accelerated Filer',
                          'non-accelerated filer': 'Non-accelerated Filer', 'non accelerated filer': 'Non-accelerated Filer'}
            if lowered in categories:
                return categories[lowered]
        if name == 'stateprovnameen':
            if lowered in STATE_PROVINCE_CODES:
                return STATE_PROVINCE_CODES[lowered]
        if name == 'edgarprovcountryen':
            if lowered in EDGAR_PROVINCE_COUNTRY_CODES:
                return EDGAR_PROVINCE_COUNTRY_CODES[lowered]
        if name == 'countrynameen':
            if lowered in COUNTRY_CODES:
                return COUNTRY_CODES[lowered]
        if name == 'exchnameen':
            exchange = re.sub(r'^the\s+|[,.]', '', lowered)
            exchange = re.sub(r'\s+(?:llc|incorporated|inc)$', '', exchange)
            if exchange in EXCHANGE_CODES:
                return EXCHANGE_CODES[exchange]
        if name in {'boolballotbox', 'yesnoballotbox'} and value in {'☐', '☑', '☒'}:
            return ('false' if value == '☐' else 'true') if name == 'boolballotbox' else ('No' if value == '☐' else 'Yes')
    raise UnsupportedInlineTransform('Unsupported nonnumeric registry transformation or input: ' + format_name)
