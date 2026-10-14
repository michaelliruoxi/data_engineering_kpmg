"""The V4 runner commits one complete source set or rolls every write back."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from sec_pipeline import filing_ingestion_v4 as runner
from sec_pipeline.database import ChunkInput, ReportInput
from sec_pipeline.filing_parser_v4 import COVERAGE, FilingSetValidation, ParsedDocument, ParsedFilingSet
from sec_pipeline.processing import ProcessingError
from tests.test_v4_table_cells import conflict_fixture


class LedgerConnection:
    """Transactional in-memory recorder; no connector or SQL write is used."""

    def __init__(self):
        self.closed = self.autocommit = False
        self.info = SimpleNamespace(transaction_status=SimpleNamespace(name='IDLE'))
        self.state = {}
        self.commits = self.rollbacks = 0
        self.queries = []
        self.corrupt = None

    @contextmanager
    def transaction(self):
        assert self.info.transaction_status.name == 'IDLE'
        before = deepcopy(self.state)
        self.info.transaction_status.name = 'INTRANS'
        try:
            yield
        except BaseException:
            self.state = before
            self.rollbacks += 1
            raise
        else:
            self.commits += 1
        finally:
            self.info.transaction_status.name = 'IDLE'

    def put(self, key, value):
        assert self.info.transaction_status.name == 'INTRANS'
        assert key not in self.state or self.state[key] == value
        self.state[key] = deepcopy(value)

    def execute(self, sql, parameters):
        assert self.info.transaction_status.name == 'INTRANS'
        self.queries.append(sql)
        if 'FROM sec.source_documents ' in sql:
            rows = self.state['sources']
        elif 'FROM sec.financial_facts ' in sql:
            rows = list(self.state['facts'].items())
        elif 'FROM sec.fact_source_occurrences ' in sql:
            rows = self.state['occurrences']
        elif 'FROM sec.reports ' in sql:
            rows = [(self.state['reports'][parameters[1]],)]
        elif 'FROM sec.chunks ' in sql:
            rows = [(item,) for item in self.state['chunks'][parameters[1]]]
        elif 'FROM sec.financial_tables ' in sql:
            rows = [(item,) for item in self.state.get(('tables', parameters[1]), [])]
        elif 'FROM sec.source_quality_issues ' in sql:
            rows = [(item,) for item in self.state['issues'].values()]
        elif 'FROM sec.source_processing_sets ' in sql:
            rows = [self.state['seal']]
        else:
            raise AssertionError(sql)
        rows = deepcopy(rows)
        if self.corrupt and self.corrupt in sql:
            rows = []
        return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: rows[0] if rows else None)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    filing, xml, primary, extra, index, schema = [UUID(int=value) for value in (1, 3, 2, 4, 5, 6)]
    roles = [('index.html', 'filing_index', index), ('main.htm', 'main_inline_xbrl_filing', primary),
             ('instance.xml', 'extracted_xbrl_instance', xml), ('schema.xsd', 'xbrl_schema', schema),
             ('extra.htm', runner.SUPPLEMENTAL_ROLE, extra)]
    sources = {}
    records = []
    for ordinal, (filename, role, source_id) in enumerate(roles):
        url = 'https://www.sec.gov/Archives/edgar/data/1/000000000126000001/' + filename
        sources[url] = source_id
        record = dict(role=role, source_url=url, local_path=filename, sha256=str(ordinal + 1) * 64,
                      size_bytes=100 + ordinal, retrieved_at_utc='2026-10-13T10:00:00+00:00')
        if source_id in (primary, extra):
            record.update(is_primary=source_id == primary, sequence=ordinal,
                          document_type='10-Q' if source_id == primary else 'EX-99.1',
                          index_locator={'source_xpath': f'/html/body/table/tr[{ordinal}]', 'source_sha256': '1' * 64})
        records.append(record)
    manifest = dict(schema_version=2, status='verified', accession_number='0000000001-26-000001',
                    core_manifest={'local_path': 'source_manifest.json', 'sha256': 'a' * 64},
                    index_document=records[0], files=records[1:], document_set={
                        'set_key': 'primary-inline-set', 'policy_version': 'inline-set-v1', 'inline_target': '',
                        'target_xml_source_url': records[2]['source_url'], 'primary_html_source_url': records[1]['source_url'],
                        'member_source_urls': [records[1]['source_url'], records[4]['source_url']],
                        'excluded_inline_documents': [], 'source_checks': dict.fromkeys(
                            ('index_membership_complete', 'single_primary_target', 'excluded_targets_separate'), True)})
    path = tmp_path / 'source_document_set_manifest.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    table, fact, _ = conflict_fixture(filing, extra, xml)
    documents = []
    for ordinal, record in enumerate((records[1], records[4])):
        identifier = sources[record['source_url']]
        placeholder = UUID(int=100 + ordinal)
        report = ReportInput(filing_id=filing, source_document_id=identifier, extraction_version='report-v1',
                             full_text='Original text', sections=[{'section_id': 'original-section'}])
        chunk = ChunkInput(filing_id=filing, report_id=placeholder, chunking_version='paragraph-v1', chunk_index=0,
                           text='Original text', offset_start=0, offset_end=13, section='original-section',
                           metadata={'source_xpath': '/html/body/p', 'section_id': 'original-section'})
        documents.append(ParsedDocument(identifier, record['source_url'], record['local_path'], record['sha256'],
                                        record['is_primary'], placeholder, report, (chunk,), (table,) if ordinal else ()))
    evidence = dict(xml_id='F1', concept='{http://fasb.org/us-gaap/2026}DividendsPayableCurrentAndNoncurrent',
                    context_ref='c1', unit_ref='usd', unit=fact.unit, period={'instant': '2026-06-30'}, dimensions=[],
                    raw_value='24000000', is_nil=False, inline_mapped_value='24000000', inline_is_nil=False,
                    html_sha256=records[4]['sha256'], xml_sha256=records[2]['sha256'])
    occurrence = dict(inline_document_id=str(extra), inline_id='F1', source_xpath='/html/body/table/tr/td[2]/ix:nonfraction',
                      evidence=evidence, fragments=())
    issues = tuple(table.structure['rows'][0][1]['quality_issues'])
    parsed = ParsedFilingSet(filing, xml, json.dumps(manifest), digest, (fact,), (occurrence,), tuple(documents),
                            dict.fromkeys(runner.REQUIRED_SOURCE_CHECKS, True), {'period_header_consistency': False},
                            issues, dict(COVERAGE), {}, {}, ({'cik': '0000000001', 'name': 'Original Registrant'},))
    connection = LedgerConnection()
    calls = []

    def seed(conn, *args):
        conn.put('metadata', filing)
        conn.put('sources', [(sources[item['source_url']], filing, item['role'], item['source_url'], item['sha256'], item['size_bytes'])
                             for item in records])
        return {'filing_id': filing, 'document_ids': {item['local_path']: sources[item['source_url']] for item in records[:-1]}}

    def supplemental(conn, record):
        calls.append(('supplemental', record))
        conn.put('supplemental', record)
        return extra

    def company(conn, record):
        conn.put('company', record)

    def document_set(conn, record, members):
        conn.put('document_set', (record, members))
        return UUID(int=20)

    def facts(conn, items, **kwargs):
        conn.put('facts_payload', tuple(items))
        conn.put('facts', {'F1': UUID(int=30)})
        return SimpleNamespace(fact_ids=conn.state['facts'])

    def occurrences(conn, items):
        conn.put('occurrences_payload', tuple(items))
        conn.put('occurrences', [('F1', UUID(int=31), items[0].fact_id, items[0].html_document_id)])
        return [UUID(int=31)]

    def report(conn, item):
        identifier = UUID(int=200 + item.source_document_id.int)
        values = conn.state.get('reports', {}) | {item.source_document_id: identifier}
        conn.state['reports'] = values
        conn.put(('report_payload', identifier), item)
        return identifier

    def chunks(conn, items, **kwargs):
        items = tuple(items)
        identifier = UUID(int=300 + items[0].report_id.int)
        conn.state['chunks'] = conn.state.get('chunks', {}) | {items[0].report_id: [identifier]}
        conn.put(('chunk_payload', identifier), items)
        return [identifier]

    def table_write(conn, item):
        conn.put(('tables', item.source_document_id), [UUID(int=40)])
        conn.put('table_payload', item)
        return UUID(int=40)

    def links(conn, target, items, keys, **kwargs):
        conn.put('accepted_links', list(items))
        return []

    def cells(conn, *args, **kwargs):
        ids = {item['issue_key']: UUID(int=50 + ordinal) for ordinal, item in enumerate(parsed.issues)}
        conn.put('issues', ids)
        conn.put('cells', args[1])
        return SimpleNamespace(issue_ids=ids)

    def validate(actual, *args, **kwargs):
        calls.append(('validate', deepcopy(connection.state)))
        assert kwargs == {'filing_id': filing, 'source_document_ids': sources}
        return FilingSetValidation(dict(actual.checks), dict(actual.semantic_checks), actual.issues, actual.counts)

    def seal(conn, record, **kwargs):
        conn.put('processing_memberships', kwargs)
        conn.put('processing_record', record)
        conn.put('seal', (UUID(int=20), 'passed', 'review_required', runner._expected_counts(parsed), record.configuration))
        return UUID(int=60)

    for name, function in dict(seed_manifest=seed, register_source_document=supplemental, register_company=company,
                               store_document_set=document_set, store_facts=facts, store_source_occurrences=occurrences,
                               store_report=report, store_chunks=chunks, store_financial_table=table_write, store_many=links,
                               store_v4_table_cells=cells, validate_filing_set=validate, store_v4_processing_set=seal).items():
        monkeypatch.setattr(runner, name, function)
    monkeypatch.setattr(runner, 'verify_document_set', lambda *args: deepcopy(manifest))
    monkeypatch.setattr(runner, 'parse_filing_set', lambda *args, **kwargs: parsed)
    return SimpleNamespace(conn=connection, parsed=parsed, path=path, root=tmp_path, manifest=manifest,
                           sources=sources, calls=calls, digest=digest)


def ingest(scenario, **kwargs):
    return runner.run_ingestion(scenario.conn, manifest_path=scenario.path, data_root=scenario.root, **kwargs)


def test_complete_set_commits_only_after_reconstruction_and_replays_exact_ids(scenario):
    result = ingest(scenario)
    assert scenario.conn.commits == 1 and scenario.conn.rollbacks == 0
    assert scenario.conn.info.transaction_status.name == 'IDLE'
    before = deepcopy(scenario.conn.state)
    assert ingest(scenario) == result
    assert scenario.conn.state == before and scenario.conn.commits == 2
    validation_state = next(value for kind, value in scenario.calls if kind == 'validate')
    assert {'metadata', 'company', 'supplemental', 'document_set', 'facts', 'occurrences', 'reports', 'chunks', 'cells', 'issues'} <= validation_state.keys()
    assert 'processing_record' not in validation_state
    assert result['source_validation_status'] == 'passed' and result['validation_status'] == 'review_required'
    assert result['validation']['passed'] is False and result['semantic_checks']['period_header_consistency'] is False
    assert result['coverage'] == COVERAGE and result['counts']['quality_issues'] == 1
    assert result['counts']['inline_documents'] == result['counts']['reports'] == 2
    assert result['counts']['accepted_cells'] == 0 and result['counts']['review_required_cells'] == 1
    assert result['source_hashes'] == {item['local_path']: item['sha256'] for item in [scenario.manifest['index_document'], *scenario.manifest['files']]}
    assert result['configuration']['document_set_manifest_sha256'] == scenario.digest
    assert set(result['source_checks']) == set(runner.REQUIRED_SOURCE_CHECKS) and len(result['source_checks']) == 14
    assert scenario.conn.state['accepted_links'] == []
    assert not any('preferred' in query or 'selection' in query for query in scenario.conn.queries)


def test_chunks_keep_original_text_sections_and_use_actual_replayed_report_ids(scenario):
    ingest(scenario)
    for document in scenario.parsed.documents:
        report_id = scenario.conn.state['reports'][document.source_document_id]
        chunk_id = scenario.conn.state['chunks'][report_id][0]
        [stored] = scenario.conn.state[('chunk_payload', chunk_id)]
        assert report_id != document.report_placeholder_id and stored.report_id == report_id
        assert replace(stored, report_id=document.report_placeholder_id) == document.chunks[0]
        assert stored.section_id is None
    [stored_occurrence] = scenario.conn.state['occurrences_payload']
    assert stored_occurrence.evidence == scenario.parsed.occurrences[0]['evidence']
    assert str(stored_occurrence.html_document_id) == scenario.parsed.occurrences[0]['inline_document_id']


@pytest.mark.parametrize('phase', ['reconstruction', 'final_source', 'sealed_counts', 'stored_occurrences'])
def test_late_failure_rolls_back_every_layer(scenario, monkeypatch, phase):
    if phase == 'reconstruction':
        def fail(*args, **kwargs):
            assert 'cells' in scenario.conn.state
            raise ValueError('Source changed during final reconstruction')
        monkeypatch.setattr(runner, 'validate_filing_set', fail)
    elif phase == 'final_source':
        calls = iter([deepcopy(scenario.manifest), {}])
        monkeypatch.setattr(runner, 'verify_document_set', lambda *args: next(calls))
    elif phase == 'sealed_counts':
        original = runner.store_v4_processing_set
        def seal(*args, **kwargs):
            result = original(*args, **kwargs)
            scenario.conn.state['seal'][3]['facts'] += 1
            return result
        monkeypatch.setattr(runner, 'store_v4_processing_set', seal)
    else:
        scenario.conn.corrupt = 'FROM sec.fact_source_occurrences '
    with pytest.raises(ProcessingError):
        ingest(scenario)
    assert scenario.conn.state == {} and scenario.conn.commits == 0 and scenario.conn.rollbacks == 1
    assert scenario.conn.info.transaction_status.name == 'IDLE'


def test_replay_failure_preserves_every_preexisting_row(scenario, monkeypatch):
    ingest(scenario)
    before = deepcopy(scenario.conn.state)
    monkeypatch.setattr(runner, 'validate_filing_set', lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('fail replay')))
    with pytest.raises(ProcessingError):
        ingest(scenario)
    assert scenario.conn.state == before and scenario.conn.commits == scenario.conn.rollbacks == 1


@pytest.mark.parametrize('condition', ['autocommit', 'active', 'closed'])
def test_connection_must_be_idle_and_owned_before_any_metadata(scenario, condition):
    if condition == 'active':
        scenario.conn.info.transaction_status.name = 'INTRANS'
    else:
        setattr(scenario.conn, condition, True)
    with pytest.raises(ProcessingError, match='idle open connection'):
        ingest(scenario)
    assert scenario.conn.state == {} and scenario.conn.commits == scenario.conn.rollbacks == 0


def test_source_check_and_coverage_claims_cannot_be_promoted(scenario, monkeypatch):
    with pytest.raises(ProcessingError, match='Coverage'):
        ingest(scenario, coverage={**COVERAGE, 'tables': 'complete'})
    changed = replace(scenario.parsed, checks={**scenario.parsed.checks, 'fresh_source_reconstruction': False})
    monkeypatch.setattr(runner, 'parse_filing_set', lambda *args, **kwargs: changed)
    with pytest.raises(ProcessingError, match='required V4 source check'):
        ingest(scenario)
    assert scenario.conn.state == {} and scenario.conn.rollbacks == 2


def test_dry_run_keeps_review_status_and_does_not_need_inventory_source_ids(scenario):
    assert scenario.parsed.source_inventory == {}
    result = runner.run_dry_run(scenario.path, scenario.root)
    assert result['mode'] == 'dry_run' and result['validation']['passed'] is False
    assert result['source_validation_status'] == 'passed' and result['coverage']['reports'] == 'unknown'
    assert scenario.conn.state == {} and scenario.conn.commits == scenario.conn.rollbacks == 0


def test_rebind_rejects_wrong_placeholder_and_preassigned_section(scenario):
    document = scenario.parsed.documents[0]
    for chunk in (replace(document.chunks[0], report_id=UUID(int=99)), replace(document.chunks[0], section_id=UUID(int=99))):
        with pytest.raises(ProcessingError, match='placeholder'):
            runner.rebind_chunks(replace(document, chunks=(chunk,)), UUID(int=200))


def test_configuration_is_independent_of_mutable_manifest(scenario):
    configuration = runner.configuration_from_manifest(scenario.manifest, scenario.digest)
    scenario.manifest['document_set']['member_source_urls'].clear()
    assert len(configuration['document_set']['member_source_urls']) == 2
