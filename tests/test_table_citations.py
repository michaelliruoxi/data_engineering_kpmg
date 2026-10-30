from copy import deepcopy
import pytest
from sec_pipeline.table_chunk_links import resolve_table_chunks
from sec_pipeline.table_citations import integrate_table_citations
from sec_pipeline import table_citations
from tests.test_table_relations import fixture, SOURCE, HTML, TABLE, ACCESSION, SET, mapped


def test_identical_chunk_replay_is_idempotent_but_conflicting_identity_is_rejected():
    table,_,_,chunk=fixture()
    once=resolve_table_chunks([chunk],[table],{HTML:SOURCE},allow_synthetic=True)
    assert resolve_table_chunks([chunk,deepcopy(chunk)],[table],{HTML:SOURCE},allow_synthetic=True)==once
    conflict=deepcopy(chunk);conflict['source_locator']={'xpath':"//*[@id='a']"}
    result=resolve_table_chunks([chunk,conflict],[table],{HTML:SOURCE},allow_synthetic=True)
    assert len(result)==1 and result[0]['mapping_status']=='rejected'
    assert result[0]['evidence']['reason']=='conflicting_duplicate_chunk_identity'


def inputs(monkeypatch,*,status='passed',ambiguous=False):
    table,_,_,chunk=fixture()
    section=mapped()
    audit={'status':status,'section_mappings':[section,section] if ambiguous else [section],
           'trace_examples':[{'table_id':TABLE,'table_chunk':None,'section':section,'cell_id':'fixture-cell','fact_id':'fixture-fact'}]}
    monkeypatch.setattr(table_citations,'audit_snapshot',lambda *args:deepcopy(audit))
    snapshot={'accession':ACCESSION,'selection':{'contract':'legacy','id':SET},'tables':[table]}
    return snapshot,chunk


def test_missing_actual_producer_keeps_real_trace_without_fabricated_chunk(monkeypatch):
    snapshot,_=inputs(monkeypatch)
    result=integrate_table_citations(snapshot,{HTML:SOURCE})
    assert result['status']=='incomplete'
    trace=result['citations'][0]['trace_examples'][0]
    assert trace['table_chunks']==[] and len(trace['section_associations'])==1
    assert not result['shared_deployed'] and not result['independent_approval']


def test_multiple_fragments_keep_existing_table_and_processing_identity(monkeypatch):
    snapshot,chunk=inputs(monkeypatch)
    fragment=deepcopy(chunk);fragment['chunk_id']='fragment-2';fragment['source_locator']={'xpath':"//*[@id='a']"}
    original=deepcopy(snapshot)
    result=integrate_table_citations(snapshot,{HTML:SOURCE},[chunk,fragment],allow_synthetic=True)
    assert len(result['citations'][0]['chunks'])==2
    assert {row['table_id'] for row in result['citations'][0]['chunks']}=={TABLE}
    assert result['status']=='incomplete' # Synthetic never claims actual acceptance.
    assert result['citations'][0]['processing_set']==snapshot['selection']
    assert result==integrate_table_citations(snapshot,{HTML:SOURCE},[chunk,fragment],allow_synthetic=True)
    assert snapshot==original


@pytest.mark.parametrize('reason',['audit','chunk','selection'])
def test_fail_closed_never_publishes_links_for_invalid_batch(monkeypatch,reason):
    snapshot,chunk=inputs(monkeypatch,status='failed' if reason=='audit' else 'passed')
    if reason=='chunk':chunk['source_locator']={'xpath':'/bad'}
    if reason=='selection':snapshot['selection']=None
    result=integrate_table_citations(snapshot,{HTML:SOURCE},[chunk],allow_synthetic=True)
    assert result['status']=='blocked' and result['citations'][0]['chunks']==[]


def test_ambiguous_sections_not_silently_reduced_to_one(monkeypatch):
    snapshot,chunk=inputs(monkeypatch,ambiguous=True)
    chunk['synthetic']=False # Protocol unit fixture only, not actual producer evidence.
    result=integrate_table_citations(snapshot,{HTML:SOURCE},[chunk])
    assert result['status']=='incomplete'
    assert 'section_association_missing_or_ambiguous' in result['citations'][0]['issues']
    assert result['contract_status']=='pending_Michael_Ruby_confirmation'
    trace=result['citations'][0]['trace_examples'][0]
    assert 'section' not in trace
    assert len(trace['section_associations'])==2


def test_source_v4_document_set_scope_is_preserved_and_cross_set_rejected(monkeypatch):
    snapshot,chunk=inputs(monkeypatch)
    selection={'contract':'source-v4','id':SET}
    snapshot['selection']=selection
    snapshot['selected_processing_set']={'document_set_id':'document-set-fixture'}
    chunk['processing_set']=selection
    chunk['source/document_set']='document-set-fixture'
    result=integrate_table_citations(snapshot,{HTML:SOURCE},[chunk],allow_synthetic=True)
    assert result['citations'][0]['source/document_set']=='document-set-fixture'
    assert len(result['citations'][0]['chunks'])==1
    chunk['source/document_set']='other-set'
    assert integrate_table_citations(snapshot,{HTML:SOURCE},[chunk],allow_synthetic=True)['status']=='blocked'
