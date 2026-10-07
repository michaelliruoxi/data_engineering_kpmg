"""Offline configuration, input, and command-boundary checks."""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg

from sec_pipeline import database as db
from sec_pipeline import db_cli


def write_sample_fixture(root: Path) -> tuple[Path, dict]:
    roles = (
        'filing_index', 'main_inline_xbrl_filing',
        'extracted_xbrl_instance', 'xbrl_extension_schema',
    )
    records = []
    for index, role in enumerate(roles):
        content = f'Original fixture source {index}'.encode()
        relative = f'source-{index}.xml'
        (root / relative).write_bytes(content)
        records.append({
            'role': role, 'local_path': relative,
            'source_url': f'https://example.invalid/{relative}',
            'size_bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(),
            'retrieved_at_utc': '2026-01-03T12:00:00+00:00',
            'status': 'verified', 'verification': {'result': 'passed'},
        })
    manifest = {
        'schema_version': 1, 'status': 'verified', 'company': 'Fixture Company',
        'cik': '0000000042', 'accession_number': '0000000042-26-000001',
        'form': '10-Q', 'filing_date': '2026-01-02',
        'reporting_period_end': '2025-12-31',
        'filing_index_url': 'https://example.invalid/index.html',
        'index_document': records[0], 'files': records[1:],
    }
    path = root / 'manifest.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    return path, manifest


class ConfigurationTests(unittest.TestCase):
    def test_environment_file_quotes_bom_and_process_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / 'database.env'
            env_file.write_text(
                '# local configuration\nPOSTGRES_PASSWORD="local=value"\n'
                "POSTGRES_USER='fixture_user'\nPOSTGRES_PORT=5544\n"
                'POSTGRES_DB=fixture_database\n', encoding='utf-8-sig',
            )
            with patch.dict(os.environ, {'PGPASSWORD': 'runtime-secret', 'PGPORT': '5545'}, clear=True):
                settings = db.connection_settings(env_file)
        self.assertEqual(settings['password'], 'runtime-secret')
        self.assertEqual(settings['port'], 5545)
        self.assertEqual(settings['user'], 'fixture_user')
        self.assertEqual(settings['dbname'], 'fixture_database')
        self.assertEqual(settings['host'], '127.0.0.1')
        self.assertNotIn('sslmode', settings)
        self.assertNotIn('sslrootcert', settings)

    def test_tls_configuration_file_reaches_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / 'database.env'
            env_file.write_text(
                'PGPASSWORD=fixture-secret\nPGHOST=database.example.invalid\n'
                'PGSSLMODE=verify-full\nPGSSLROOTCERT="C:/certificates/root bundle.pem"\n',
                encoding='utf-8',
            )
            with patch.dict(os.environ, {}, clear=True), patch.object(db.psycopg, 'connect') as connect:
                db.connect(env_file)
            settings = connect.call_args.kwargs
        self.assertEqual(settings['host'], 'database.example.invalid')
        self.assertEqual(settings['sslmode'], 'verify-full')
        self.assertEqual(settings['sslrootcert'], 'C:/certificates/root bundle.pem')

    def test_process_tls_configuration_overrides_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / 'database.env'
            env_file.write_text(
                'PGPASSWORD=fixture-secret\nPGSSLMODE=require\nPGSSLROOTCERT=file-root.pem\n',
                encoding='utf-8',
            )
            with patch.dict(os.environ, {
                'PGSSLMODE': 'verify-full', 'PGSSLROOTCERT': 'runtime-root.pem',
            }, clear=True):
                settings = db.connection_settings(env_file)
        self.assertEqual(settings['sslmode'], 'verify-full')
        self.assertEqual(settings['sslrootcert'], 'runtime-root.pem')

    def test_invalid_tls_mode_fails_before_connection(self):
        for sslmode in ('', 'verify_full', 'VERIFY-FULL', 'invalid-private-value'):
            with (self.subTest(sslmode=sslmode), patch.dict(os.environ, {
                'PGPASSWORD': 'fixture-secret', 'PGSSLMODE': sslmode,
            }, clear=True), patch.object(db.psycopg, 'connect') as connect):
                with self.assertRaisesRegex(db.DatabaseError, 'PGSSLMODE') as raised:
                    db.connect(None)
                connect.assert_not_called()
                self.assertNotIn('private-value', str(raised.exception))

    def test_placeholder_password_and_invalid_ports_are_rejected(self):
        for password in ('', 'CHANGE_ME'):
            with self.subTest(password=password), patch.dict(os.environ, {'POSTGRES_PASSWORD': password}, clear=True):
                with self.assertRaises(db.DatabaseError):
                    db.connection_settings(None)
        for port in ('zero', '0', '-1', '65536'):
            with self.subTest(port=port), patch.dict(
                os.environ, {'PGPASSWORD': 'fixture-secret', 'PGPORT': port}, clear=True,
            ):
                with self.assertRaises(db.DatabaseError):
                    db.connection_settings(None)

    def test_invalid_env_syntax_does_not_echo_credentials(self):
        for line in ('PGPASSWORD="secret-without-closing-quote', 'secret-invalid-line'):
            with self.subTest(line=line), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'database.env'
                path.write_text(line, encoding='utf-8')
                with patch.dict(os.environ, {}, clear=True), self.assertRaises(db.DatabaseError) as raised:
                    db.connection_settings(path)
                self.assertNotIn('secret', str(raised.exception))

    def test_cli_connection_errors_are_redacted(self):
        output = io.StringIO()
        with (patch.object(db_cli, 'connect', side_effect=psycopg.OperationalError('password=private-value')),
              patch('sys.stderr', output)):
            result = db_cli.main(['status'])
        self.assertEqual(result, 1)
        self.assertIn('OperationalError', output.getvalue())
        self.assertNotIn('private-value', output.getvalue())


