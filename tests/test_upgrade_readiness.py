"""The pre-migration inventory predicts the migrator and never writes."""

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest

from sec_pipeline import database as db
from sec_pipeline import upgrade_readiness as readiness
from sec_pipeline.parser_policy import policy_metadata


PACKAGED = db._packaged_migrations()
NAMES = [name for name, _ in PACKAGED]


def ledger(names=NAMES):
    return {name: db.migration_digest(content) for name, content in PACKAGED if name in names}


def test_plan_matches_each_migrator_outcome():
    assert readiness.migration_plan(ledger())['status'] == 'current'
    plan = readiness.migration_plan(ledger(NAMES[:9]))
    assert plan['status'] == 'pending' and plan['pending'] == NAMES[9:] and plan['applied'] == NAMES[:9]
    assert not plan['blockers']
    assert readiness.migration_plan({})['pending'] == NAMES

    changed = ledger() | {NAMES[11]: 'f' * 64}
    plan = readiness.migration_plan(changed)
    assert plan['status'] == 'blocked' and plan['checksum_mismatches'] == [NAMES[11]]

    plan = readiness.migration_plan(ledger() | {'999_private.sql': 'a' * 64})
    assert plan['status'] == 'blocked' and plan['unknown_applied'] == ['999_private.sql']

    plan = readiness.migration_plan(ledger([*NAMES[:9], NAMES[10]]))
    assert plan['status'] == 'blocked' and plan['applied_after_pending'] == [NAMES[10]]
    assert plan['blockers'] == ['applied_history_is_not_a_contiguous_prefix']


