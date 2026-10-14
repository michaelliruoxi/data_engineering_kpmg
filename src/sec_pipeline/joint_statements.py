"""Select reporting-parent statements from a source-proven combined filing."""

from collections import Counter
import json
import re

from .filing_sources import _dei, normalized_date
from .source_locators import source_xpath
from .statement_headings import statement_kind, normalized_heading


def _require(condition, message):
    from .tables import TableExtractionError
    if not condition:
        raise TableExtractionError(message)


def _words(value):
    return re.sub(r'[^a-z0-9]+', ' ', value.casefold()).strip()


def _dei_namespace(concept):
    return bool(re.fullmatch(r'\{https?://xbrl\.sec\.gov/dei/[0-9]{4}\}[A-Za-z]+', concept))


def _dei_value(fact):
    return ' '.join(fact['raw_value'].split())


def _primary_identity(tree, facts):
    ciks = [fact for fact in facts.values() if _dei_namespace(fact['concept'])
            and fact['concept'].endswith('}EntityCentralIndexKey')]
    if len({_dei_value(fact).zfill(10) for fact in ciks}) < 2:
        return None
    primary = [fact for fact in ciks if fact['dimensions'] == [] and re.fullmatch(r'[0-9]{1,10}', _dei_value(fact))
               and fact.get('entity', {}).get('identifier', '').zfill(10) == _dei_value(fact).zfill(10)
               and fact['entity'].get('scheme') in {'http://www.sec.gov/CIK', 'https://www.sec.gov/CIK'}]
    unique = {_dei_value(fact).zfill(10) for fact in primary}
    _require(len(unique) == 1, 'Joint statements require a unique undimensioned reporting registrant.')
    cik = next(iter(unique))
    contexts = {fact['context_ref'] for fact in primary}
    fields = {}
    ids = {}
    for fact in facts.values():
        if fact['context_ref'] in contexts and _dei_namespace(fact['concept']):
            name = fact['concept'].split('}', 1)[-1]
            fields.setdefault(name, set()).add(_dei_value(fact))
            ids.setdefault(name, []).append(fact['xml_id'])
    _require(len(fields.get('EntityRegistrantName', [])) == 1
             and len(fields.get('DocumentType', [])) == 1
             and len(fields.get('DocumentPeriodEndDate', [])) == 1,
             'Reporting registrant DEI name, form or period is ambiguous.')
    inline = _dei(tree.getroot(), expected_cik=cik, inline=True)
    for field in ('EntityRegistrantName', 'DocumentType', 'DocumentPeriodEndDate'):
        normalize = normalized_date if field == 'DocumentPeriodEndDate' else str.casefold
        _require({normalize(value) for value in fields[field]} == {normalize(value) for value in inline.get(field, [])},
                 'Reporting registrant XML and inline DEI identities disagree.')
    names, secondary_members = {}, set()
    for fact in ciks:
        _require(re.fullmatch(r'[0-9]{1,10}', _dei_value(fact)), 'Invalid combined-filing DEI CIK.')
        registrant = _dei_value(fact).zfill(10)
        values = {_dei_value(item) for item in facts.values() if item['context_ref'] == fact['context_ref']
                  and _dei_namespace(item['concept']) and item['concept'].endswith('}EntityRegistrantName')}
        _require(len(values) == 1, 'Combined-filing registrant name is ambiguous.')
        name = next(iter(values))
        _require(registrant not in names or names[registrant] == name, 'Combined-filing registrant names conflict.')
        names[registrant] = name
        if registrant != cik:
            dimensions = fact['dimensions']
            _require(fact.get('entity', {}).get('identifier', '').zfill(10) == cik and len(dimensions) == 1
                     and dimensions[0]['kind'] == 'explicit'
                     and re.fullmatch(r'\{https?://xbrl\.sec\.gov/dei/[0-9]{4}\}LegalEntityAxis', dimensions[0]['dimension']),
                     'A combined-filing subsidiary lacks its original DEI legal entity scope.')
            secondary_members.add(dimensions[0]['member'])
    return {'cik': cik, 'name': names[cik], 'entity': dict(primary[0]['entity']), 'names': names,
            'secondary_members': secondary_members, 'dei_context_refs': sorted(contexts), 'dei_xml_ids': {key: sorted(ids[key]) for key in
                ('EntityCentralIndexKey', 'EntityRegistrantName', 'DocumentType', 'DocumentPeriodEndDate')}}


