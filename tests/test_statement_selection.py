"""Issuer heading variants and conservative primary-statement evidence."""

import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

from lxml import etree, html

from sec_pipeline.tables import TableExtractionError, extract_tables, header_period, select_statements, verify_inline


GAAP = 'http://fasb.org/us-gaap/2024'
CONCEPTS = {
    'income_statement': ('Revenues', 'OperatingIncomeLoss', 'NetIncomeLoss'),
    'balance_sheet': ('Assets', 'Liabilities', 'StockholdersEquity'),
    'cash_flow': ('NetCashProvidedByUsedInOperatingActivities',
                  'NetCashProvidedByUsedInInvestingActivities',
                  'NetCashProvidedByUsedInFinancingActivities',
                  'CashAndCashEquivalentsAtCarryingValue'),
}
HEADINGS = {'income_statement': 'Consolidated Statements of Operations',
            'balance_sheet': 'Consolidated Statements of Financial Position',
            'cash_flow': 'Consolidated Statements of Cash Flows'}


def statement(kind, heading=None, *, concepts=None, position='before', prefix='g', namespace=GAAP):
    heading = HEADINGS[kind] if heading is None else heading
    names = CONCEPTS[kind] if concepts is None else concepts
    rows = ''.join(f'<tr><td>Reported item {index}</td><td><ix:nonfraction '
                   f'name="{prefix}:{concept}" id="{kind}_{index}">1</ix:nonfraction></td></tr>'
                   for index, concept in enumerate(names))
    title = f'<p>{heading}</p>'
    inside = f'<caption>{heading}</caption>' if position == 'caption' else (
        f'<tr><th colspan="2">{heading}</th></tr>' if position == 'row' else '')
    table = f'<table id="{kind}" xmlns:{prefix}="{namespace}">{inside}{rows}</table>'
    return title + table if position == 'before' else table


def filing(**overrides):
    return etree.ElementTree(html.document_fromstring('<html><body>' + ''.join(
        overrides.get(kind, statement(kind)) for kind in CONCEPTS) + '</body></html>'))


