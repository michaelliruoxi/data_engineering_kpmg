"""Flagged V8 sets against the disposable PostgreSQL fixture."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import psycopg
import pytest

from sec_pipeline import database as db, filing_ingestion_v8
from sec_pipeline.extraction_profiles import FLAGGED_PROFILE, corrected_tables, versions_for
from sec_pipeline.parser_policy import policy_metadata
from sec_pipeline.publication import publish_corrected_set, rollback_corrected_set, served_selection
from sec_pipeline.source_cells_v3 import fact_reference
from sec_pipeline.source_set_storage import (
    ReportMembershipInput, TaggingDefectInput, store_source_occurrences, store_tagging_defects, store_v4_processing_set,
)
from sec_pipeline.tagging_defects import defect_key
from sec_pipeline.v4_table_cells import store_v4_table_cells
from tests.test_catalog_postgres import disposable_catalog_database  # noqa: F401
from tests.test_corrected_profile_postgres import corrected_fixture
from tests.test_source_set_postgres import database_fixture, local_connection, seal  # noqa: F401

OBSERVED = ('period', 'concept', 'context_ref', 'unit_ref', 'unit', 'dimensions', 'raw_value', 'is_nil')


def add_revenue_fact(conn, item):
    """A mapped finance metric printed outside every table, with its exact source occurrence."""
    base = next(fact for fact in item.facts if fact.occurrence_key == 'F3')
    revenue = replace(base, occurrence_key='F7', concept_name='Revenues', raw_value='500', numeric_value=Decimal('500'),
                      source_reference=base.source_reference | {'element_id': 'F7'})
    fact_id = db.store_fact(conn, revenue)
    template = item.occurrence_inputs[[value.occurrence_key for value in item.facts].index('F3')]
    occurrence = replace(template, fact_id=fact_id, inline_id='F7',
                         source_xpath="/html/body/*[name()='p']/*[name()='ix:nonfraction']",
                         evidence=fact_reference(revenue) | {
                             'xml_id': 'F7', 'inline_mapped_value': '500', 'inline_is_nil': False,
                             'html_sha256': template.evidence['html_sha256'], 'xml_sha256': 'b' * 64,
                             'inline_xpath': '/html/body/p/ix:nonfraction'})
    [occurrence_id] = store_source_occurrences(conn, [occurrence])
    return revenue, fact_id, occurrence_id


def fact_defect(item, fact, fact_id, occurrence_id, *, printed, tagged, inline_xpath):
    observed = {name: value for name, value in fact_reference(fact).items() if name in OBSERVED} | {'tagged_number': tagged}
    return TaggingDefectInput(
        filing_id=item.filing, document_set_id=item.document_set,
        defect_key=defect_key('tables-v8', {'inline_id': fact.occurrence_key, 'code': 'printed_number_outside_tag'}),
        fact_id=fact_id, occurrence_id=occurrence_id, inline_id=fact.occurrence_key, inline_document_id=item.sources['primary'],
        table_id=None, cell_id=None, code='printed_number_outside_tag', expected={'printed_number': printed},
        observed=observed, evidence={'inline_xpath': inline_xpath, 'html_sha256': 'a' * 64, 'filename': 'primary.htm'})


def flagged_fixture(conn, item, *, key='flagged-test', link=True, fact_defects=()):
    """The fixture's period conflict recorded as a V8 company tagging error on its cell."""
    versions = versions_for(FLAGGED_PROFILE)
    table, = corrected_tables([item.table], profile=FLAGGED_PROFILE)
    structure = deepcopy(table.structure)
    cell = structure['rows'][0][1]
    issue, ref = cell['quality_issues'][0], cell['source_fact_references'][0]
    fact = next(value for value in item.facts if value.occurrence_key == ref['xml_id'])
    tags = [{'inline_id': ref['xml_id'], 'inline_xpath': ref['inline_xpath']}]
    record_key = defect_key('tables-v8', {'cell_xpath': cell['source_xpath'], 'inline_id': ref['xml_id'], 'code': issue['code']})
    evidence = deepcopy(issue['evidence']) | {'inline_xpath': ref['inline_xpath'], 'cell_tags': tags}
    for name in ('source_fact_references', 'quality_issues', 'quality_issue_keys'):
        cell.pop(name)
    cell.update(classification='source_defect', tagging_defect={
        'defect_key': record_key, 'code': issue['code'], 'rule_version': 'company-tagging-defects-v1',
        'inline_id': ref['xml_id'], 'inline_xpath': ref['inline_xpath'], 'cell_tags': deepcopy(tags)})
    structure['inventory'] = dict(Counter(value['classification'] for row in structure['rows'] for value in row))
    table = replace(table, structure=structure)
    table_id = db.store_financial_table(conn, table)
    store_v4_table_cells(conn, table_id, table, item.fact_ids, item.occurrences, item.document_set)
    cell_id = conn.execute('SELECT id FROM sec.table_cells WHERE table_id=%s AND cell_key=%s',
                           (table_id, cell['cell_id'])).fetchone()[0]
    observed = {name: value for name, value in fact_reference(fact).items() if name in OBSERVED}
    records = [TaggingDefectInput(
        filing_id=item.filing, document_set_id=item.document_set, defect_key=record_key, fact_id=item.fact_ids[ref['xml_id']],
        occurrence_id=item.occurrences[ref['xml_id']], inline_id=ref['xml_id'], inline_document_id=item.sources['primary'],
        table_id=table_id, cell_id=cell_id, code=issue['code'], expected=deepcopy(issue['expected']),
        observed=observed, evidence=evidence), *fact_defects]
    defect_ids = store_tagging_defects(conn, records)
    reports = []
    for name in ('primary', 'secondary'):
        text = 'Flagged ' + name + ' document text.'
        report_id = db.store_report(conn, db.ReportInput(filing_id=item.filing, source_document_id=item.sources[name],
                                                         extraction_version=versions['reports'], full_text=text))
        db.store_chunk(conn, db.ChunkInput(filing_id=item.filing, report_id=report_id, chunking_version=versions['chunks'],
                                           chunk_index=0, text=text, offset_start=0, offset_end=len(text)))
        reports.append(ReportMembershipInput(report_id=report_id, chunking_version=versions['chunks']))
    selection = replace(item.selection, set_key=key, versions=versions,
                        source_checks=item.selection.source_checks | {'numeric_tag_boundaries_verified': True},
                        semantic_checks={'period_header_consistency': True, 'primary_section_boundaries': True},
                        configuration=item.selection.configuration | {
                            'versions': versions, **policy_metadata(versions), 'extraction_profile': FLAGGED_PROFILE,
                            'section_validation_policy': 'source-boundaries-v2'})
    set_id = store_v4_processing_set(conn, selection, table_ids=[table_id], reports=reports,
                                     defect_ids=defect_ids if link else [])
    return set_id, table_id, defect_ids


