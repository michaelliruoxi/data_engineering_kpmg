"""Joint cash continuations require exact source scope and reconciliation."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from lxml import html

from sec_pipeline.joint_cash_continuation import COMPONENTS, RECONCILIATION, TOTAL, USD, validate_cash_continuation
from sec_pipeline.tables import TableExtractionError, xml_evidence


ROOT = Path(__file__).resolve().parents[1]
GAAP = 'http://fasb.org/us-gaap/2026'
DURATION = {'startDate': '2026-01-01', 'endDate': '2026-06-30'}
INSTANT = {'instant': '2026-06-30'}
SCOPE = {'entity': {'scheme': 'http://www.sec.gov/CIK', 'identifier': '0000000001'}, 'unit': USD, 'dimensions': []}


def fixture(duration=False):
    main_names = ['NetCashProvidedByUsedInOperatingActivities', 'NetCashProvidedByUsedInInvestingActivities',
                  'NetCashProvidedByUsedInFinancingActivities', TOTAL]
    candidate_names = ['DividendsPaid'] if duration else [*COMPONENTS, TOTAL]
    main_rows, current_rows, main_refs, current_refs = [], [], [], []
    for prefix, names, rows, refs, amounts in [('main', main_names, main_rows, main_refs, ['100', '-10', '-20', '100']),
                                             ('candidate', candidate_names, current_rows, current_refs, ['5'] if duration else ['80', '10', '10', '100'])]:
        for index, (name, value) in enumerate(zip(names, amounts)):
            identifier = f'{prefix}-{index}'
            period = DURATION if (prefix == 'main' and index < 3) or duration and prefix == 'candidate' else INSTANT
            refs.append({'xml_id': identifier, 'concept': '{' + GAAP + '}' + name, 'raw_value': value,
                         'is_nil': False, 'context_ref': identifier, 'unit_ref': 'usd', 'period': dict(period),
                         'entity': deepcopy(SCOPE['entity']), 'unit': deepcopy(USD), 'dimensions': []})
            rows.append(f'<tr><td>{name}</td><td><ix:nonfraction id="{identifier}" name="g:{name}" '
                        f'contextref="{identifier}" unitref="usd">{value}</ix:nonfraction></td></tr>')
    source = (f'<html xmlns:g="{GAAP}" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
              '<table id="main">' + ''.join(main_rows) + '</table><p>' + RECONCILIATION + '</p>'
              '<table id="candidate">' + ''.join(current_rows) + '</table></body></html>')
    tree = html.document_fromstring(source).getroottree()
    duration_axis, candidate_axis = json.dumps(DURATION, sort_keys=True), json.dumps(DURATION if duration else INSTANT, sort_keys=True)
    main = {'table': tree.xpath('//table[@id="main"]')[0], 'axes': [duration_axis], 'references': {duration_axis: main_refs},
            'evidence': {duration_axis: {str(index): [fact['xml_id']] for index, fact in enumerate(main_refs)}},
            'scopes': {json.dumps(SCOPE, sort_keys=True)}, 'heading': {'kind': 'cash_flow'}}
    candidate = {'table': tree.xpath('//table[@id="candidate"]')[0], 'axes': [candidate_axis],
                 'references': {candidate_axis: current_refs}, 'heading': {'kind': 'cash_flow', 'continued': True}, 'unresolved': False}
    return main, candidate


class JointCashContinuationTests(unittest.TestCase):
    def test_original_cash_components_and_total_receive_inspectable_proof(self):
        main, candidate = fixture()
        proof = validate_cash_continuation(main, candidate)
        self.assertEqual(proof['segment_role'], 'cash_reconciliation')
        self.assertEqual(proof['periods'][0]['period'], INSTANT)
        self.assertEqual(proof['periods'][0]['main_closing_xml_id'], 'main-3')
        self.assertEqual(proof['periods'][0]['component_xml_ids'], ['candidate-0', 'candidate-1', 'candidate-2'])
        self.assertEqual(proof['verified_xml_ids'], ['candidate-0', 'candidate-1', 'candidate-2', 'candidate-3'])

    def test_duration_continuation_retains_stock_dimensions_but_not_entity_changes(self):
        main, candidate = fixture(duration=True)
        fact = next(iter(candidate['references'].values()))[0]
        fact['dimensions'] = [{'kind': 'explicit', 'dimension': '{' + GAAP + '}StatementClassOfStockAxis',
                               'member': '{' + GAAP + '}PreferredStockMember'}]
        self.assertEqual(validate_cash_continuation(main, candidate), {'segment_role': 'statement', 'column_periods': [DURATION]})
        fact['dimensions'] = [{'kind': 'explicit', 'dimension': '{http://xbrl.sec.gov/dei/2026}LegalEntityAxis', 'member': '{issuer}SubsidiaryMember'}]
        with self.assertRaises(TableExtractionError):
            validate_cash_continuation(main, candidate)

    def test_dates_currency_entity_scope_missing_caption_and_components_fail_closed(self):
        for change in ('date', 'currency', 'entity', 'dimension', 'caption', 'total', 'closing', 'nil', 'missing', 'namespace'):
            main, candidate = fixture()
            axis = candidate['axes'][0]
            fact = candidate['references'][axis][0]
            if change == 'date':
                fact['period']['instant'] = '2026-07-31'
            elif change == 'currency':
                fact['unit']['numerator'] = ['{http://www.xbrl.org/2003/iso4217}EUR']
            elif change == 'entity':
                fact['entity']['identifier'] = '0000000002'
            elif change == 'dimension':
                fact['dimensions'] = [{'kind': 'explicit', 'dimension': '{issuer}BusinessAxis', 'member': '{issuer}ComponentMember'}]
            elif change == 'caption':
                candidate['table'].getprevious().text = 'Unrelated balances'
            elif change in {'total', 'closing'}:
                target = candidate if change == 'total' else main
                item = next(iter(target['references'].values()))[-1]
                item['raw_value'] = '101'
                target['table'].xpath(f'.//*[@id="{item["xml_id"]}"]')[0].text = '101'
            elif change == 'nil':
                fact['is_nil'] = True
            elif change == 'missing':
                candidate['references'][axis].pop(2)
            else:
                fact['concept'] = '{https://issuer.example/2026}CashAndCashEquivalentsAtCarryingValue'
                candidate['table'].getroottree().getroot().set('xmlns:other', 'https://issuer.example/2026')
                candidate['table'].xpath('.//*[@id="candidate-0"]')[0].set('name', 'other:CashAndCashEquivalentsAtCarryingValue')
            with self.subTest(change=change), self.assertRaises(TableExtractionError):
                validate_cash_continuation(main, candidate)

    def test_exponent_gap_and_continuation_axis_mismatch_are_rejected(self):
        main, candidate = fixture()
        for values in (next(iter(main['references'].values())), next(iter(candidate['references'].values()))):
            for fact in values:
                if fact['concept'].endswith('}' + TOTAL):
                    fact['raw_value'] = '1E30'
                    candidate['table'].getroottree().xpath(f'//*[@id="{fact["xml_id"]}"]')[0].text = '1E30'
        for index, value in enumerate(['1E30', '1', '0']):
            candidate['references'][candidate['axes'][0]][index]['raw_value'] = value
            candidate['table'].xpath(f'.//*[@id="candidate-{index}"]')[0].text = value
        with self.assertRaises(TableExtractionError):
            validate_cash_continuation(main, candidate)
        main, candidate = fixture(duration=True)
        candidate['axes'] = [json.dumps({'startDate': '2026-04-01', 'endDate': '2026-06-30'}, sort_keys=True)]
        with self.assertRaises(TableExtractionError):
            validate_cash_continuation(main, candidate)

    def test_hst_original_source_component_set_reconciles_to_parent_cash(self):
        folder = ROOT / 'data/raw/sec/0001070750-26-000126'
        html_paths, xml_paths = list(folder.glob('*.htm')), list(folder.glob('*_htm.xml'))
        if len(html_paths) != 1 or len(xml_paths) != 1:
            self.skipTest('Archived joint-statement source unavailable')
        from collections import Counter
        from sec_pipeline.joint_statements import _primary_identity, _headings, _candidate
        from sec_pipeline.tables import local
        tree = html.parse(str(html_paths[0]), parser=html.HTMLParser(encoding='utf-8'))
        facts = xml_evidence(xml_paths[0], include_entity=True)
        identity = _primary_identity(tree, facts)
        ids = Counter(node.get('id') for node in tree.iter() if local(node) == 'nonfraction' and node.get('id'))
        candidates = []
        for xpath in ('/html/body/div[87]/table', '/html/body/div[97]/table'):
            table = tree.xpath(xpath)[0]
            candidates.append(_candidate(table, tree, facts, ids, identity, _headings(table, tree, identity)))
        proof = validate_cash_continuation(*candidates)
        self.assertEqual([period['main_closing_xml_id'] for period in proof['periods']], ['f-320', 'f-321'])
        self.assertEqual([period['total_xml_id'] for period in proof['periods']], ['f-328', 'f-329'])
        self.assertEqual(len(proof['verified_xml_ids']), 8)


if __name__ == '__main__':
    unittest.main()