class StatementSelectionTests(unittest.TestCase):
    def test_common_income_headings_with_different_revenue_wording(self):
        for heading in ('CONDENSED CONSOLIDATED STATEMENTS OF INCOME (UNAUDITED)',
                        'Consolidated Statements of Earnings', 'Statements of Operations',
                        'ConsolidatedStatements of Operations',
                        'Consolidated Statements of Income (Loss)', 'Income Statements'):
            with self.subTest(heading=heading):
                selected = select_statements(filing(income_statement=statement('income_statement', heading)))
                self.assertEqual(selected['income_statement'].get('id'), 'income_statement')

    def test_balance_and_cash_heading_variants(self):
        for heading in ('Consolidated Balance Sheets', 'Statements of Financial Condition',
                        'Condensed Consolidated Statements of Financial Position'):
            selected = select_statements(filing(balance_sheet=statement('balance_sheet', heading)))
            self.assertEqual(selected['balance_sheet'].get('id'), 'balance_sheet')
        selected = select_statements(filing(cash_flow=statement('cash_flow', 'CASH FLOWS STATEMENTS')))
        self.assertEqual(selected['cash_flow'].get('id'), 'cash_flow')

    def test_caption_opening_row_and_wrapper_headings(self):
        for position in ('caption', 'row'):
            with self.subTest(position=position):
                selected = select_statements(filing(**{kind: statement(kind, position=position) for kind in CONCEPTS}))
                self.assertEqual(set(selected), set(CONCEPTS))
        value = statement('income_statement').replace('<table ', '<div><div><table ').replace('</table>', '</table></div></div>')
        self.assertEqual(len(select_statements(filing(income_statement=value))), 3)

    def test_heading_cannot_be_borrowed_across_a_table(self):
        value = '<p>Consolidated Statements of Operations</p><table><tr><td>Other table</td></tr></table>'
        value += statement('income_statement', heading='', position='caption')
        with self.assertRaisesRegex(TableExtractionError, 'income_statement: expected one statement, found 0'):
            select_statements(filing(income_statement=value))

    def test_partial_statements_and_untagged_label_lookalikes_fail(self):
        for kind in CONCEPTS:
            with self.subTest(kind=kind):
                with self.assertRaises(TableExtractionError):
                    select_statements(filing(**{kind: statement(kind, concepts=CONCEPTS[kind][:-1])}))
        value = '<p>INCOME STATEMENTS</p><table><tr><td>Total revenue Operating income Net income</td></tr></table>'
        with self.assertRaises(TableExtractionError):
            select_statements(filing(income_statement=value))

    def test_selected_data_notes_and_prose_are_not_primary_income(self):
        for title in ('Selected Consolidated Statements of Income', 'Summary of Statements of Income',
                      'See Consolidated Statements of Income',
                      'Note 3. Consolidated Statements of Operations'):
            with self.subTest(title=title), self.assertRaises(TableExtractionError):
                select_statements(filing(income_statement=statement('income_statement', title)))

    def test_comprehensive_income_requires_the_full_primary_income_concept_groups(self):
        title = 'CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME (LOSS)'
        selected = select_statements(filing(income_statement=statement('income_statement', title)))
        self.assertEqual(selected['income_statement'].get('id'), 'income_statement')
        with self.assertRaises(TableExtractionError):
            select_statements(filing(income_statement=statement('income_statement', title, concepts=(
                'NetIncomeLoss', 'OtherComprehensiveIncomeLossNetOfTax', 'ComprehensiveIncomeNetOfTax'))))

    def test_heading_child_blocks_and_neutral_qualifiers_do_not_hide_statements(self):
        for qualifier in ('', '(Unaudited)', '(In millions, except par value amounts)'):
            value = ('<div><div>Example Corporation</div><div>Consolidated Statements of Operations</div></div>'
                     '<div><span>' + qualifier + '</span>'
                     + statement('income_statement', heading='', position='caption') + '</div>')
            with self.subTest(qualifier=qualifier):
                self.assertEqual(len(select_statements(filing(income_statement=value))), 3)

    def test_explicit_breaks_separate_company_statement_and_units_heading_lines(self):
        value = ('<div><span>EXAMPLE INC. AND SUBSIDIARIES<br>CONSOLIDATED BALANCE SHEETS<br></span>'
                 '<span>(in millions, except share and per share data)<br>(unaudited)</span></div>'
                 + statement('balance_sheet', heading='', position='caption'))
        self.assertEqual(len(select_statements(filing(balance_sheet=value))), 3)

    def test_explicit_consolidated_statement_precedes_generic_parent_note(self):
        value = statement('income_statement').replace('id="income_statement"', 'id="consolidated-income"')
        value += statement('income_statement', 'Statements of income')
        self.assertEqual(select_statements(filing(income_statement=value))['income_statement'].get('id'), 'consolidated-income')

    def test_parent_company_only_contexts_cannot_supply_primary_statement_evidence(self):
        value = ('<xbrli:context id="parent"><xbrli:entity><xbrli:segment>'
                 '<xbrldi:explicitMember dimension="srt:ConsolidationItemsAxis">srt:ParentCompanyMember</xbrldi:explicitMember>'
                 '</xbrli:segment></xbrli:entity></xbrli:context>'
                 + statement('income_statement').replace('<ix:nonfraction ', '<ix:nonfraction contextref="parent" '))
        with self.assertRaisesRegex(TableExtractionError, 'income_statement: expected one statement, found 0'):
            select_statements(filing(income_statement=value))

    def test_concepts_use_namespace_uri_not_prefix_or_issuer_local_name(self):
        selected = select_statements(filing(income_statement=statement('income_statement', prefix='standard')))
        self.assertEqual(len(selected), 3)
        with self.assertRaises(TableExtractionError):
            select_statements(filing(income_statement=statement(
                'income_statement', prefix='us-gaap', namespace='https://issuer.example/2024')))

    def test_duplicate_supported_statements_fail_instead_of_selecting_first(self):
        value = statement('income_statement') + statement('income_statement', 'Consolidated Statements of Earnings')
        with self.assertRaisesRegex(TableExtractionError, 'income_statement: expected one statement, found 2'):
            select_statements(filing(income_statement=value))

    def test_continued_statement_is_not_silently_reduced_to_its_first_table(self):
        value = statement('income_statement') + statement(
            'income_statement', 'Consolidated Statements of Operations (continued)',
            concepts=('EarningsPerShareBasic',))
        with self.assertRaisesRegex(TableExtractionError, 'split statement continuation is unsupported'):
            select_statements(filing(income_statement=value))

    def test_hidden_statement_duplicates_and_nested_layout_tables_are_ignored(self):
        for attribute in ('hidden', 'style="display: none"', 'aria-hidden="true"'):
            value = statement('income_statement') + f'<div {attribute}>' + statement('income_statement') + '</div>'
            self.assertEqual(len(select_statements(filing(income_statement=value))), 3)
        tree = filing()
        body = tree.getroot().find('body')
        wrapper = html.fromstring('<table><tr><td></td></tr></table>')
        for node in list(body):
            wrapper[0][0].append(node)
        body.append(wrapper)
        self.assertEqual(len(select_statements(tree)), 3)

    def test_hidden_facts_cannot_supply_missing_statement_evidence(self):
        tree = filing()
        income = tree.xpath('//table[@id="income_statement"]')[0]
        income.xpath('.//*[contains(@name, "NetIncomeLoss")]')[0].set('style', 'display:none')
        with self.assertRaises(TableExtractionError):
            select_statements(tree)