def test_flagged_set_passes_publishes_and_keeps_flagged_facts_from_readers(local_connection):  # noqa: F811
    conn = local_connection
    item = database_fixture(conn, review=True)
    revenue, revenue_id, revenue_occurrence = add_revenue_fact(conn, item)
    original = seal(conn, item)
    boundary = fact_defect(item, revenue, revenue_id, revenue_occurrence, printed='5001', tagged='500',
                           inline_xpath='/html/body/p/ix:nonfraction')
    flagged, _, defect_ids = flagged_fixture(conn, item, fact_defects=[boundary])
    status, counts = conn.execute('SELECT validation_status, counts FROM sec.source_processing_sets WHERE id=%s',
                                  (flagged,)).fetchone()
    assert status == 'passed' and len(defect_ids) == 2
    assert (counts['source_defect_cells'], counts['tagging_defects'], counts['quality_issues'],
            counts['review_required_cells']) == (1, 2, 0, 0)
    assert 'tagging_defects' not in conn.execute('SELECT counts FROM sec.source_processing_sets WHERE id=%s',
                                                 (original,)).fetchone()[0]
    current = served_selection(conn, item.filing)
    assert current == ('source', original)
    assert 'F7' in {row[0] for row in conn.execute('SELECT occurrence_key FROM finance.metric_evidence WHERE filing_id=%s',
                                                    (item.filing,))}
    assert publish_corrected_set(conn, item.filing, flagged, expected_current=current) == ('source', flagged)
    ready = {row[0] for row in conn.execute('SELECT occurrence_key FROM sec.analysis_ready_facts WHERE filing_id=%s',
                                            (item.filing,))}
    assert {'F3', 'F4', 'F5', 'F6'} <= ready and not {'F1', 'F7'} & ready
    assert conn.execute("SELECT count(*) FROM sec.analysis_ready_statement_cells WHERE processing_set_id=%s "
                        "AND resolution_status='source_defect'", (flagged,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM sec.source_statement_cells WHERE processing_set_id=%s "
                        "AND resolution_status='source_defect'", (flagged,)).fetchone()[0] == 1
    flagged_facts = dict(conn.execute('SELECT occurrence_key, tagging_defect_codes FROM sec.source_fact_catalog '
                                      'WHERE processing_set_id=%s AND tagging_defect_count>0', (flagged,)).fetchall())
    assert flagged_facts == {'F1': ['instant_context_under_duration_header'], 'F7': ['printed_number_outside_tag']}
    metrics = {row[0] for row in conn.execute('SELECT occurrence_key FROM finance.metric_evidence WHERE filing_id=%s',
                                               (item.filing,))}
    assert 'F7' not in metrics and 'F3' in metrics
    recorded = conn.execute('SELECT parser_revisions FROM finance.processing_revision WHERE processing_set_id=%s',
                            (flagged,)).fetchone()[0]
    assert recorded == policy_metadata(filing_ingestion_v8.VERSIONS)['parser_revisions']
    assert rollback_corrected_set(conn, item.filing, flagged) == current
    assert 'F7' in {row[0] for row in conn.execute('SELECT occurrence_key FROM finance.metric_evidence WHERE filing_id=%s',
                                                    (item.filing,))}
    conn.execute('SET CONSTRAINTS ALL IMMEDIATE')


def test_flagged_defects_must_match_cells_and_never_reach_readers_through_links(local_connection):  # noqa: F811
    conn = local_connection
    item = database_fixture(conn, review=True)
    # A flagged cell needs its own selected defect record.
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        flagged_fixture(conn, item, key='unlinked', link=False)
    # A fact may not be flagged while a resolved cell of the same set still links it.
    operating = next(fact for fact in item.facts if fact.occurrence_key == 'F3')
    linked = fact_defect(item, operating, item.fact_ids['F3'], item.occurrences['F3'], printed='1001', tagged='100',
                         inline_xpath=item.occurrence_inputs[[f.occurrence_key for f in item.facts].index('F3')].evidence['inline_xpath'])
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        flagged_fixture(conn, item, key='linked-flag', fact_defects=[linked])
    # A partially tagged number is never shown in a statement cell, flagged or not.
    dividend = next(fact for fact in item.facts if fact.occurrence_key == 'F1')
    shown = fact_defect(item, dividend, item.fact_ids['F1'], item.occurrences['F1'], printed='240', tagged='24',
                        inline_xpath=item.occurrence_inputs[[f.occurrence_key for f in item.facts].index('F1')].evidence['inline_xpath'])
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        flagged_fixture(conn, item, key='shown-partial', fact_defects=[shown])
    # A partially tagged number must keep both texts, the tagged one inside the printed one.
    wrong = replace(linked, defect_key=linked.defect_key[:-1] + '1', expected={'printed_number': '999'})
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        store_tagging_defects(conn, [wrong])
    # A cell defect must name the fact the cell shows.
    flagged, table_id, _ = flagged_fixture(conn, item)
    cell_id = conn.execute("SELECT id FROM sec.table_cells WHERE table_id=%s AND resolution_status='source_defect'",
                           (table_id,)).fetchone()[0]
    other = replace(linked, defect_key=linked.defect_key[:-1] + '2', table_id=table_id, cell_id=cell_id,
                    code='fact_period_differs_from_column', expected={'period': {'instant': '2026-06-30'}})
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        store_tagging_defects(conn, [other])
    conn.execute('SET CONSTRAINTS ALL IMMEDIATE')


def test_v4_to_v8_coexist_and_a_defect_free_v8_set_publishes(local_connection):  # noqa: F811
    conn = local_connection
    item = database_fixture(conn, review=False)
    original = seal(conn, item)
    flagged, _, _ = corrected_fixture(conn, item, key='flagged-clean', profile=FLAGGED_PROFILE)
    status, counts = conn.execute('SELECT validation_status, counts FROM sec.source_processing_sets WHERE id=%s',
                                  (flagged,)).fetchone()
    assert status == 'passed' and (counts['source_defect_cells'], counts['tagging_defects']) == (0, 0)
    assert publish_corrected_set(conn, item.filing, flagged, expected_current=('source', original)) == ('source', flagged)
    assert rollback_corrected_set(conn, item.filing, flagged) == ('source', original)
    with pytest.raises((ValueError, psycopg.errors.CheckViolation)), conn.transaction():
        corrected_fixture(conn, item, key='flagged-v7-revisions', profile=FLAGGED_PROFILE,
                          configuration=item.selection.configuration | {
                              'versions': filing_ingestion_v8.VERSIONS, 'extraction_profile': FLAGGED_PROFILE,
                              'section_validation_policy': 'source-boundaries-v2',
                              'parser_revisions': policy_metadata(filing_ingestion_v8.VERSIONS)['parser_revisions']
                              | {'facts': 'numeric-tag-boundaries-v3'}})
    conn.execute('SET CONSTRAINTS ALL IMMEDIATE')
