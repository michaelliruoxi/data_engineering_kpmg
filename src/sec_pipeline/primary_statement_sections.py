"""Selected primary statements must have financial-statement section ownership."""

from .table_sections import map_table_section


def primary_statement_section_issues(tables, report, source, *, accession, sections=None):
    """Check original-DOM ownership without using any statement heading text.

    Callers supply the actual selected tables, rather than rediscovering them
    from the same heading patterns that created the report boundaries. Stored
    acceptance supplies relational sections; parsing uses report descriptors.
    """
    sections = report.get('sections', []) if sections is None else sections
    section_types = {row.get('section_key', row.get('section_id')): row.get('section_type')
                     for row in sections}
    issues = []
    for table in tables:
        mapping = map_table_section(table, report, sections, source, accession=accession)
        kind = section_types.get(mapping.get('section_key'))
        if mapping['mapping_status'] != 'mapped' or kind != 'financial_statements':
            issues.append({'check': 'primary_statement_outside_financial_section',
                'table_id': table.get('id'), 'table_key': table.get('table_key'),
                'statement_type': table.get('statement_type'), 'section_type': kind,
                'mapping': mapping})
    return issues
