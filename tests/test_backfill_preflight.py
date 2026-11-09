"""The preflight honors the same configurable size cutoff as ingestion."""

from contextlib import nullcontext
import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest


GIB = 1024**3
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def preflight(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    cli = importlib.import_module('check_backfill_ready')
    state_path, universe_path = tmp_path / 'queue.json', tmp_path / 'universe.json'
    state_path.write_text('{}')
    universe_path.write_text('{}')
    monkeypatch.setattr(cli, 'STATE', state_path)
    monkeypatch.setattr(cli, 'UNIVERSE', universe_path)
    monkeypatch.setattr(cli, 'open_state', lambda *args: ({'filings': {'fixture': {}}}, {}))
    monkeypatch.setattr(cli, 'adapter_profile', lambda *args: (SimpleNamespace(VERSIONS={'tables': 'tables-v3'}), None))
    monkeypatch.setattr(cli, 'explicit_database_environment', nullcontext)
    monkeypatch.setattr(cli, 'ingestion_settings', lambda *args: {'sslmode': 'verify-full'})
    monkeypatch.setattr(cli, 'check_connection_target', Mock())
    applied = [(path.name, hashlib.sha256(path.read_bytes()).hexdigest())
               for path in (ROOT / 'src/sec_pipeline/migrations').glob('*.sql')
               if path.name[:3].isdigit() and int(path.name[:3]) <= 9]
    conn = MagicMock()
    conn.__enter__.return_value = conn
    observed = SimpleNamespace(size=0, queries=[])

    def execute(query, params=None):
        observed.queries.append(query)
        if query == 'SET TRANSACTION READ ONLY':
            return None
        if query == 'SELECT name,sha256 FROM sec.schema_migrations':
            return SimpleNamespace(fetchall=lambda: applied)
        if query == 'SELECT has_table_privilege(current_user,%s,%s)':
            return SimpleNamespace(fetchone=lambda: (True,))
        if query == 'SELECT pg_database_size(current_database())':
            return SimpleNamespace(fetchone=lambda: (observed.size,))
        raise AssertionError(f'Unexpected SQL: {query}')

    conn.execute.side_effect = execute
    connect = Mock(return_value=conn)
    monkeypatch.setattr(cli, 'connect', connect)
    return SimpleNamespace(cli=cli, observed=observed, connect=connect)


@pytest.mark.parametrize('arguments,limit', [([], 14), (['--max-database-gib', '35'], 35)])
@pytest.mark.parametrize('offset,expected', [(-1, 0), (0, 2), (1, 2)])
def test_preflight_cutoff_is_inclusive_in_bytes(preflight, capsys, arguments, limit, offset, expected):
    preflight.observed.size = limit * GIB + offset
    assert preflight.cli.main(arguments) == expected
    output = capsys.readouterr().out
    assert f'{limit} GiB' in output
    assert ('preflight passed' in output) == (expected == 0)
    assert preflight.observed.queries[0] == 'SET TRANSACTION READ ONLY'


def test_explicit_limit_allows_database_above_old_cutoff(preflight, capsys):
    preflight.observed.size = 15 * GIB
    assert preflight.cli.main(['--max-database-gib', '35']) == 0
    assert 'limit 35 GiB' in capsys.readouterr().out


@pytest.mark.parametrize('invalid', ['0', '-1', 'NaN', 'inf', '14.5'])
def test_invalid_limits_fail_before_database_access(preflight, invalid):
    with pytest.raises(SystemExit) as exc:
        preflight.cli.main(['--max-database-gib', invalid])
    assert exc.value.code == 2
    preflight.connect.assert_not_called()
