"""Prove contiguous bank cash components against two identical closing totals."""

import json
import re


def bind_bank_cash_components(rows, periods, facts, ns, html_ids):
    from .cash_reconciliation import TOTAL, _aligned_amounts, _entries, _gaap, _words
    from .tables import TableExtractionError

    total_label = 'cash due from banks and deposits with banks at end of period'
    cash_label = r'cash and due from banks(?: including segregated cash and other deposits)?'
    deposit_label = 'deposits with banks net of allowance'
    bindings, groups = {}, []
    for closing_index, closing_row in enumerate(rows):
        if not closing_row or _words(closing_row[0]['display_text']) != total_label:
            continue
        following = [(index, row) for index, row in enumerate(rows[closing_index + 1:closing_index + 10], closing_index + 1)
                     if row and any(cell['display_text'].strip(' \u200b') for cell in row)]
        if not following or not re.fullmatch(cash_label, _words(following[0][1][0]['display_text'])):
            continue
        if (len(following) < 3 or _words(following[1][1][0]['display_text']) != deposit_label
                or _words(following[2][1][0]['display_text']) != total_label):
            raise TableExtractionError('Bank cash component block lacks its contiguous deposits row and repeated closing total')
        component_rows = [_entries(row, periods, facts, ns, html_ids) for _, row in following[:2]]
        repeated = _entries(following[2][1], periods, facts, ns, html_ids)
        closing = _entries(closing_row, periods, facts, ns, html_ids)
        if len({json.dumps(entry['scope'], sort_keys=True) for entry in closing.values()}) != 1:
            raise TableExtractionError('Bank cash closing columns disagree on entity or currency')
        if any(set(entries) != set(closing) for entries in [*component_rows, repeated]):
            raise TableExtractionError('Bank cash component columns must retain the exact closing date axes')
        evidence = []
        for axis, endpoint in closing.items():
            cash, deposits, total = component_rows[0][axis], component_rows[1][axis], repeated[axis]
            if (not _gaap(endpoint['fact']['concept'], {TOTAL}) or not _gaap(total['fact']['concept'], {TOTAL})
                    or not _gaap(cash['fact']['concept'], {'CashAndDueFromBanks'})
                    or not _gaap(deposits['fact']['concept'], {'InterestBearingDepositsInBanks'})
                    or any(entry['scope'] != endpoint['scope'] for entry in (cash, deposits, total))):
                raise TableExtractionError('Bank reconciliation requires its exact standard concepts and source scope')
            values = _aligned_amounts([cash['amount'], deposits['amount'], total['amount'], endpoint['amount']])
            if values[0] + values[1] != values[2] or values[2] != values[3]:
                raise TableExtractionError('Bank cash components do not equal both original closing totals')
            entries = [cash, deposits, total]
            for entry in entries:
                key = entry['cell']['cell_id']
                if key in bindings:
                    raise TableExtractionError('Bank cash component blocks overlap')
                bindings[key] = {'method': 'cash_reconciliation', 'period': dict(endpoint['fact']['period']),
                                 'heading_source_xpath': closing_row[0]['source_xpath']}
            evidence.append({'column_period': json.loads(axis), 'period': dict(endpoint['fact']['period']),
                             'scope': endpoint['scope'], 'closing_xml_id': endpoint['fact']['xml_id'],
                             'component_xml_ids': [cash['fact']['xml_id'], deposits['fact']['xml_id']],
                             'total_xml_id': total['fact']['xml_id'], 'cell_ids': [entry['cell']['cell_id'] for entry in entries]})
        groups.append({'verification_version': 'bank-cash-components-v1',
                       'heading_source_xpath': closing_row[0]['source_xpath'], 'heading_text': closing_row[0]['display_text'],
                       'first_data_row': following[0][0], 'last_data_row': following[2][0], 'periods': evidence, 'extension_concepts': []})
    return bindings, groups