class StatementPeriodTests(unittest.TestCase):
    def test_calendar_quarters_year_to_date_and_annual_periods(self):
        cases = [
            (['Three Months Ended', 'September 30,', '2025'], '2025-07-01', '2025-09-30'),
            (['6 months ended June 30, 2024'], '2024-01-01', '2024-06-30'),
            (['Nine Months Ended March 31, 2026'], '2025-07-01', '2026-03-31'),
            (['Three Months EndedMarch 31,', '2026'], '2026-01-01', '2026-03-31'),
            (['Years Ended', 'December 31, 2023'], '2023-01-01', '2023-12-31'),
            (['Fiscal Year Ended June 30, 2025'], '2024-07-01', '2025-06-30'),
            (['Quarter Ended February 29, 2024'], '2023-12-01', '2024-02-29'),
            (['Twelve Months Ended', 'Sept. 30, 2024'], '2023-10-01', '2024-09-30'),
        ]
        for headings, start, end in cases:
            with self.subTest(headings=headings):
                self.assertEqual(header_period(headings), {'startDate': start, 'endDate': end})

    def test_balance_sheet_dates_and_explicit_week_periods(self):
        self.assertEqual(header_period(['As of October 28, 2023']), {'instant': '2023-10-28'})
        self.assertEqual(header_period(['52 weeks ended September 28, 2024']),
                         {'startDate': '2023-10-01', 'endDate': '2024-09-28'})
        self.assertEqual(header_period(['13 weeks ended December 28, 2024']),
                         {'startDate': '2024-09-29', 'endDate': '2024-12-28'})

    def test_fiscal_contexts_require_matching_heading_endpoint_and_duration(self):
        cases = [
            ('Years ended September 27,2025', '2024-09-29', '2025-09-27'),
            ('Years ended September 30,2023', '2022-09-25', '2023-09-30'),
            ('Three Months Ended June 27,2026', '2026-03-29', '2026-06-27'),
            ('Nine Months Ended June 27,2026', '2025-09-28', '2026-06-27'),
        ]
        for heading, start, end in cases:
            period = {'startDate': start, 'endDate': end}
            with self.subTest(heading=heading):
                self.assertEqual(header_period([heading], context_period=period), period)
        self.assertIsNone(header_period(['Three Months Ended June 27,2026'], context_period={
            'startDate': '2025-09-28', 'endDate': '2026-06-27'}))
        self.assertIsNone(header_period(['Years ended September 27,2025'], context_period={
            'startDate': '2024-09-29', 'endDate': '2025-09-28'}))
        self.assertIsNone(header_period(['Years ended September 27,2025'], context_period={
            'startDate': '2024-09-30', 'endDate': '2025-09-27'}))

    def test_year_only_headers_need_corroborated_annual_or_instant_contexts(self):
        annual = {'startDate': '2025-01-01', 'endDate': '2025-12-31'}
        self.assertEqual(header_period(['2025'], context_period=annual), annual)
        instant = {'instant': '2025-12-31'}
        self.assertEqual(header_period(['2025'], context_period=instant), instant)
        self.assertIsNone(header_period(['2025']))
        self.assertIsNone(header_period(['2024'], context_period=annual))
        self.assertIsNone(header_period(['Three months ended', '2025'], context_period=annual))
        self.assertIsNone(header_period(['2025'], context_period={'startDate': '2025-10-01', 'endDate': '2025-12-31'}))

    def test_fixed_zero_transformation_still_requires_zero_xml_value(self):
        element = html.fromstring('<ix:nonfraction name="g:ProceedsFromIssuanceOfLongTermDebt" '
                                  'contextref="year" unitref="usd" format="ixt:fixed-zero" scale="6">—</ix:nonfraction>')
        evidence = {'concept': '{' + GAAP + '}ProceedsFromIssuanceOfLongTermDebt',
                    'context_ref': 'year', 'unit_ref': 'usd', 'is_nil': False, 'raw_value': '0'}
        verify_inline(element, evidence, {'g': GAAP})
        with self.assertRaisesRegex(TableExtractionError, 'Inline/XML value mismatch'):
            verify_inline(element, evidence | {'raw_value': '1'}, {'g': GAAP})

    def test_ambiguous_invalid_and_unstated_fiscal_periods_remain_unresolved(self):
        for heading in ('Years ended December 31, 2024 and 2023', 'December 31 and June 30, 2024',
                        'Year ended September 28, 2024', 'Five months ended June 30, 2024',
                        'February 30, 2024', 'Three and nine months ended March 31, 2024',
                        'March 2024'):
            with self.subTest(heading=heading):
                self.assertIsNone(header_period([heading]))

    def test_untagged_dash_remains_unresolved_without_decimal_crash(self):
        root = Path(__file__).resolve().parents[1]
        source = root / 'data/raw/sec/0001193125-26-191507/msft-20260331.htm'
        instance = source.with_name('msft-20260331_htm.xml')
        scope = {'filing_id': UUID(int=1), 'html_document_id': UUID(int=2), 'xml_document_id': UUID(int=3)}
        baseline = extract_tables(source, instance, **scope)
        repeat = next(cell for row in baseline[2].structure['rows'] for cell in row
                      if any(ref['method'] == 'same_row_semantic_repeat' for ref in cell['fact_references']))
        tree = html.parse(str(source))
        cell = tree.xpath(repeat['source_xpath'])[0]
        for child in list(cell):
            cell.remove(child)
        cell.text = '—'
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / 'filing.htm'
            changed.write_bytes(html.tostring(tree))
            result = extract_tables(changed, instance, **scope)
        self.assertEqual(result[2].structure['inventory'].get('unresolved'), 1)

    def test_annual_filing_extracts_with_scoped_namespaces_and_case_insensitive_cash_label(self):
        xml_facts, statements = [], []
        for kind, names in CONCEPTS.items():
            rows = []
            header = 'December 31, 2024' if kind == 'balance_sheet' else 'Year Ended December 31, 2024'
            for index, concept in enumerate(names):
                instant = kind == 'balance_sheet' or concept == 'CashAndCashEquivalentsAtCarryingValue'
                context = 'instant' if instant else 'year'
                ident = f'{kind}_{index}'
                label = 'Cash and cash equivalents, END OF YEAR' if kind == 'cash_flow' and instant else concept
                rows.append(f'<tr><td>{label}</td><td><ix:nonfraction id="{ident}" '
                            f'name="g:{concept}" contextref="{context}" unitref="usd">1</ix:nonfraction></td></tr>')
                xml_facts.append(f'<g:{concept} id="{ident}" contextRef="{context}" unitRef="usd">1</g:{concept}>')
            statements.append(f'<p>{HEADINGS[kind]}</p><table xmlns:g="{GAAP}">'
                              f'<tr><td></td><td>{header}</td></tr>{"".join(rows)}</table>')
        source = '<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>' + ''.join(statements) + '</body></html>'
        instance = f'''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:g="{GAAP}" xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
          <xbrli:context id="year"><xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
          <xbrli:context id="instant"><xbrli:period><xbrli:instant>2024-12-31</xbrli:instant></xbrli:period></xbrli:context>
          <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>{''.join(xml_facts)}</xbrli:xbrl>'''
        with tempfile.TemporaryDirectory() as directory:
            html_path, xml_path = Path(directory) / 'filing.htm', Path(directory) / 'filing.xml'
            html_path.write_text(source, encoding='utf-8')
            xml_path.write_text(instance, encoding='utf-8')
            tables = extract_tables(html_path, xml_path, filing_id=UUID(int=1),
                                    html_document_id=UUID(int=2), xml_document_id=UUID(int=3))
        self.assertEqual([table.table_key for table in tables], list(CONCEPTS))
        self.assertTrue(all(table.structure['inventory'].get('unresolved', 0) == 0 for table in tables))
        self.assertEqual([table.structure['inventory']['resolved'] for table in tables], [3, 3, 4])


