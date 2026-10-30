"""Committed synthetic company-regression models, not acceptance of real filings."""
from collections import Counter
from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from uuid import UUID

from lxml import html
import pytest
from sec_pipeline.database import FactInput, FinancialTableInput
from sec_pipeline.source_cells_v4 import transform_tables, validate_transformation
from sec_pipeline.balance_segments import verified_balance_segments
from sec_pipeline.tables import _segment_candidate, local
from sec_pipeline.table_relations import _valid_occurrence

ROOT=Path(__file__).parent/'fixtures/company_regressions'


def test_fixture_hashes_and_explicit_provenance():
    manifest=json.loads((ROOT/'manifest.json').read_text())
    assert manifest['provenance'].startswith('synthetic')
    for name,digest in manifest['sha256'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest


def test_ntap_style_opening_cash_does_not_invent_period_correction():
    data=json.loads((ROOT/'ntap_opening_cash.json').read_text())
    for key in ('filing_id','source_document_id'):
        data['table'][key]=UUID(data['table'][key])
    table=FinancialTableInput(**data['table'])
    facts=[]
    for raw in data['facts']:
        for key in ('filing_id','source_document_id'): raw[key]=UUID(raw[key])
        for key in ('period_start','period_end','instant_date'):
            if raw[key]: raw[key]=date.fromisoformat(raw[key])
        if raw['numeric_value'] is not None: raw['numeric_value']=Decimal(raw['numeric_value'])
        facts.append(FactInput(**raw))
    source=(ROOT/'ntap_opening_cash.html').read_bytes()
    original=deepcopy(facts)
    [result]=transform_tables([table],source,facts)
    assert result.structure['rows'][4][1]['quality_issues'][0]['code']=='opening_balance_date_not_prior_day'
    assert facts==original
    assert validate_transformation([result],[table],source,facts)


def test_bank_split_statement_keeps_two_physical_tables():
    tree=html.document_fromstring((ROOT/'bank_continuation.html').read_bytes()).getroottree()
    facts=json.loads((ROOT/'bank_continuation.json').read_text())
    ids=Counter(n.get('id') for n in tree.iter() if local(n)=='nonfraction')
    candidates=[_segment_candidate(tree,t,'balance_sheet',facts,ids) for t in tree.xpath('//table')]
    selected=verified_balance_segments(tree,candidates,facts)
    assert [x.table_key for x in selected]==['balance_sheet.segment_1','balance_sheet.segment_2']
    assert len(selected[0].group['balance_continuation']['totals'])==2


@pytest.mark.parametrize('raw',[None,''])
def test_nke_nil_receipt_contract(raw):
    d=json.loads((ROOT/'nke_nil.json').read_text())
    d['occurrence']['evidence']['raw_value']=raw
    assert _valid_occurrence(d['occurrence'],d['reference'],d['fact'],d['table'],d['document_set'],None)
    d['occurrence']['evidence']['inline_mapped_value']='0'
    assert not _valid_occurrence(d['occurrence'],d['reference'],d['fact'],d['table'],d['document_set'],None)