def _company(value, names):
    value = _words(value)
    for cik, name in names.items():
        candidate = _words(name)
        if value == candidate or value in {candidate + ' and subsidiaries', candidate + ' and subsidiary companies'}:
            return cik
    return None


def _header_metadata(value):
    from .statement_headings import _metadata, _MONTH
    if _metadata(value):
        return True
    value = _words(value)
    return bool(re.fullmatch(r'(?:unaudited|audited)(?: in)? (?:millions|thousands|billions)(?: except per (?:share|unit) amounts)?', value)
                or re.fullmatch(r'(?:millions|thousands|billions) of dollars(?: except share data| shares in thousands)?', value)
                or re.fullmatch(r'(?:quarter and )?year to date(?: ended)? [a-z]+ [0-9]{1,2} [0-9]{4}(?: and [0-9]{4})?', value)
                or re.fullmatch(_MONTH + r' [0-9]{1,2} [0-9]{4} and ' + _MONTH + r' [0-9]{1,2} [0-9]{4}', value)
                or value == 'supplemental disclosure of cash flow information in millions'
                or value == 'the following table provides a reconciliation of cash cash equivalents and restricted cash reported on the balance sheet to the amount shown in the statements of cash flows')


def _headings(table, tree, identity):
    """Use only this table's headers or bounded preceding header-only blocks."""
    from .tables import _hidden, layout_grid, local, text
    # A financial note's text block is not a primary statement section.
    if any(local(node) in {'nonnumeric', 'continuation'} for node in table.iterancestors()):
        return None
    rows, _ = layout_grid(table, tree)
    blocks = []
    for row in rows[:24]:
        if any(local(node) == 'nonfraction' for cell in row for node in cell['_element'].iter()):
            break
        blocks.extend(cell['_element'] for cell in row if cell['display_text'])
    if any(re.search(r'\b(?:parent company|parent only|unconsolidated|selected financial|note\s*[0-9]|notes to)\b',
                     _words(re.sub(r'([a-z])([A-Z])', r'\1 \2', text(block))))
           for block in blocks):
        return None
    node, count = table, 0
    for _ in range(4):
        kinds = {statement_kind(text(block)) for block in blocks} - {None}
        companies = {_company(text(block), identity['names']) for block in blocks} - {None}
        if kinds and companies:
            break
        previous = node.getprevious()
        barrier, empty = False, 0
        while previous is not None and count < 12:
            value = text(previous).strip(' \u200b')
            if value:
                if (len(value) > 500 or any(local(child) == 'nonfraction' and not _hidden(child) for child in previous.iter())
                        or previous.xpath('.//table') or local(previous) == 'table'):
                    barrier = True
                    break
                blocks.append(previous)
                count += 1
                # A complete nearby registrant/title pair does not need older text.
                kinds = {statement_kind(text(block)) for block in blocks} - {None}
                companies = {_company(text(block), identity['names']) for block in blocks} - {None}
                if kinds and companies:
                    break
                if not statement_kind(value) and not _company(value, identity['names']) and not _header_metadata(value):
                    barrier = True
                    break
                if re.search(r'\bnotes?\s+(?:to|\d)|^item\s+\d|^part\s+[ivx]+', value, re.I):
                    barrier = True
                    break
            else:
                empty += 1
                if empty > 128:
                    barrier = True
                    break
            previous = previous.getprevious()
        kinds = {statement_kind(text(block)) for block in blocks} - {None}
        companies = {_company(text(block), identity['names']) for block in blocks} - {None}
        if kinds and companies:
            break
        if barrier or count >= 12:
            break
        node = node.getparent()
        if node is None or local(node) in {'body', 'html', 'td', 'th', 'table'}:
            break
    kinds = {statement_kind(text(block)) for block in blocks} - {None}
    companies = {_company(text(block), identity['names']) for block in blocks} - {None}
    if len(kinds) != 1 or companies != {identity['cik']}:
        return None
    kind = next(iter(kinds))
    titles = [block for block in blocks if statement_kind(text(block)) == kind]
    company = next(block for block in blocks if _company(text(block), identity['names']) == identity['cik'])
    title = titles[0]
    return {'kind': kind, 'heading': normalized_heading(text(title)),
            'continued': bool(re.search(r'\bcontinued\s*\)?\s*$', text(title), re.I)),
            'title_source': {'source_xpath': source_xpath(tree, title), 'display_text': text(title)},
            'registrant_source': {'source_xpath': source_xpath(tree, company), 'display_text': text(company)}}


