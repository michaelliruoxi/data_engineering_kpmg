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
    observed = SimpleNamespace(size=0, queries=[], applied=applied, privileges=[], scope=None)
    monkeypatch.setattr(cli, 'open_state', lambda *args: (
        setattr(observed, 'scope', args) or {'filings': {'fixture': {}}}, {}))

    def execute(query, params=None):
        observed.queries.append(query)
        if query == 'SET TRANSACTION READ ONLY':
            return None
        if query == 'SELECT name,sha256 FROM sec.schema_migrations':
            return SimpleNamespace(fetchall=lambda: observed.applied)
        if query == 'SELECT has_table_privilege(current_user,%s,%s)':
            observed.privileges.append(params)
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


def _all_applied():
    return [(path.name, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted((ROOT / 'src/sec_pipeline/migrations').glob('[0-9][0-9][0-9]_*.sql'))]


@pytest.fixture
def reviewed(preflight, monkeypatch):
    """The real reviewed profile: its migrations and tables are not mocked."""
    from sec_pipeline import backfill
    monkeypatch.setattr(preflight.cli, 'adapter_profile', backfill.adapter_profile)
    return preflight


def test_default_scope_is_the_established_queue_and_legacy_schema(preflight, capsys):
    assert preflight.cli.main([]) == 0
    assert preflight.observed.scope == (preflight.cli.STATE, preflight.cli.UNIVERSE, '2021-10-13', '2026-10-13')
    assert 'tables-v3; schema 001-009;' in capsys.readouterr().out
    assert {table for table, _ in preflight.observed.privileges} == {'sec.' + name for name in preflight.cli.TABLES}


def test_reviewed_profile_is_not_ready_on_the_legacy_schema(reviewed, capsys):
    """A database at 001-009 passes for tables-v3 but cannot start tables-v6."""
    assert reviewed.cli.main(['--tables-version', 'tables-v6']) == 1
    captured = capsys.readouterr()
    assert 'preflight passed' not in captured.out
    for number in ('010', '011', '012', '013', '014', '015'):
        assert number + '_' in captured.err
    assert '009_' not in captured.err
    assert not reviewed.observed.privileges
    assert 'SELECT pg_database_size(current_database())' not in reviewed.observed.queries


@pytest.mark.parametrize('version', ['tables-v5', 'tables-v6'])
def test_reviewed_profile_passes_only_with_every_migration_and_source_set_table(reviewed, capsys, version):
    reviewed.observed.applied = _all_applied()
    assert reviewed.cli.main(['--tables-version', version]) == 0
    assert f'{version}; schema 001-015;' in capsys.readouterr().out
    checked = {table for table, _ in reviewed.observed.privileges}
    assert checked == {'sec.' + name for name in reviewed.cli.TABLES + reviewed.cli.SOURCE_SET_TABLES}
    assert {privilege for _, privilege in reviewed.observed.privileges} == {'SELECT', 'INSERT'}
    assert reviewed.observed.queries[0] == 'SET TRANSACTION READ ONLY'


def test_document_set_v4_keeps_the_legacy_schema_but_checks_source_set_tables(reviewed, capsys):
    assert reviewed.cli.main(['--tables-version', 'tables-v4']) == 0
    assert 'tables-v4; schema 001-009;' in capsys.readouterr().out
    assert ('sec.source_processing_sets', 'INSERT') in reviewed.observed.privileges


def test_changed_applied_migration_is_named_and_blocks(reviewed, capsys):
    reviewed.observed.applied = [(name, 'f' * 64 if name.startswith('012_') else digest)
                                 for name, digest in _all_applied()]
    assert reviewed.cli.main(['--tables-version', 'tables-v6']) == 1
    error = capsys.readouterr().err
    assert '012_publication_readers.sql' in error and '013_' not in error
    assert not reviewed.observed.privileges


def test_explicit_scope_is_forwarded_and_requires_all_four_values(preflight, tmp_path, capsys):
    state, universe = tmp_path / 'scoped-queue.json', tmp_path / 'scoped-universe.json'
    state.write_text('{}')
    universe.write_text('{}')
    scope = ['--state', str(state), '--universe', str(universe), '--since', '2021-01-01', '--until', '2026-11-09']
    assert preflight.cli.main(scope) == 0
    assert preflight.observed.scope == (state, universe, '2021-01-01', '2026-11-09')
    for missing in range(0, len(scope), 2):
        with pytest.raises(SystemExit) as exc:
            preflight.cli.main(scope[:missing] + scope[missing + 2:])
        assert exc.value.code == 2
    with pytest.raises(SystemExit):
        preflight.cli.main(scope[:5] + ['2021-1-1'] + scope[6:])


def test_scoped_queue_lock_blocks_before_database_access(preflight, tmp_path, capsys):
    state, universe = tmp_path / 'scoped-queue.json', tmp_path / 'scoped-universe.json'
    state.write_text('{}')
    universe.write_text('{}')
    (tmp_path / 'scoped-queue.json.lock').write_text('4242')
    assert preflight.cli.main(['--state', str(state), '--universe', str(universe),
                               '--since', '2021-01-01', '--until', '2026-11-09']) == 1
    assert 'queue lock exists' in capsys.readouterr().err
    preflight.connect.assert_not_called()


@pytest.mark.parametrize('settings,expected', [
    ({'sslmode': 'verify-full', 'host': 'database.example.com'}, 0),
    ({'sslmode': 'require', 'host': 'database.example.com'}, 1),
    ({'host': 'database.example.com'}, 1),
    ({'sslmode': 'disable', 'host': '127.0.0.1'}, 0),
    ({'sslmode': 'disable', 'host': 'localhost'}, 0),
    ({'sslmode': 'disable', 'host': '::1'}, 0),
    ({'sslmode': 'disable', 'host': '10.0.0.5'}, 1),
])
def test_only_a_loopback_rehearsal_database_may_omit_verified_tls(preflight, monkeypatch, settings, expected):
    monkeypatch.setattr(preflight.cli, 'ingestion_settings', lambda *args: settings)
    assert preflight.cli.main([]) == expected
    assert preflight.connect.called is (expected == 0)


def test_env_file_and_database_select_the_checked_target(preflight, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(preflight.cli, 'ingestion_settings',
                        lambda *args: seen.append(args) or {'sslmode': 'verify-full'})
    assert preflight.cli.main([]) == 0
    assert preflight.cli.main(['--env-file', str(tmp_path / 'rehearsal.env'), '--database', 'sec_rehearsal']) == 0
    assert seen == [(preflight.cli.ENV_FILE, 'sec_filings'), (tmp_path / 'rehearsal.env', 'sec_rehearsal')]
