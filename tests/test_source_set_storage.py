"""V4 source storage validation/replay without database connections by default."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import re
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from sec_pipeline.database import DataConflict, DatabaseError
from sec_pipeline.source_set_storage import (
    SOURCE_CHECKS, DocumentMemberInput, DocumentSetInput, FactSourceOccurrenceInput,
    QualityIssueInput, ReportMembershipInput, ReviewBindingInput, V4ProcessingSetInput,
    store_document_set, store_quality_issues, store_review_bindings, store_source_occurrences, store_v4_processing_set,
)


class Recorder:
    autocommit = False

    def __init__(self):
        self.tables = {}
        self.queries = []
        self.pipeline_sizes = []

    @contextmanager
    def pipeline(self):
        start=len(self.queries)
        yield
        self.pipeline_sizes.append(len(self.queries)-start)

    @contextmanager
    def transaction(self):
        snapshot = deepcopy(self.tables)
        try:
            yield
        except Exception:
            self.tables = snapshot
            raise

    def execute(self, query, parameters):
        query = query.as_string() if hasattr(query, 'as_string') else query
        values = [value.obj if isinstance(value, Jsonb) else value for value in parameters]
        self.queries.append(query)
        rows = []
        if 'lock_processing_filing' in query:
            pass
        elif query.startswith('INSERT'):
            match = re.search(r'INTO sec\."?(\w+)"? \((.+)\) VALUES', query)
            table, columns = match[1], re.findall(r'"(\w+)"', match[2])
            item = dict(zip(columns, deepcopy(values)))
            item.setdefault('id', uuid4())
            conflict = re.search(r'ON CONFLICT \((.*?)\)',query)
            keys = re.findall(r'"(\w+)"',conflict[1]) if conflict else []
            existing = [row for row in self.tables.get(table,[]) if keys and all(row[key]==item[key] for key in keys)]
            if not existing:
                self.tables.setdefault(table, []).append(item)
                rows = [(item['id'],)]
        elif query.startswith('SELECT id'):
            table = re.search(r'FROM sec\."?(\w+)"?', query)[1]
            columns = (['id'] if 'WHERE id=%s' in query else []) + re.findall(r'"(\w+)" (?:IS NOT DISTINCT FROM|=) %s', query)
            rows = [(row['id'],) for row in self.tables.get(table, [])
                    if all(row.get(column) == value for column, value in zip(columns, values))]
        elif query.startswith('SELECT'):
            match = re.search(r'SELECT "?(\w+)"? FROM sec\."?(\w+)"? WHERE (\w+)=%s', query)
            column, table, key = match.groups()
            rows = [(row[column],) for row in self.tables.get(table, []) if row[key] == values[0]]
        else:
            raise AssertionError(query)
        return SimpleNamespace(fetchone=lambda: rows[0] if rows else None, fetchall=lambda: rows)

    def commit(self):
        raise AssertionError('The caller owns commit.')

    def rollback(self):
        raise AssertionError('The caller owns rollback.')


def documents():
    record = DocumentSetInput(filing_id=UUID(int=1), set_key='primary-inline-set', xml_document_id=UUID(int=2),
        primary_html_document_id=UUID(int=3), index_document_id=UUID(int=4), manifest_sha256='a' * 64, evidence={
            'member_source_urls':['https://example.invalid/primary.htm','https://example.invalid/secondary.htm'],
            'inline_target':'','excluded_inline_documents':[],
            'source_checks':{'index_membership_complete':True,'single_primary_target':True,'excluded_targets_separate':True}})
    members = [DocumentMemberInput(source_document_id=UUID(int=3), source_sha256='b' * 64, sequence=1,
        document_type='10-Q', is_primary=True, index_locator={'source_xpath':'/html/body/table/tr[2]','source_sha256':'c'*64}),
        DocumentMemberInput(source_document_id=UUID(int=5), source_sha256='d'*64, sequence=2,
        document_type='EX-99.1', is_primary=False,index_locator={'source_xpath':'/html/body/table/tr[3]','source_sha256':'c'*64})]
    return record,members


def occurrence(**updates):
    evidence = {'xml_id':'F1','concept':'{urn:fixture}Cash','context_ref':'c1','unit_ref':'usd',
                'unit':{'numerator':['USD'],'denominator':[]},'period':{'instant':'2026-06-30'},'dimensions':[],
                'raw_value':'24000000','is_nil':False,'inline_mapped_value':'24000000','inline_is_nil':False,
                'html_sha256':'a'*64,'xml_sha256':'b'*64,'inline_xpath':'/html/body/ix:nonfraction'}
    evidence.update(updates)
    return FactSourceOccurrenceInput(filing_id=UUID(int=1),document_set_id=UUID(int=2),fact_id=UUID(int=3),
        html_document_id=UUID(int=4),inline_id='F1',source_xpath="/html/body/*[name()='ix:nonfraction']",evidence=evidence)


def selection():
    return V4ProcessingSetInput(filing_id=UUID(int=1),set_key='filing-import-v4',document_set_id=UUID(int=2),
        source_checks={key:True for key in SOURCE_CHECKS},semantic_checks={'period_header_consistency':True},
        coverage={key:'complete' for key in ('facts','tables','reports','chunks')},configuration={})


def test_document_set_exact_replay_preserves_ids_and_rejects_members_or_manifest_changes():
    conn=Recorder()
    record,members=documents()
    first=store_document_set(conn,record,members)
    before=deepcopy(conn.tables)
    assert store_document_set(conn,record,list(reversed(members))) == first
    assert conn.tables == before
    for changed, changed_members in [(replace(record,manifest_sha256='f'*64),members), (record,members[:1]),
        (record,[*members,replace(members[1],source_document_id=UUID(int=99),sequence=3)])]:
        with pytest.raises(DataConflict):
            store_document_set(conn,changed,changed_members)
        assert conn.tables == before


@pytest.mark.parametrize('change',[{'sequence':True},{'sequence':0},{'is_primary':1},{'document_type':'EX-98'},
                                  {'document_type':'EX-FILING FEES'},{'source_sha256':'BAD'}])
def test_document_members_reject_invalid_or_separate_targets(change):
    _,members=documents()
    with pytest.raises(DatabaseError):
        replace(members[0],**change)


@pytest.mark.parametrize('change',[{'inline_mapped_value':'25000000'},{'inline_is_nil':True},
    {'inline_mapped_value':True},{'inline_mapped_value':'NaN'},{'inline_mapped_value':'Infinity'},
    {'value_comparison':'guess'},{'xml_id':'F2'}])
def test_source_numeric_mismatch_or_unsupported_comparison_cannot_become_review_issue(change):
    with pytest.raises(DatabaseError):
        occurrence(**change)


def test_escaped_markup_preserves_distinct_lexical_values_only_under_explicit_equal_canonical_proof():
    record=occurrence(unit=None,unit_ref=None,raw_value='<p a="1" b="2"/>',inline_mapped_value='<p b="2" a="1"></p>',
        inline_kind='nonnumeric',inline_escape=True,value_comparison='escaped-markup-infoset-v1',
        inline_canonical_sha256='e'*64,xml_canonical_sha256='e'*64)
    assert record.evidence['raw_value'] != record.evidence['inline_mapped_value']
    for change in ({'inline_canonical_sha256':'f'*64},{'inline_escape':False},{'inline_kind':'nonfraction'},
                   {'unit':{'numerator':['USD'],'denominator':[]}}):
        with pytest.raises(DatabaseError):
            replace(record,evidence=record.evidence|change)


def test_occurrence_replay_is_exact_and_mixed_filing_batch_rejects_before_sql():
    conn=Recorder()
    record=occurrence()
    first=store_source_occurrences(conn,[record])
    assert store_source_occurrences(conn,[record]) == first
    with pytest.raises(DataConflict):
        store_source_occurrences(conn,[replace(record,source_xpath='/html/body/span')])
    queries=len(conn.queries)
    with pytest.raises(DatabaseError):
        store_source_occurrences(conn,[record,replace(record,filing_id=UUID(int=99),fact_id=UUID(int=98))])
    assert len(conn.queries) == queries


def test_frozen_record_mutation_is_revalidated_before_any_write():
    conn=Recorder()
    record=occurrence()
    record.evidence['inline_mapped_value']='99'
    with pytest.raises(DatabaseError):
        store_source_occurrences(conn,[record])
    assert not conn.queries


def test_quality_issues_have_stable_ids_and_cannot_rewrite_observed_source():
    conn=Recorder()
    record=QualityIssueInput(filing_id=UUID(int=1),document_set_id=UUID(int=2),binding_id=UUID(int=3),issue_key='table/cell/period',
        code='instant_context_under_duration_header',expected={'period':{'startDate':'2026-01-01','endDate':'2026-06-30'}},
        observed={'period':{'instant':'2026-06-30'}},evidence={'source_xpath':'/table/tr/td'})
    first=store_quality_issues(conn,[record])
    assert store_quality_issues(conn,[record]) == first
    with pytest.raises(DataConflict):
        store_quality_issues(conn,[replace(record,observed={'period':{'instant':'2026-06-29'}})])


def test_processing_membership_replay_is_exact_and_never_prefers_a_set():
    conn=Recorder()
    record=selection()
    reports=[ReportMembershipInput(report_id=UUID(int=7)),ReportMembershipInput(report_id=UUID(int=8))]
    kwargs={'table_ids':[UUID(int=5),UUID(int=6)],'reports':reports,'issue_ids':[]}
    first=store_v4_processing_set(conn,record,**kwargs)
    before=deepcopy(conn.tables)
    assert store_v4_processing_set(conn,record,**kwargs) == first
    for changes in ({'table_ids':[UUID(int=5)]},{'reports':reports[:1]}, {'issue_ids':[UUID(int=9)]}):
        with pytest.raises(DataConflict):
            store_v4_processing_set(conn,record,**(kwargs|changes))
        assert conn.tables == before
    assert not any('preferred' in query for query in conn.queries)


@pytest.mark.parametrize('changes',[
    {'source_checks':{'numeric_values_compared':True}},
    {'source_checks':{key:(False if key=='union_correspondence' else True) for key in SOURCE_CHECKS}},
    {'semantic_checks':{'period_header_consistency':1}},
    {'versions':{'facts':'facts-v1','tables':'tables-v3','reports':'report-v1','chunks':'paragraph-v1'}},
])
def test_v4_processing_contract_cannot_bypass_source_fidelity(changes):
    with pytest.raises(DatabaseError):
        replace(selection(),**changes)


def test_v4_storage_rejects_autocommit_and_empty_batches_do_not_touch_database():
    conn=Recorder()
    assert store_source_occurrences(conn,[]) == []
    assert not conn.queries
    conn.autocommit=True
    with pytest.raises(DatabaseError):
        store_source_occurrences(conn,[occurrence()])


def test_occurrence_pipelines_are_bounded_ordered_and_replay_uses_indexable_identity():
    conn=Recorder()
    template=occurrence()
    records=[replace(template,evidence=template.evidence|{'xml_id':f'F{index}'},inline_id=f'F{index}',fact_id=UUID(int=100+index),
                     source_xpath=f'/html/body/span[{index}]') for index in range(1,7)]
    first=store_source_occurrences(conn,records,batch_size=2)
    assert len(first)==len(set(first))==6 and conn.pipeline_sizes==[2,2,2]
    assert store_source_occurrences(conn,records,batch_size=2)==first
    assert max(conn.pipeline_sizes)==2
    replay=[query for query in conn.queries if 'FOR SHARE' in query]
    assert replay and all('"document_set_id" = %s' in query and '"fact_id" = %s' in query for query in replay)


def test_report_coverage_can_remain_unknown_without_weakening_source_checks():
    record=selection()
    assert replace(record,coverage=record.coverage|{'reports':'unknown'}).coverage['reports']=='unknown'


@pytest.mark.parametrize('accession,expected',[('0000021076-26-000034',2088),('0000831001-26-000045',9437)])
def test_real_document_set_reader_evidence_fits_occurrence_storage_contract(accession,expected):
    from sec_pipeline.fact_extraction import extract_facts
    from sec_pipeline.inline_document_sets import InlineSourceDocument,read_inline_document_set
    root=Path(__file__).resolve().parents[1]
    path=root/'data/raw/sec'/accession/'source_document_set_manifest.json'
    if not path.is_file():pytest.skip('Optional archived document set unavailable.')
    manifest=json.loads(path.read_text(encoding='utf-8'))
    xml=next(file for file in manifest['files'] if file['role']=='extracted_xbrl_instance')
    members=[file for file in manifest['files'] if file['role'] in {'main_inline_xbrl_filing','supplemental_inline_xbrl_document'}]
    documents=[InlineSourceDocument(UUID(int=10+index),Path(file['local_path']).name,root/file['local_path'],file['sha256'],file['source_url'])
               for index,file in enumerate(members)]
    with patch('psycopg.connect',side_effect=AssertionError('Database access forbidden')), \
            patch('socket.create_connection',side_effect=AssertionError('Network access forbidden')):
        parsed=extract_facts(root/xml['local_path'],filing_id=UUID(int=1),source_document_id=UUID(int=2))
        parsed.require_complete()
        result=read_inline_document_set(documents,parsed.facts,xml_sha256=xml['sha256'])
        records=[FactSourceOccurrenceInput(filing_id=UUID(int=1),document_set_id=UUID(int=3),fact_id=UUID(int=100+index),
            html_document_id=UUID(item['inline_document_id']),inline_id=item['inline_id'],source_xpath=item['source_xpath'],
            evidence=item['evidence'],fragments=item['fragments']) for index,item in enumerate(result.occurrences)]
    assert len(records)==expected