def _candidate(table, tree, facts, html_ids, identity, heading):
    from .tables import _column_periods, _STATEMENT_CONCEPTS, column_headers, layout_grid, local, _hidden, verify_inline
    from .joint_statement_periods import additional_gaap_group, resolve_ytd_period
    rows, _ = layout_grid(table, tree)
    first = next((index for index, row in enumerate(rows) if any(local(node) == 'nonfraction'
                 for cell in row for node in cell['_element'].iter())), None)
    if first is None:
        return None
    periods, _ = _column_periods(rows, first, facts, {}, html_ids, advanced=True)
    columns, spanning, refs, evidence = {}, {}, {}, {}
    for row in rows[first:]:
        for cell in row:
            for node in cell['_element'].iter():
                if local(node) != 'nonfraction' or _hidden(node):
                    continue
                fact = facts.get(node.get('id'))
                _require(fact is not None and html_ids.get(node.get('id')) == 1, 'Joint statement has ambiguous inline/XML evidence.')
                verify_inline(node, fact, {})
                _require(fact.get('entity') == identity['entity'], 'A primary statement has a different source entity.')
                if any(dimension.get('dimension', '').endswith(('}LegalEntityAxis', '}ConsolidatedEntitiesAxis'))
                       and dimension.get('member') in identity['secondary_members']
                       for dimension in fact['dimensions']):
                    return None
                if cell['column'] > 0:
                    columns.setdefault(cell['column'], []).append(fact)
                    for column in range(cell['column'], cell['column'] + cell['colspan']):
                        spanning.setdefault(column, []).append(fact)
    period_evidence = {}
    for column, values in columns.items():
        if periods.get(column) is None:
            values = spanning[column]
            durations = {tuple(sorted(fact['period'].items())) for fact in values if set(fact['period']) == {'startDate', 'endDate'}}
            concepts = {fact['concept'] for fact in values if set(fact['period']) == {'startDate', 'endDate'}}
            headers = column_headers(rows, first, column)
            # A YTD caption can occupy the row-label column, above all years.
            shared = [cell for row in rows[:first] for cell in row if cell['column'] == 0
                      and re.search(r'\byear[ -]+to[ -]+date\b', cell['display_text'], re.I)]
            values_header = [*headers, *(cell['display_text'] for cell in shared)]
            resolved = resolve_ytd_period(values_header, dict(next(iter(durations)))) if len(durations) == 1 and len(concepts) >= 2 else None
            if resolved:
                periods[column] = resolved
                period_evidence[str(column)] = {'method': 'source_correlated_year_to_date', 'headers': values_header,
                    'xml_ids': sorted({fact['xml_id'] for fact in values if fact['period'] == resolved})}
    axes = []
    scopes = set()
    unresolved = False
    for column, values in columns.items():
        period = periods.get(column)
        if period is None:
            unresolved = True
            continue
        axis = json.dumps(period, sort_keys=True)
        if axis not in axes:
            axes.append(axis); evidence[axis] = {}; refs[axis] = []
        for fact in values:
            refs[axis].append(fact)
            if fact['dimensions'] or fact['is_nil']:
                continue
            if not re.fullmatch(r'\{https?://(?:fasb\.org|xbrl\.us)/us-gaap/[^}]+\}[^}]+', fact['concept']):
                continue
            groups = [index for index, names in enumerate(_STATEMENT_CONCEPTS[heading['kind']])
                      if fact['concept'].split('}', 1)[-1] in names]
            alias = additional_gaap_group(fact['concept'])
            if alias and alias[0] == heading['kind']:
                groups.append(alias[1])
            for group in groups:
                wanted = {'instant': period.get('endDate')} if heading['kind'] == 'cash_flow' and group == 3 else period
                if fact['period'] == wanted:
                    evidence[axis].setdefault(str(group), []).append(fact['xml_id'])
                    scopes.add(json.dumps({'entity': fact['entity'], 'unit': fact['unit'], 'dimensions': []}, sort_keys=True))
    return {'table': table, 'heading': heading, 'axes': axes, 'evidence': evidence, 'references': refs,
            'periods': {str(column): periods[column] for column in columns if periods.get(column) is not None},
            'period_evidence': period_evidence, 'unresolved': unresolved, 'scopes': scopes}


