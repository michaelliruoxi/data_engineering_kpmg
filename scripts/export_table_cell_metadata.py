"""Export actual table/cell/fact keys for RAG without DB writes or headers.

Input JSON has cells (statement_cells_all_versions), sections, tables
(financial_tables), and one report. No processing set is chosen by this script.
"""
import argparse
from collections import Counter
import json
from pathlib import Path

from sec_pipeline.table_cell_metadata import export_table_metadata


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    snapshot=json.loads(args.input.expanduser().read_text())
    records=export_table_metadata(**{key:snapshot[key] for key in ('cells','sections','tables','report')})
    output=args.output_dir.expanduser();output.mkdir(parents=True,exist_ok=True)
    with (output/'table_cell_metadata.jsonl').open('w') as stream:
        for table in records:stream.write(json.dumps(table,ensure_ascii=False)+'\n')
    cells=[c for t in records for c in t['cells']]
    facts=[f for c in cells for f in c['fact_ids']]
    summary={
        'status':'metadata_keys_and_retained_layout_checks_passed',
        'table_count':len(records),'cell_count':len(cells),
        'unique_cell_ids':len({c['cell_id'] for c in cells}),
        'accepted_cell_fact_references':len(facts),'unique_fact_ids':len(set(facts)),
        'resolution_status_counts':dict(Counter(c['resolution_status'] for c in cells)),
        'review_required_cells':[{'table_id':t['table_id'],'table_key':t['table_key'],
            'cell_id':c['cell_id'],'cell_key':c['cell_key'],'fact_ids':c['fact_ids']}
            for t in records for c in t['cells'] if c['resolution_status']=='review_required'],
        'section_mapping_method':'unique_retained_whole_table_token_alignment',
        'section_mapping_persisted':False,'original_html_audit_performed':False,
        'actual_rag_producer_chunks_supplied':False,'processing_selection_verified':False,
        'ids_generated':False,'database_writes':False,'shared_deployed':False,
    }
    (output/'table_cell_metadata_checks.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print('Output:',output.resolve())
    return 0


if __name__=='__main__':raise SystemExit(main())
