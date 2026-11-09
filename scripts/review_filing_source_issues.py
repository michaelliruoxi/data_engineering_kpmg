#!/usr/bin/env python3
"""Explain original-source numeric and cash-period issues without database writes.

Exit 0 means no issue was found by these bounded checks, 2 means review remains
required, and 1 means source correspondence or table reconstruction failed.
This is not an ingestion receipt or permission to change original facts.
"""

import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

from sec_pipeline.fact_extraction import check_inline_correspondence, extract_facts
from sec_pipeline.source_cells_v3 import transform_tables as transform_v3
from sec_pipeline.source_cells_v4 import corroborate_opening_cash_issue, transform_tables as transform_v4
from sec_pipeline.tables import extract_tables


def review_sources(html_path, xml_path):
    html_path, xml_path = Path(html_path), Path(xml_path)
    source = html_path.read_bytes()
    # Offline placeholders are deliberately not receiving database identities.
    scope = dict(filing_id=UUID(int=1), html_document_id=UUID(int=2), xml_document_id=UUID(int=3))
    facts = extract_facts(xml_path, filing_id=scope['filing_id'],
                          source_document_id=scope['xml_document_id']).require_complete().facts
    correspondence = check_inline_correspondence(html_path, facts)
    result = {
        'schema_version': 1, 'scope': 'offline_original_source_review',
        'database_checked': False, 'corrections_applied': False,
        'source_files': {'html': html_path.name, 'xml': xml_path.name},
        'source_sha256': {'html': hashlib.sha256(source).hexdigest(),
                          'xml': hashlib.sha256(xml_path.read_bytes()).hexdigest()},
        'inline_correspondence': correspondence,
        'cash_period_issues': [],
        'limits': ['Raw facts and sealed table payloads are unchanged.',
                   'Corroboration explains the expected date; it does not approve or rebind the original occurrence.',
                   'UUIDs in reconstructed issue evidence are offline placeholders, not database IDs.'],
    }
    if (correspondence['mismatches'] or correspondence['numeric_values_not_compared']
            or correspondence['inline_ids_without_fact']):
        return result | {'status': 'failed', 'reason': 'original_html_xml_correspondence_failed'}
    tables = extract_tables(html_path, xml_path, **scope, tables_version='tables-v2')
    tables = transform_v4(transform_v3(tables, source, facts), source, facts)
    for table in tables:
        for row in table.structure['rows']:
            for cell in row:
                for issue in cell.get('quality_issues', []):
                    result['cash_period_issues'].append({
                        'table_key': table.table_key, 'issue': issue,
                        'corroboration': corroborate_opening_cash_issue(issue, source, facts),
                    })
    unresolved = sum(table.structure['inventory'].get('unresolved', 0) for table in tables)
    result['tables_reconstructed'] = len(tables)
    result['unresolved_table_cells'] = unresolved
    review = (result['cash_period_issues'] or correspondence.get('numeric_tag_boundary_issues')
              or correspondence.get('irregular_number_display') or unresolved)
    result['status'] = 'review_required' if review else 'no_issue_found_by_bounded_checks'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--html', type=Path, required=True)
    parser.add_argument('--xml', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if any(args.output.resolve() == source.resolve()
           or (args.output.exists() and source.exists() and args.output.samefile(source))
           for source in (args.html, args.xml)):
        parser.error('The review output must not replace either original source.')
    result = review_sources(args.html, args.xml)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'status': result['status'], 'output': str(args.output),
                      'database_checked': False, 'corrections_applied': False}))
    return 1 if result['status'] == 'failed' else 2 if result['status'] == 'review_required' else 0


if __name__ == '__main__':
    raise SystemExit(main())
