"""CLI validation before connecting, and explicit catalog selection wiring."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sec_pipeline import db_cli


class CatalogCliTests(unittest.TestCase):
    def specification(self, payload):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(temporary) / 'selection.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def test_invalid_spec_never_connects(self):
        for payload in ([], {}, {'filing_id': 'invalid-uuid'}, {'filing_id': 123}):
            with self.subTest(payload=payload), patch.object(db_cli, 'connect') as connect:
                with redirect_stderr(io.StringIO()):
                    result = db_cli.main(['processing-set', '--spec', str(self.specification(payload))])
                self.assertEqual(result, 1)
                connect.assert_not_called()

    def test_json_uuid_and_explicit_preference_are_passed_in_one_transaction(self):
        filing, report, selected = uuid4(), uuid4(), uuid4()
        path = self.specification(dict(
            filing_id=str(filing), report_id=str(report), set_key='excerpt-v1',
            coverage=dict(facts='absent', tables='absent', reports='partial', chunks='absent'),
            validation_status='passed', validation={'checks': {'source_match': True}}, configuration={},
        ))
        connection = MagicMock()
        with patch.object(db_cli, 'connect', return_value=connection), \
             patch('sec_pipeline.catalog.store_processing_set', return_value=selected) as store, \
             patch('sec_pipeline.catalog.select_preferred_processing_set') as prefer, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(db_cli.main(['processing-set', '--spec', str(path), '--prefer']), 0)
        conn, record = store.call_args.args
        self.assertIs(conn, connection.__enter__.return_value)
        self.assertEqual((record.filing_id, record.report_id), (filing, report))
        prefer.assert_called_once_with(conn, filing, selected)
        self.assertEqual(json.loads(output.getvalue())['processing_set_id'], str(selected))


if __name__ == '__main__':
    unittest.main()
