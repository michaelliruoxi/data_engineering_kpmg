"""Replay original-source citations without network, DB writes or fabricated chunks."""
import argparse
import json
from pathlib import Path
from sec_pipeline.table_citations import integrate_table_citations
from sec_pipeline.table_relations_cli import source_bytes


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--chunks',type=Path,help='Actual producer JSON array; omit to expose missing chunk evidence')
    parser.add_argument('--input-fields',type=Path,help='JSON canonical field to producer dotted-path mapping')
    parser.add_argument('--allow-synthetic',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    snapshot=json.loads(args.snapshot.read_text())
    chunks=json.loads(args.chunks.read_text()) if args.chunks else []
    if not isinstance(chunks,list): parser.error('Producer chunks must be a JSON array')
    fields=json.loads(args.input_fields.read_text()) if args.input_fields else None
    result=integrate_table_citations(snapshot,source_bytes(snapshot,args.source_root),chunks,
                                    allow_synthetic=args.allow_synthetic,input_fields=fields)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    # Even a complete draft is not confirmed producer/consumer acceptance.
    return 1 if result['status']=='blocked' else 2


if __name__=='__main__':
    raise SystemExit(main())