APPLE_SOURCES = Path(__file__).resolve().parents[1] / 'data/raw/sec'
APPLE_FILINGS = {'0000320193-25-000079': 'aapl-20250927', '0000320193-26-000020': 'aapl-20260627'}


@unittest.skipUnless(all((APPLE_SOURCES / accession / (stem + '.htm')).is_file()
                         and (APPLE_SOURCES / accession / (stem + '_htm.xml')).is_file()
                         for accession, stem in APPLE_FILINGS.items()), 'Archived Apple acceptance sources are unavailable')
class AppleStatementAcceptanceTests(unittest.TestCase):
    """Read-only checks against the archived annual and quarterly SEC sources."""

    scope = {'filing_id': UUID(int=1), 'html_document_id': UUID(int=2), 'xml_document_id': UUID(int=3)}

    def paths(self, accession='0000320193-25-000079'):
        stem = APPLE_FILINGS[accession]
        return (APPLE_SOURCES / accession / (stem + '.htm'),
                APPLE_SOURCES / accession / (stem + '_htm.xml'))

    def test_annual_and_quarterly_statements_preserve_fiscal_periods_and_values(self):
        expected = {
            '0000320193-25-000079': ([57, 56, 87], [
                ('income_statement', 'f-78', '416161000000', '2024-09-29', '2025-09-27'),
                ('income_statement', 'f-80', '383285000000', '2022-09-25', '2023-09-30'),
                ('cash_flow', 'f-355', '35934000000', None, '2025-09-27')]),
            '0000320193-26-000020': ([76, 58, 58], [
                ('income_statement', 'f-56', '109417000000', '2026-03-29', '2026-06-27'),
                ('income_statement', 'f-58', '364357000000', '2025-09-28', '2026-06-27'),
                ('cash_flow', 'f-352', '39544000000', None, '2026-06-27')]),
        }
        for accession, (counts, samples) in expected.items():
            with self.subTest(accession=accession):
                tables = extract_tables(*self.paths(accession), **self.scope)
                self.assertEqual([t.structure['inventory']['resolved'] for t in tables], counts)
                self.assertTrue(all(t.structure['inventory'].get('unresolved', 0) == 0 for t in tables))
                for kind, ident, value, start, end in samples:
                    table = next(t for t in tables if t.table_key == kind)
                    ref = next(ref for row in table.structure['rows'] for cell in row
                               for ref in cell['fact_references'] if ref['xml_id'] == ident)
                    self.assertEqual(ref['raw_value'], value)
                    self.assertEqual(ref['period'], {'startDate': start, 'endDate': end} if start else {'instant': end})
                for table in (tables[0], tables[2]):
                    evidence = table.structure['column_period_evidence']
                    self.assertTrue(all(item['method'] == 'corroborated_fiscal_contexts'
                                        and len(item['xml_ids']) >= 2 for item in evidence.values()))
                    for row in table.structure['rows']:
                        for cell in row:
                            if cell['classification'] == 'resolved':
                                self.assertEqual(cell['column_period'], evidence[str(cell['column'])]['period'])

    def test_conflicting_context_in_a_fiscal_column_is_not_resolved_by_majority(self):
        html_path, xml_path = self.paths()
        document = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
        instance = etree.parse(str(xml_path), etree.XMLParser(resolve_entities=False, no_network=True))
        fact = instance.xpath('//*[@id="f-78"]')[0]
        context = copy.deepcopy(instance.xpath('//*[@id="' + fact.get('contextRef') + '"]')[0])
        context.set('id', 'conflicting-period')
        context.find('.//{http://www.xbrl.org/2003/instance}startDate').text = '2024-09-22'
        instance.getroot().append(context)
        fact.set('contextRef', 'conflicting-period')
        document.xpath('//*[@id="f-78"]')[0].set('contextref', 'conflicting-period')
        with tempfile.TemporaryDirectory() as directory:
            changed_html, changed_xml = Path(directory) / 'filing.htm', Path(directory) / 'filing.xml'
            changed_html.write_bytes(html.tostring(document, encoding='utf-8'))
            changed_xml.write_bytes(etree.tostring(instance, encoding='utf-8'))
            tables = extract_tables(changed_html, changed_xml, **self.scope)
        unresolved = [cell for row in tables[0].structure['rows'] for cell in row if cell['classification'] == 'unresolved']
        self.assertTrue(unresolved)
        self.assertTrue(all(cell['unresolved_reason'] == 'Unresolved column period' for cell in unresolved))

    def test_changed_heading_endpoint_blocks_fiscal_context_resolution(self):
        html_path, xml_path = self.paths()
        document = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
        income = select_statements(document)['income_statement']
        replacements = 0
        for node in income.iter():
            if node.text and 'September 27,' in node.text:
                node.text = node.text.replace('September 27,', 'September 26,')
                replacements += 1
        self.assertEqual(replacements, 1)
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / 'filing.htm'
            changed.write_bytes(html.tostring(document, encoding='utf-8'))
            tables = extract_tables(changed, xml_path, **self.scope)
        self.assertGreater(tables[0].structure['inventory'].get('unresolved', 0), 0)