def select_joint_statement_segments(tree, facts):
    """Return deterministic parent groups, or None for an ordinary filing."""
    from .tables import _segment_facts, _STATEMENT_CONCEPTS, _hidden, local, _segments_adjacent, StatementSegment
    facts = _segment_facts(facts)
    identity = _primary_identity(tree, facts)
    if identity is None:
        return None
    html_ids = Counter(node.get('id') for node in tree.iter() if local(node) == 'nonfraction' and node.get('id'))
    choices = {kind: [] for kind in _STATEMENT_CONCEPTS}
    for table in tree.xpath('//table'):
        if _hidden(table) or table.xpath('.//table'):
            continue
        heading = _headings(table, tree, identity)
        if heading is None:
            continue
        candidate = _candidate(table, tree, facts, html_ids, identity, heading)
        if candidate is not None:
            choices[heading['kind']].append(candidate)
    output = []
    for kind, candidates in choices.items():
        required = {str(index) for index in range(len(_STATEMENT_CONCEPTS[kind]))}
        complete = [candidate for candidate in candidates if candidate['axes'] and not candidate['unresolved']
                    and all(set(groups) == required for groups in candidate['evidence'].values())]
        selected = []
        if len(complete) == 1:
            selected = list(complete)
            if kind == 'cash_flow':
                selected += [candidate for candidate in candidates if candidate is not complete[0]
                             and candidate['heading']['continued'] and not candidate['unresolved']]
        elif kind == 'balance_sheet' and len(candidates) == 2 and not complete:
            first, second = candidates
            _require(first['axes'] == second['axes'] and first['axes'] and not first['unresolved'] and not second['unresolved']
                     and all(set(groups) == {'0'} for groups in first['evidence'].values())
                     and all(set(groups) == {'1', '2'} for groups in second['evidence'].values())
                     and all(set(json.loads(axis)) == {'instant'} for axis in first['axes']),
                     'Joint balance segments do not establish assets and liabilities/equity for the same dates.')
            selected = candidates
        _require(bool(selected) and len(complete) <= 1, f'{kind}: joint filing has no unique complete reporting-parent statement.')
        scopes = set().union(*(candidate['scopes'] for candidate in selected))
        _require(len(scopes) == 1, 'Joint statement core facts disagree on source entity or currency.')
        scope = json.loads(next(iter(scopes)))
        _require(scope['unit']['denominator'] == [] and len(scope['unit']['numerator']) == 1
                 and re.fullmatch(r'\{http://www.xbrl.org/2003/iso4217\}[A-Z]{3}', scope['unit']['numerator'][0]),
                 'Joint statement core facts require one source reporting currency.')
        if kind == 'income_statement':
            _require(all(candidate is selected[0] or not any('0' in groups for groups in candidate['evidence'].values())
                         for candidate in candidates), 'Competing parent income statements cannot be discarded.')
        for previous, candidate in zip(selected, selected[1:]):
            _require(previous['heading']['heading'] == candidate['heading']['heading']
                     and _segments_adjacent(tree, previous['table'], candidate['table'], kind),
                     'Joint statement segments disagree on heading or original document adjacency.')
        continuation_proofs = {}
        if kind == 'cash_flow':
            from .joint_cash_continuation import validate_cash_continuation
            for index, candidate in enumerate(selected[1:], 1):
                continuation_proofs[index] = validate_cash_continuation(selected[0], candidate)
        combined = {}
        for candidate in selected:
            for axis, groups in candidate['evidence'].items():
                for group, ids in groups.items():
                    combined.setdefault(axis, {}).setdefault(group, []).extend(ids)
        keys = [kind] if len(selected) == 1 else [f'{kind}.segment_{index + 1}' for index in range(len(selected))]
        for index, (key, candidate) in enumerate(zip(keys, selected)):
            group = {'group_key': kind, 'segment_count': len(selected), 'segment_keys': keys, 'segment_index': index + 1,
                     'source_xpaths': [source_xpath(tree, value['table']) for value in selected],
                     'heading': candidate['heading']['heading'], 'column_periods': [json.loads(axis) for axis in selected[0]['axes']],
                     'scope': scope,
                     'combined_concept_evidence': [{'period': json.loads(axis), 'groups': {name: sorted(set(ids)) for name, ids in sorted(groups.items())}}
                                                   for axis, groups in combined.items()],
                     'joint_statement': {'registrant_cik': identity['cik'], 'registrant_name': identity['name'],
                         'context_entity': identity['entity'], 'dei_context_refs': identity['dei_context_refs'],
                         'dei_xml_ids': identity['dei_xml_ids'], 'registrant_source': candidate['heading']['registrant_source'],
                         'title_source': candidate['heading']['title_source'], 'column_periods': candidate['periods'],
                         'period_evidence': candidate['period_evidence']}}
            if index in continuation_proofs:
                proof = continuation_proofs[index]
                group['segment_role'] = proof['segment_role']
                group['joint_cash_continuation'] = proof
            output.append(StatementSegment(kind, key, candidate['table'], group))
    return tuple(output)