def test_plan_accepts_a_ledger_recorded_from_a_crlf_checkout():
    crlf = {name: hashlib.sha256(content.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')).hexdigest()
            for name, content in PACKAGED}
    assert readiness.migration_plan(crlf)['status'] == 'current'


class Inventory:
    """Dispatch the exact statements; any other SQL fails the test."""

    def __init__(self, *, applied=NAMES, relations=None, read_only='on', legacy=(), source=(), served=(),
                 impact=(0, 0), sessions=0, reader=True):
        self.applied, self.read_only, self.statements = ledger(applied), read_only, []
        self.relations = {'sec.schema_migrations', 'sec.filing_processing_sets', 'sec.reports',
                          'sec.source_processing_sets', 'finance.filing'} if relations is None else relations
        self.rows = {readiness.LEGACY_RECEIPTS_SQL: list(legacy), readiness.SOURCE_RECEIPTS_SQL: list(source),
                     'SELECT processing_family, count(*) FROM finance.filing GROUP BY 1 ORDER BY 1': list(served)}
        self.single = {'SHOW transaction_read_only': (read_only,),
                       'SELECT transaction_timestamp()': (datetime(2026, 11, 10, 12, tzinfo=timezone.utc),),
                       'SELECT pg_database_size(current_database())': (5 * 1024**3,),
                       readiness.READER_IMPACT_SQL: impact}
        self.sessions, self.reader = sessions, reader
        self.info = SimpleNamespace(dbname='sec_filings', server_version=170011)

    def execute(self, statement, parameters=None):
        self.statements.append(statement)
        assert not statement.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP', 'GRANT'))
        if statement == 'SELECT to_regclass(%s)':
            return SimpleNamespace(fetchone=lambda: (parameters[0] if parameters[0] in self.relations else None,))
        if statement == 'SELECT name, sha256 FROM sec.schema_migrations ORDER BY name':
            return SimpleNamespace(fetchall=lambda: sorted(self.applied.items()))
        if 'pg_stat_activity' in statement:
            return SimpleNamespace(fetchone=lambda: (self.sessions,))
        if 'pg_roles' in statement:
            return SimpleNamespace(fetchone=lambda: (self.reader,))
        if statement in self.rows:
            return SimpleNamespace(fetchall=lambda: self.rows[statement])
        return SimpleNamespace(fetchone=lambda: self.single[statement])


V3 = {'facts': 'facts-v1', 'tables': 'tables-v3', 'reports': 'report-v1', 'chunks': 'paragraph-v1'}
V6 = {'facts': 'facts-v1', 'tables': 'tables-v6', 'reports': 'report-v3', 'chunks': 'paragraph-v2'}
OLD_REVISIONS = {'facts': 'numeric-tag-boundaries-v1', 'reports': 'legacy-visible-layout-boundaries-v1'}


def test_inventory_requires_a_read_only_transaction():
    with pytest.raises(ValueError, match='Read-only'):
        readiness.read_inventory(Inventory(read_only='off'))


def test_receipts_are_classified_by_the_revisions_they_recorded():
    current_v3 = policy_metadata(V3)['parser_revisions']
    conn = Inventory(
        legacy=[('filing-import-v3', 'facts-v1', 'tables-v3', 'report-v1', 'paragraph-v1', 'passed', OLD_REVISIONS, 90),
                ('filing-import-v3', 'facts-v1', 'tables-v3', 'report-v1', 'paragraph-v1', 'passed', None, 7),
                ('filing-import-v3', 'facts-v1', 'tables-v3', 'report-v1', 'paragraph-v1', 'passed', current_v3, 4),
                ('filing-import-v3-text', 'facts-v1', None, 'report-v1', 'paragraph-v1', 'passed', None, 5),
                ('manual-example-v1', 'manual-facts', 'manual-tables', 'manual-report', 'manual-chunks', 'passed', None, 1)],
        source=[('automated-v6', V6, 'passed', policy_metadata(V6)['parser_revisions'], 3),
                ('filing-import-v4', V3 | {'tables': 'tables-v4'}, 'review_required', None, 2)],
        served=[(None, 40), ('legacy', 100), ('source', 12)], impact=(9, 6))
    inventory = readiness.read_inventory(conn)
    stale = [(row['set_key'], row['processing_sets']) for row in inventory['receipts'] if row['stale_for_this_checkout']]
    assert stale == [('filing-import-v3', 90), ('filing-import-v3', 7), ('filing-import-v3-text', 5), ('filing-import-v4', 2)]
    assert inventory['receipts'][3]['versions'] == {'facts': 'facts-v1', 'reports': 'report-v1', 'chunks': 'paragraph-v1'}
    assert inventory['served_selection_counts'] == {'none': 40, 'legacy': 100, 'source': 12}
    assert inventory['reader_impact'] == {'status': 'counted', 'legacy_served_filings_with_passed_source_set': 9,
                                          'legacy_served_filings_with_passed_v4_set': 6}
    assessment = readiness.assess(inventory)
    assert assessment == {'status': 'current', 'attention': [], 'pending_migrations': [], 'stale_processing_sets': 104}
    json.dumps(inventory)


def test_legacy_schema_reports_the_pending_batch_and_the_reader_count():
    conn = Inventory(applied=NAMES[:10], impact=(9, 6))
    assessment = readiness.assess(readiness.read_inventory(conn))
    assert assessment['pending_migrations'] == NAMES[10:]
    assert assessment['status'] == 'review_required'
    assert assessment['attention'] == ['default_source_readers_lose_rows_for_legacy_served_filings']


def test_no_reader_impact_and_idle_database_is_ready_to_migrate():
    assessment = readiness.assess(readiness.read_inventory(Inventory(applied=NAMES[:10])))
    assert assessment['status'] == 'ready_to_migrate' and not assessment['attention']


def test_schema_before_serving_views_cannot_count_reader_impact_and_says_so():
    conn = Inventory(applied=NAMES[:9], relations={'sec.schema_migrations', 'sec.filing_processing_sets',
                                                   'sec.reports', 'sec.source_processing_sets'},
                     sessions=2, reader=False)
    inventory = readiness.read_inventory(conn)
    assert inventory['reader_impact']['status'] == 'not_available' and inventory['served_selection_counts'] is None
    assert readiness.READER_IMPACT_SQL not in conn.statements
    assert readiness.assess(inventory)['attention'] == [
        'reader_impact_not_counted_before_migration_012', 'other_sessions_active',
        'sec_reader_role_absent_so_reader_grants_are_skipped']


def test_empty_database_reports_everything_pending_without_querying_missing_relations():
    conn = Inventory(applied=(), relations=set())
    inventory = readiness.read_inventory(conn)
    assert inventory['migrations']['pending'] == NAMES and inventory['receipts'] is None
    assert not any('processing_sets' in statement or 'finance.filing' in statement for statement in conn.statements)
    assert readiness.assess(inventory)['stale_processing_sets'] is None


def test_blocked_history_overrides_other_statuses():
    conn = Inventory(applied=[*NAMES[:9], NAMES[10]])
    assert readiness.assess(readiness.read_inventory(conn))['status'] == 'blocked'


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    module = importlib.import_module('check_upgrade_ready')
    conn = Inventory(applied=NAMES[:10])
    events = []

    @contextmanager
    def transaction():
        events.append('begin')
        yield
        events.append('end')

    conn.transaction = transaction
    monkeypatch.setattr(module, 'connect', Mock(return_value=nullcontext(conn)))
    monkeypatch.setattr(module, 'explicit_database_environment', nullcontext)
    monkeypatch.setattr(module, 'ingestion_settings', lambda *args: {
        'host': 'database.example.com', 'port': 5432, 'dbname': args[1], 'password': 'never-recorded'})
    monkeypatch.setattr(module, 'check_connection_target', lambda *args: None)
    return SimpleNamespace(module=module, conn=conn, events=events)


def arguments(output):
    return ['--env-file', 'unused.env', '--database', 'sec_filings', '--output', str(output)]


def test_cli_reads_in_one_repeatable_read_only_transaction_and_records_no_secret(cli, tmp_path, capsys):
    output = tmp_path / 'evidence' / 'before.json'
    assert cli.module.main(arguments(output)) == 0
    assert cli.conn.statements[0] == 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'
    assert cli.events == ['begin', 'end']
    evidence = json.loads(output.read_text())
    assert evidence['destination'] == {'host': 'database.example.com', 'port': 5432, 'dbname': 'sec_filings'}
    assert 'never-recorded' not in output.read_text()
    assert evidence['assessment']['status'] == 'ready_to_migrate'
    assert evidence['assessment']['pending_migrations'] == NAMES[10:]
    assert json.loads(capsys.readouterr().out)['output'] == str(output)


def test_cli_exit_code_two_records_a_decision_or_blocker(cli, tmp_path):
    cli.conn.single[readiness.READER_IMPACT_SQL] = (3, 1)
    assert cli.module.main(arguments(tmp_path / 'review.json')) == 2
    cli.conn.applied = ledger([*NAMES[:9], NAMES[10]])
    assert cli.module.main(arguments(tmp_path / 'blocked.json')) == 2
    assert json.loads((tmp_path / 'blocked.json').read_text())['assessment']['status'] == 'blocked'


def test_cli_never_replaces_earlier_evidence(cli, tmp_path):
    output = tmp_path / 'before.json'
    output.write_text('earlier evidence')
    with pytest.raises(SystemExit) as stopped:
        cli.module.main(arguments(output))
    assert stopped.value.code == 2 and output.read_text() == 'earlier evidence'
    cli.module.connect.assert_not_called()


def test_cli_withholds_connection_errors_and_writes_nothing(cli, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.module, 'connect', Mock(side_effect=psycopg.OperationalError(
        'connection to host database.example.com password=never-recorded failed')))
    output = tmp_path / 'before.json'
    assert cli.module.main(arguments(output)) == 1
    captured = capsys.readouterr()
    assert 'never-recorded' not in captured.err + captured.out and 'OperationalError' in captured.err
    assert not output.exists()


@pytest.mark.skipif(os.environ.get('SEC_DB_TEST') != '1', reason='Dedicated local PostgreSQL checks are opt-in.')
def test_every_inventory_statement_executes_read_only_in_the_migrated_database():
    from tests.test_catalog_postgres import local_test_options

    with psycopg.connect(**local_test_options(), autocommit=True) as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            inventory = readiness.read_inventory(conn)
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                with conn.transaction():
                    conn.execute('UPDATE sec.companies SET name=name WHERE false')
    assert inventory['migrations']['status'] == 'current' and inventory['migrations']['applied'] == NAMES
    assert inventory['reader_impact']['status'] == 'counted'
    assert type(inventory['reader_impact']['legacy_served_filings_with_passed_source_set']) is int
    assert isinstance(inventory['receipts'], list) and isinstance(inventory['served_selection_counts'], dict)
    assert readiness.assess(inventory)['status'] == 'current'
    json.dumps(inventory)


@pytest.mark.skipif(os.environ.get('SEC_DB_CONCURRENCY_TEST') != '1',
                    reason='Set SEC_DB_CONCURRENCY_TEST=1 for disposable local database tests.')
def test_plan_predicts_the_real_migrator_on_a_database_at_the_legacy_schema():
    """Rebuild 001-009 in a database this test created, then compare with migrate()."""
    from tests.test_catalog_postgres import disposable_catalog_database

    with disposable_catalog_database() as options, psycopg.connect(**options) as conn:
        conn.execute('DROP SCHEMA finance CASCADE')
        conn.execute('DROP SCHEMA sec CASCADE')
        conn.execute('CREATE SCHEMA sec')
        conn.execute('CREATE TABLE sec.schema_migrations (name text PRIMARY KEY, sha256 text NOT NULL, '
                     'applied_at timestamptz NOT NULL DEFAULT now())')
        for name, content in PACKAGED[:9]:
            conn.execute(content.replace(b'\r\n', b'\n').decode('utf-8'))
            conn.execute('INSERT INTO sec.schema_migrations(name,sha256) VALUES (%s,%s)',
                         (name, db.migration_digest(content)))
        conn.commit()
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            before = readiness.read_inventory(conn)
        assert before['migrations']['status'] == 'pending' and before['migrations']['pending'] == NAMES[9:]
        assert before['reader_impact']['status'] == 'not_available'
        assert readiness.assess(before)['attention'][0] == 'reader_impact_not_counted_before_migration_012'
        assert db.migrate(conn) == before['migrations']['pending']
        conn.commit()
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            after = readiness.read_inventory(conn)
        assert after['migrations']['status'] == 'current' and after['reader_impact']['status'] == 'counted'