class DiverseStatementAcceptanceTests(unittest.TestCase):
    """Optional archived SEC fixtures; no downloads or database access."""

    scope = {'filing_id': UUID(int=1), 'html_document_id': UUID(int=2), 'xml_document_id': UUID(int=3)}

    def paths(self, accession):
        directory = APPLE_SOURCES / accession
        primary = list(directory.glob('*.htm'))
        instance = list(directory.glob('*_htm.xml'))
        if len(primary) != 1 or len(instance) != 1:
            self.skipTest('Archived acceptance source is unavailable')
        return primary[0], instance[0]

    def extract(self, accession):
        return extract_tables(*self.paths(accession), **self.scope)

    def test_existing_table_versions_keep_their_complete_record_payloads(self):
        # Captured before the shared-header/parent-company repairs. These
        # already valid records must remain replay-compatible under tables-v1.
        expected = {
            '0001193125-26-191507': 'b98d984cd627e19398f44f35db534086fb383c07ad533afdd8bcb85a786922b2',
            '0000320193-25-000079': '4c0014b66d030a17dd7a8bebb02244830db26a2836e052f72cf30ad141308495',
            '0000320193-26-000020': '9514f2ac5388b72732ffcf81ee93dc8de96dcca486db54b1fa67625645a608f2',
            '0001045810-26-000021': '8e4b731d72e1ca25fb3dbf567591a3f8d7165b1ff8a4d5d94e3d3aa0698bf78e',
            '0001045810-26-000075': 'd5d056f5c504a3fe7f7575b16a3a2b33676b78e413b29b363efe0242712ecf99',
            '0001628280-26-054343': '9dacaa05b3f9754365ce97aa946da6149c0feac51e5eb178a09f080f8ff85a96',
        }
        checked = 0
        for accession, digest in expected.items():
            if not list((APPLE_SOURCES / accession).glob('*.htm')):
                continue
            with self.subTest(accession=accession):
                tables = self.extract(accession)
                payload = json.dumps([asdict(table) for table in tables], default=str, sort_keys=True).encode()
                self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)
                checked += 1
        self.assertGreater(checked, 0)

    def test_jpm_consolidated_statements_exclude_parent_company_note(self):
        tables = self.extract('0001628280-26-008131')
        self.assertEqual([t.structure['inventory']['resolved'] for t in tables], [90, 66, 138])
        self.assertTrue(all(t.structure['inventory'].get('unresolved', 0) == 0 for t in tables))
        cash = [ref for row in tables[1].structure['rows'] for cell in row
                if cell['row_heading'] == 'Cash and due from banks' for ref in cell['fact_references']]
        self.assertEqual({ref['raw_value'] for ref in cash}, {'21742000000', '23372000000'})
        for table in tables:
            for row in table.structure['rows']:
                for cell in row:
                    for ref in cell['fact_references']:
                        self.assertFalse(any(dimension.get('member', '').endswith('}ParentCompanyMember')
                                             for dimension in ref['dimensions']))

    def test_exxon_year_columns_and_note_references_keep_distinct_meanings(self):
        tables = self.extract('0000034088-26-000045')
        self.assertEqual([t.structure['inventory']['resolved'] for t in tables], [60, 60, 105])
        self.assertTrue(all(t.structure['inventory'].get('unresolved', 0) == 0 for t in tables))
        notes = [cell for table in tables for row in table.structure['rows'] for cell in row
                 if cell['display_text'].isdigit() and any(''.join(header.split()) == 'NoteReferenceNumber'
                                                          for header in cell['column_headings'])]
        self.assertEqual(len(notes), 22)
        self.assertTrue(all(cell['classification'] == 'label' and not cell['fact_references'] for cell in notes))
        reference = next(ref for row in tables[2].structure['rows'] for cell in row
                         for ref in cell['fact_references'] if ref['xml_id'] == 'f-223')
        self.assertEqual(reference['period'], {'startDate': '2025-01-01', 'endDate': '2025-12-31'})
        self.assertEqual(reference['raw_value'], '29764000000')

    def test_micron_shared_year_heading_is_not_misclassified_as_instant(self):
        tables = self.extract('0000723125-26-000023')
        self.assertTrue(all(t.structure['inventory'].get('unresolved', 0) == 0 for t in tables))
        periods = {tuple(sorted(cell['column_period'].items())) for row in tables[0].structure['rows']
                   for cell in row if cell['classification'] == 'resolved'}
        expected = [{'startDate': '2025-08-29', 'endDate': '2026-09-03'},
                    {'startDate': '2024-08-30', 'endDate': '2025-08-28'},
                    {'startDate': '2023-09-01', 'endDate': '2024-08-29'}]
        self.assertEqual(periods, {tuple(sorted(period.items())) for period in expected})

    def test_split_constellation_cash_flow_is_not_silently_truncated(self):
        with self.assertRaisesRegex(TableExtractionError, 'cash_flow: expected one statement, found 0'):
            self.extract('0000016918-26-000040')

    def test_nike_supplemental_dividends_payable_uses_column_end(self):
        tables = self.extract('0000320187-26-000193')
        self.assertTrue(all(t.structure['inventory'].get('unresolved', 0) == 0 for t in tables))
        cells = [cell for row in tables[2].structure['rows'] for cell in row
                 if cell['row_heading'] == 'Dividends declared and not paid' and cell['fact_references']]
        self.assertEqual([cell['fact_references'][0]['raw_value'] for cell in cells], ['613000000', '594000000'])
        self.assertTrue(all(cell['fact_references'][0]['period'] == {'instant': cell['column_period']['endDate']}
                            for cell in cells))

    def test_delta_cash_reconciliation_uses_its_own_explicit_instant_headers(self):
        tables = self.extract('0000027904-26-000038')
        cash = tables[2]
        self.assertEqual(cash.structure['inventory']['resolved'], 46)
        self.assertEqual(cash.structure['inventory'].get('unresolved', 0), 0)
        [section] = cash.structure['cash_reconciliation_sections']
        self.assertEqual((section['header_start_row'], section['first_data_row']), (40, 43))
        self.assertTrue(all(cell['classification'] in {'header', 'blank'} for row in cash.structure['rows'][40:43] for cell in row))
        references = [ref for row in cash.structure['rows'][43:] for cell in row for ref in cell['fact_references']]
        self.assertEqual(len(references), 8)
        self.assertEqual({tuple(ref['period'].items()) for ref in references},
                         {(('instant', '2026-09-30'),), (('instant', '2025-09-30'),)})
        self.assertEqual({ref['raw_value'] for ref in references},
                         {'3787000000', '3791000000', '77000000', '97000000',
                          '18000000', '83000000', '3882000000', '3971000000'})

    def test_delta_reconciliation_rejects_a_changed_secondary_date(self):
        html_path, xml_path = self.paths('0000027904-26-000038')
        document = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
        table = select_statements(document)['cash_flow']
        rows = table.xpath('./tr|./tbody/tr|./thead/tr|./tfoot/tr')
        replacements = 0
        for node in rows[40].iter():
            if node.text and 'September' in node.text:
                node.text = node.text.replace('30', '29')
                replacements += 1
        self.assertEqual(replacements, 1)
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / 'filing.htm'
            changed.write_bytes(html.tostring(document, encoding='utf-8'))
            tables = extract_tables(changed, xml_path, **self.scope)
        unresolved = [cell for row in tables[2].structure['rows'] for cell in row if cell['classification'] == 'unresolved']
        self.assertEqual(len(unresolved), 8)
        self.assertTrue(all(cell['unresolved_reason'] == 'Fact period differs from column heading' for cell in unresolved))

    def test_delta_instants_need_the_explicit_reconciliation_boundary(self):
        html_path, xml_path = self.paths('0000027904-26-000038')
        document = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
        table = select_statements(document)['cash_flow']
        rows = table.xpath('./tr|./tbody/tr|./thead/tr|./tfoot/tr')
        for node in rows[39].iter():
            if node.text:
                node.text = node.text.replace('reconciliation of cash', 'discussion of cash')
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / 'filing.htm'
            changed.write_bytes(html.tostring(document, encoding='utf-8'))
            tables = extract_tables(changed, xml_path, **self.scope)
        self.assertNotIn('cash_reconciliation_sections', tables[2].structure)
        unresolved = [cell for row in tables[2].structure['rows'] for cell in row if cell['classification'] == 'unresolved']
        self.assertEqual(sum(cell['unresolved_reason'] == 'Unexpected instant in cash-flow row' for cell in unresolved), 8)

    def test_supplemental_instant_must_match_the_column_end_date(self):
        html_path, xml_path = self.paths('0000320187-26-000193')
        instance = etree.parse(str(xml_path), etree.XMLParser(resolve_entities=False, no_network=True))
        fact = instance.xpath('//*[@id="f-189"]')[0]
        context = instance.xpath('//*[@id="' + fact.get('contextRef') + '"]')[0]
        context.find('.//{http://www.xbrl.org/2003/instance}instant').text = '2026-08-30'
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / 'filing.xml'
            changed.write_bytes(etree.tostring(instance, encoding='utf-8'))
            tables = extract_tables(html_path, changed, **self.scope)
        failed = [cell for row in tables[2].structure['rows'] for cell in row
                  if cell['row_heading'] == 'Dividends declared and not paid' and cell['classification'] == 'unresolved']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['unresolved_reason'], 'Fact period differs from column heading')

    def test_supplemental_instant_rule_does_not_accept_an_unrelated_payable_concept(self):
        html_path, xml_path = self.paths('0000320187-26-000193')
        document = html.parse(str(html_path), parser=html.HTMLParser(encoding='utf-8', no_network=True))
        instance = etree.parse(str(xml_path), etree.XMLParser(resolve_entities=False, no_network=True))
        fact = instance.xpath('//*[@id="f-189"]')[0]
        namespace = etree.QName(fact).namespace
        fact.tag = '{' + namespace + '}AccountsPayableCurrent'
        document.xpath('//*[@id="f-189"]')[0].set('name', 'us-gaap:AccountsPayableCurrent')
        with tempfile.TemporaryDirectory() as directory:
            changed_html, changed_xml = Path(directory) / 'filing.htm', Path(directory) / 'filing.xml'
            changed_html.write_bytes(html.tostring(document, encoding='utf-8'))
            changed_xml.write_bytes(etree.tostring(instance, encoding='utf-8'))
            tables = extract_tables(changed_html, changed_xml, **self.scope)
        failed = [cell for row in tables[2].structure['rows'] for cell in row
                  if cell['row_heading'] == 'Dividends declared and not paid' and cell['classification'] == 'unresolved']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['unresolved_reason'], 'Unexpected instant in cash-flow row')


if __name__ == '__main__':
    unittest.main()
