"""Inventory of statement tables that a selected primary statement leaves out.

A statement can continue on another page under its own title marked
"(continued)", or be followed by a separately headed reconciliation of its
closing cash. Those tables are tagged source content of the statement. The
selected segments do not include them, so the omission is recorded with the
statement instead of passing as a complete extraction. Nothing is extracted or
bound here: the inventory names the original table and counts its tagged facts.
"""

from copy import deepcopy
from dataclasses import replace
import re

from lxml import html

from .source_locators import source_xpath
from .tables import _hidden, _statement_heading, _statement_heading_values, local


POLICY = 'statement-continuation-inventory-v1'
_CONTINUED = re.compile(r'[\s:–—-]*(?:\(\s*)?continued(?:\s*\))?\s*$', re.I)
# Titles of tables printed after the cash-flow statement as part of it.
_CASH_FOLLOWERS = (
    ('supplemental_cash_flow_disclosures',
     re.compile(r'supplemental\b.{0,60}\b(?:cash flows?|non-?cash)\b.{0,60}', re.I)),
    ('cash_reconciliation_table', re.compile(r'reconciliation of cash\b.{0,120}', re.I)),
)


def _tagged(table):
    return [node for node in table.iter() if local(node) == 'nonfraction' and not _hidden(node)]


def omitted_statement_tables(tree, tables):
    """Return, per statement type, the tagged tables printed with it that no segment covers.

    Two kinds of evidence qualify, both read from the original document: a
    table under the statement's own title marked "(continued)", and the tagged
    tables that directly follow the cash-flow statement under a supplemental
    cash-flow or cash-reconciliation title. A table titled that way elsewhere
    (for example in a lease note) is not part of the statement.
    """
    selected = {table.structure['source_xpath'] for table in tables}
    kinds = {table.statement_type for table in tables}
    candidates = [(table, source_xpath(tree, table)) for table in tree.xpath('//table')
                  if not _hidden(table) and not table.xpath('.//table')]
    found, recorded = {}, set()

    def record(kind, table, path, reason, heading):
        recorded.add(path)
        found.setdefault(kind, []).append({'source_xpath': path, 'reason': reason, 'heading': ' '.join(heading.split()),
                                           'tagged_fact_count': len(_tagged(table))})

    for table, path in candidates:
        if path in selected or not _tagged(table):
            continue
        for value in _statement_heading_values(table):
            title = _CONTINUED.sub('', value)
            if title != value and _statement_heading(title) in kinds:
                record(_statement_heading(title), table, path, 'continued_statement_heading', value)
                break
    if 'cash_flow' in kinds:
        paths = [path for _, path in candidates]
        last = max(paths.index(table.structure['source_xpath']) for table in tables
                   if table.statement_type == 'cash_flow' and table.structure['source_xpath'] in paths)
        for table, path in candidates[last + 1:]:
            if not _tagged(table):
                continue                      # page furniture between the statement and its follower
            if path in selected:
                break
            match = next(((reason, value) for value in _statement_heading_values(table)
                          for reason, pattern in _CASH_FOLLOWERS
                          if pattern.fullmatch(' '.join(value.split()).strip(':'))), None)
            if match is None:
                break
            if path not in recorded:
                record('cash_flow', table, path, *match)
    return found


def with_omitted_continuations(tables, source):
    """Attach the inventory to the last segment of each statement; other tables are unchanged."""
    tree = html.document_fromstring(source, parser=html.HTMLParser(encoding='utf-8', no_network=True)).getroottree()
    omitted = omitted_statement_tables(tree, tables)
    last = {table.statement_type: table.table_key for table in tables}
    result = []
    for table in tables:
        structure = deepcopy(table.structure)
        if table.table_key == last[table.statement_type]:
            structure['omitted_continuations'] = {'policy': POLICY, 'tables': omitted.get(table.statement_type, [])}
        result.append(replace(table, structure=structure))
    return tuple(result)