class OfflineInputTests(unittest.TestCase):
    def test_corrupt_last_source_fails_before_any_database_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, manifest = write_sample_fixture(root)
            last_source = root / manifest['files'][-1]['local_path']
            last_source.write_bytes(b'corrupted bytes')
            connection = Mock()
            with (patch('httpx.Client.send', side_effect=AssertionError('Unexpected HTTP request')),
                  patch('urllib.request.urlopen', side_effect=AssertionError('Unexpected HTTP request'))):
                with self.assertRaisesRegex(db.DatabaseError, 'SHA-256 mismatch'):
                    db.seed_manifest(connection, manifest_path, root)
            self.assertEqual(connection.mock_calls, [])
            self.assertEqual(last_source.read_bytes(), b'corrupted bytes')

    def test_manifest_status_schema_missing_fields_and_duplicate_roles(self):
        mutations = (
            lambda manifest: manifest.update(schema_version=2),
            lambda manifest: manifest.update(status='download_failed'),
            lambda manifest: manifest.pop('cik'),
            lambda manifest: manifest['files'][0].update(role='filing_index'),
            lambda manifest: manifest['files'][1].update(status='failed'),
            lambda manifest: manifest['files'][2].update(verification={'result': 'failed'}),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, manifest = write_sample_fixture(root)
                mutate(manifest)
                path.write_text(json.dumps(manifest), encoding='utf-8')
                connection = Mock()
                with self.assertRaises(db.DatabaseError):
                    db.seed_manifest(connection, path, root)
                self.assertEqual(connection.mock_calls, [])

    def test_paths_cannot_escape_root_or_refer_to_one_file_twice(self):
        for invalid in ('../outside.xml', 'source-0.xml'):
            with self.subTest(path=invalid), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, manifest = write_sample_fixture(root)
                manifest['files'][0]['local_path'] = invalid
                path.write_text(json.dumps(manifest), encoding='utf-8')
                connection = Mock()
                with self.assertRaisesRegex(db.DatabaseError, 'contained within the data root'):
                    db.seed_manifest(connection, path, root)
                self.assertEqual(connection.mock_calls, [])

    def test_malformed_manifest_and_nested_objects_fail_without_writes(self):
        mutations = (
            lambda manifest: [],
            lambda manifest: dict(manifest, files=None),
            lambda manifest: dict(manifest, index_document=[]),
            lambda manifest: dict(manifest, files=['invalid', *manifest['files'][1:]]),
            lambda manifest: dict(manifest, index_document=dict(manifest['index_document'], verification=[])),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, manifest = write_sample_fixture(root)
                path.write_text(json.dumps(mutate(manifest)), encoding='utf-8')
                connection = Mock()
                with self.assertRaises(db.DatabaseError):
                    db.seed_manifest(connection, path, root)
                self.assertEqual(connection.mock_calls, [])

    def test_missing_sources_and_wrong_sizes_fail_before_writes(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, manifest = write_sample_fixture(root)
                source = root / manifest['files'][1]['local_path']
                if missing:
                    source.unlink()
                else:
                    manifest['files'][1]['size_bytes'] += 1
                    path.write_text(json.dumps(manifest), encoding='utf-8')
                connection = Mock()
                with self.assertRaises(db.DatabaseError):
                    db.seed_manifest(connection, path, root)
                self.assertEqual(connection.mock_calls, [])

    def test_invalid_numeric_values_and_naive_retrieval_time_never_reach_sql(self):
        connection = Mock()
        fact = db.FactInput(
            filing_id=uuid4(), source_document_id=uuid4(), extraction_version='fixture-v1',
            occurrence_key='fact-1', concept_namespace='https://example.invalid/taxonomy',
            concept_name='Revenue', period_kind='forever', numeric_value=Decimal('1'),
        )
        for value in (1.25, 1, Decimal('NaN'), Decimal('Infinity'), Decimal('-Infinity')):
            with self.subTest(value=value), self.assertRaises(db.DatabaseError):
                db.store_fact(connection, replace(fact, numeric_value=value))
        source = db.SourceDocumentInput(
            filing_id=uuid4(), role='fixture', source_url='https://example.invalid/source',
            local_path='source.xml', sha256='0' * 64, size_bytes=0,
            retrieved_at=datetime(2026, 1, 1),
        )
        with self.assertRaisesRegex(db.DatabaseError, 'timezone'):
            db.register_source_document(connection, source)
        self.assertEqual(connection.mock_calls, [])

    def test_write_helpers_reject_autocommit_connections(self):
        connection = Mock(autocommit=True)
        with self.assertRaisesRegex(db.DatabaseError, 'caller-managed'):
            db.register_company(connection, db.CompanyInput(cik='0000000042', name='Fixture'))
        connection.execute.assert_not_called()

    def test_missing_packaged_migrations_fail_without_database_operations(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'files', return_value=Path(directory)):
            connection = Mock()
            with self.assertRaisesRegex(db.DatabaseError, 'missing'):
                db.migrate(connection)
            self.assertEqual(connection.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