def validate_joint_cells(table, segment, tree, facts):
    """Recheck stored parent columns and inline identities against original DOM."""
    from .tables import column_headers, layout_grid, local, _hidden
    rows, _ = layout_grid(segment.element, tree)
    first = next(index for index, row in enumerate(rows) if any(local(node) == 'nonfraction'
                 for cell in row for node in cell['_element'].iter()))
    original = {cell['cell_id']: (cell, row[0]['display_text']) for row in rows for cell in row}
    periods = segment.group['joint_statement']['column_periods']
    for row in table.structure['rows']:
        for cell in row:
            anchor = original.get(cell['cell_id'])
            _require(anchor is not None, 'Joint statement cell has no original source anchor.')
            source, row_heading = anchor
            _require(cell.get('row_heading') == row_heading, 'Joint statement row label differs from its original source.')
            period = periods.get(str(source['column']))
            if period is not None:
                _require(cell.get('column_period') == period
                         and cell.get('column_headings') == column_headers(rows, first, source['column']),
                         'Joint statement column period or headings differ from source proof.')
            nodes = [node for node in source['_element'].iter() if local(node) == 'nonfraction' and not _hidden(node)]
            if nodes:
                _require(cell.get('classification') in {'resolved', 'resolved_label', 'unresolved'},
                         'A joint statement numeric source cannot be reclassified as non-financial.')
            if nodes and cell.get('classification') in {'resolved', 'resolved_label'}:
                refs = cell.get('fact_references', [])
                _require([ref.get('xml_id') for ref in refs] == [node.get('id') for node in nodes],
                         'Joint statement cell substitutes or omits its own source occurrence.')
                for ref, node in zip(refs, nodes):
                    fact = facts[node.get('id')]
                    _require(ref.get('method') == 'inline_id' and all(ref.get(key) == fact[key] for key in
                        ('concept', 'context_ref', 'unit_ref', 'unit', 'period', 'dimensions', 'raw_value', 'is_nil')),
                        'Joint statement reference differs from its original XML occurrence.')
