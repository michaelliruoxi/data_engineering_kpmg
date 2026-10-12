"""Offline table export; placeholder UUIDs never reach a database."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from uuid import UUID
from .tables import extract_tables
from .database import _verified_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path('source_manifest.json'))
    parser.add_argument('--data-root', type=Path, default=Path('.'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = _verified_manifest(args.manifest, args.data_root)
    roles = {f['role']: f for f in manifest['files']}
    tables = extract_tables(args.data_root / roles['main_inline_xbrl_filing']['local_path'],
        args.data_root / roles['extracted_xbrl_instance']['local_path'],
        filing_id=UUID(int=1), html_document_id=UUID(int=2), xml_document_id=UUID(int=3))
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {'accession': manifest['accession_number'], 'tables_version': 'tables-v1',
               'dry_run': True, 'ids_are_placeholders': True, 'tables': []}
    for table in tables:
        structure = table.structure
        (args.output / f'{table.table_key}.json').write_text(
            json.dumps(asdict(table), default=str, indent=2, ensure_ascii=False), encoding='utf-8')
        (args.output / f'{table.table_key}.tsv').write_text(table.readable_text, encoding='utf-8')
        facts = {f['xml_id'] for row in structure['rows'] for cell in row for f in cell['fact_references']}
        fallback = [cell['cell_id'] for row in structure['rows'] for cell in row
                    if any(f['method'] != 'inline_id' for f in cell['fact_references'])]
        summary['tables'].append({'table_key': table.table_key, 'inventory': structure['inventory'],
                                 'unique_fact_count': len(facts), 'semantic_repeat_cells': fallback})
    summary['unresolved_count'] = sum(t.structure['inventory'].get('unresolved', 0) for t in tables)
    (args.output / 'inventory.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))
    if summary['unresolved_count']:
        raise SystemExit('Unresolved cells: export requires review and cannot be loaded')


if __name__ == '__main__':
    main()
