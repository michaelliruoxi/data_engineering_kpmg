"""Read-only shared acceptance; password comes from a private terminal prompt."""
import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import socket

import psycopg
from psycopg.rows import dict_row

HOST = 'sec-filings.cghyw6082mug.us-east-1.rds.amazonaws.com'
ACCESSION = '0001193125-26-191507'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ca', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if '-----BEGIN CERTIFICATE-----' not in args.ca.read_text():
        raise ValueError('CA is not a PEM certificate bundle.')
    for key in list(os.environ):
        if key.startswith(('PG', 'POSTGRES_')):
            os.environ.pop(key)
    result = {'sslmode': 'verify-full', 'ca': str(args.ca.resolve()),
              'dns': socket.gethostbyname_ex(HOST)}
    with socket.create_connection((HOST, 5432), 10):
        result['tcp'] = 'passed'
    password = getpass('Jazzy database password (private input): ')
    with psycopg.connect(host=HOST, port=5432, dbname='sec_filings', user='sec_jazzy',
                        password=password, sslmode='verify-full', sslrootcert=str(args.ca),
                        connect_timeout=10, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            result['identity'] = conn.execute('SELECT current_user, current_database()').fetchone()
            result['ssl'] = conn.execute('SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()
            result['permissions'] = conn.execute("""SELECT relation,
                has_table_privilege(current_user,'sec.'||relation,'SELECT') AS select,
                has_table_privilege(current_user,'sec.'||relation,'INSERT') AS insert,
                has_table_privilege(current_user,'sec.'||relation,'UPDATE') AS update,
                has_table_privilege(current_user,'sec.'||relation,'DELETE') AS delete
                FROM unnest(ARRAY['financial_facts','financial_tables','financial_table_facts']) AS relation""").fetchall()
            result['filing'] = conn.execute('SELECT * FROM sec.filing_catalog WHERE accession_number=%s', (ACCESSION,)).fetchall()
            for table in ('financial_facts', 'financial_tables'):
                result[table] = conn.execute(f'''SELECT d.source_document_id, s.role, s.local_path,
                    d.extraction_version, count(*) AS count FROM sec.{table} d
                    JOIN sec.source_documents s ON s.id=d.source_document_id
                    JOIN sec.filings f ON f.id=d.filing_id WHERE f.accession_number=%s
                    GROUP BY d.source_document_id,s.role,s.local_path,d.extraction_version
                    ORDER BY d.extraction_version,s.local_path''', (ACCESSION,)).fetchall()
            result['links'] = conn.execute('''SELECT t.source_document_id AS html_source,
                ff.source_document_id AS xml_source, t.extraction_version AS table_version,
                ff.extraction_version AS fact_version,t.table_key,count(*) AS count,
                bool_and(t.filing_id=l.filing_id AND ff.filing_id=l.filing_id) AS same_filing
                FROM sec.financial_table_facts l JOIN sec.financial_tables t ON t.id=l.table_id
                JOIN sec.financial_facts ff ON ff.id=l.fact_id JOIN sec.filings f ON f.id=l.filing_id
                WHERE f.accession_number=%s GROUP BY t.source_document_id,ff.source_document_id,
                t.extraction_version,ff.extraction_version,t.table_key ORDER BY table_version,t.table_key''', (ACCESSION,)).fetchall()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, default=str, indent=2) + '\n')
    print(json.dumps(result, default=str, indent=2))


if __name__ == '__main__':
    main()
