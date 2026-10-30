"""Generate isolated MSFT export inputs from hash-verified committed originals.

No network or database. Export UUIDs are placeholders only: the local loader
rebinds all identities to its actual receiving database before validation.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from uuid import UUID
from sec_pipeline.database import _verified_manifest
from sec_pipeline.report_cleaning import clean_report
from sec_pipeline.tables import extract_tables


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    root=Path(__file__).resolve().parents[1]
    manifest=_verified_manifest(root/'source_manifest.json',root)
    roles={r['role']:root/r['local_path'] for r in manifest['files']}
    source=roles['main_inline_xbrl_filing']
    tables=extract_tables(source,roles['extracted_xbrl_instance'],filing_id=UUID(int=1),
                          html_document_id=UUID(int=2),xml_document_id=UUID(int=3))
    args.output.mkdir(parents=True,exist_ok=False)
    for table in tables:
        (args.output/(table.table_key+'.json')).write_text(json.dumps(asdict(table),default=str,indent=2)+'\n')
    (args.output/'sections.json').write_text(json.dumps(clean_report(source,form=manifest['form']).sections,indent=2)+'\n')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
