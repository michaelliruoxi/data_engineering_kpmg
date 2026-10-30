"""Compose existing relationship, section and draft chunk contracts without writes.

This integration artifact never invents chunk identities, approves a producer
contract, or turns synthetic records into real acceptance evidence.
"""
from .table_chunk_links import CONTRACT, resolve_table_chunks
from .table_relations import audit_snapshot
from .table_sections import canonical


def integrate_table_citations(snapshot, sources, chunks=(), *, allow_synthetic=False, input_fields=None):
    snapshot = canonical(snapshot)
    audit = audit_snapshot(snapshot, sources)
    selection = snapshot.get('selection')
    selected = snapshot.get('selected_processing_set') or {}
    document_set = selected.get('document_set_id')
    scope = {'accession': snapshot['accession'], 'processing_set': selection,
             'source/document_set': document_set}
    tables = [table | scope for table in snapshot['tables']]
    resolved = resolve_table_chunks(chunks, tables, sources, input_fields=input_fields,
                                    allow_synthetic=allow_synthetic)
    blockers = []
    if audit['status'] != 'passed':
        blockers.append('relationship_or_original_source_audit_not_passed')
    if not selection:
        blockers.append('explicit_processing_selection_required')
    rejected = [row for row in resolved if row['mapping_status'] == 'rejected']
    if rejected:
        blockers.append('one_or_more_producer_chunks_rejected')
    citations = []
    receipts = {str(row['id']): {key: row.get(key) for key in
                ('id', 'role', 'source_url', 'sha256')} for row in snapshot.get('sources', [])}
    facts = {str(row['id']): row for row in snapshot.get('facts', [])}
    for table in tables:
        sections = [row for row in audit['section_mappings'] if row['table_id'] == table['id']]
        mapped_sections = [row for row in sections if row['mapping_status'] == 'mapped']
        links = [row for row in resolved if row['table_id'] == table['id'] and row['mapping_status'] != 'rejected']
        issues = []
        if len(mapped_sections) != 1:
            issues.append('section_association_missing_or_ambiguous')
        if not links:
            issues.append('actual_table_chunk_not_supplied_or_not_resolved')
        if any(link['synthetic'] for link in links):
            issues.append('synthetic_producer_evidence_only')
        # Don't publish resolved citations when source/relationship audit fails.
        verified_links = links if not blockers else []
        traces = [{key: value for key, value in row.items() if key not in {'table_chunk', 'section'}} |
                  {'table_chunks': verified_links, 'section_associations': mapped_sections,
                   'section_status': 'unique_mapped' if len(mapped_sections) == 1 else 'missing_or_ambiguous',
                   'chunk_status': 'draft_or_synthetic_links_not_approved' if verified_links else 'no_verified_table_chunk'}
                  for row in audit['trace_examples'] if row['table_id'] == table['id']]
        for trace in traces:
            source_ids = [str(table['source_document_id']),
                          str(facts.get(str(trace.get('fact_id')), {}).get('source_document_id'))]
            trace['source_receipts'] = [receipts[key] for key in dict.fromkeys(source_ids) if key in receipts]
        citations.append({'table_id': table['id'], 'table_key': table['table_key'],
                          'processing_set': selection, 'source/document_set': document_set,
                          'sections': sections, 'chunks': verified_links, 'trace_examples': traces,
                          'issues': issues})
    return canonical({'accession': snapshot['accession'], 'selection': selection,
                      'contract': CONTRACT, 'contract_status': 'pending_Michael_Ruby_confirmation',
                      'status': 'blocked' if blockers else 'incomplete' if any(r['issues'] for r in citations) else 'draft_ready_for_contract_review',
                      'blockers': blockers, 'rejected_chunks': rejected, 'citations': citations,
                      'audit': audit, 'shared_deployed': False, 'independent_approval': False})
