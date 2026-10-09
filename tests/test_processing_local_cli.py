"""Local write-test target guards, without opening any database connection."""

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("local_ingestion_tests", ROOT / "scripts" / "test_ingestion_local.py")
command = importlib.util.module_from_spec(spec)
spec.loader.exec_module(command)
live_spec = importlib.util.spec_from_file_location("live_ingestion_targets", ROOT / "tests" / "test_ingestion_postgres.py")
live = importlib.util.module_from_spec(live_spec)
live_spec.loader.exec_module(live)


class LocalIngestionTargetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env_file = Path(directory.name) / "local.env"
        self.env_file.write_text(
            "POSTGRES_DB=sec_filings\nPOSTGRES_USER=fixture_user\nPOSTGRES_PASSWORD=fixture-only\n"
            "PGHOST=127.0.0.1\nPGSSLMODE=disable\n", encoding="utf-8",
        )
        self.enterContext(patch("psycopg.connect", side_effect=AssertionError("Unexpected database connection")))

    def test_local_target_uses_explicit_file_and_ignores_ambient_rds_settings(self):
        with patch.dict(os.environ, {
            "PGHOST": "shared.example.invalid", "PGHOSTADDR": "203.0.113.1", "PGPORT": "6543",
            "PGDATABASE": "shared", "PGUSER": "shared_reader", "PGPASSWORD": "ambient-only",
            "PGSERVICE": "shared", "PGSSLMODE": "verify-full",
        }):
            settings = command.local_test_settings(self.env_file, "sec_filings_test")
            self.assertEqual((settings["host"], settings["hostaddr"], settings["port"], settings["dbname"]),
                ("127.0.0.1", "127.0.0.1", 5432, "sec_filings_test"))
            self.assertEqual((settings["user"], settings["password"], settings["sslmode"]),
                ("fixture_user", "fixture-only", "disable"))
            self.assertEqual(os.environ["PGHOST"], "shared.example.invalid")

    def test_remote_host_in_explicit_file_is_rejected_before_connecting(self):
        self.env_file.write_text(self.env_file.read_text().replace("127.0.0.1", "shared.example.invalid"))
        with self.assertRaisesRegex(ValueError, "loopback"):
            command.local_test_settings(self.env_file, "sec_filings_test")

    def test_application_database_cannot_be_selected_even_if_named_test(self):
        self.env_file.write_text(self.env_file.read_text().replace("POSTGRES_DB=sec_filings\n", "POSTGRES_DB=sec_filings_test\n"))
        with self.assertRaisesRegex(ValueError, "differ"):
            command.local_test_settings(self.env_file, "sec_filings_test")

    def test_non_test_and_invalid_database_names_are_rejected(self):
        for name in ("sec_filings", "test", "sec_filings_test;DROP", "sec_filings_test_", "sec_filings-test"):
            with self.subTest(database=name), self.assertRaisesRegex(ValueError, "dedicated"):
                command.local_test_settings(self.env_file, name)

    def test_missing_file_cannot_fall_back_to_process_credentials(self):
        self.env_file.unlink()
        with patch.dict(os.environ, {"PGPASSWORD": "ambient-only"}), self.assertRaisesRegex(ValueError, "does not exist"):
            command.local_test_settings(self.env_file, "sec_filings_test")

    def test_missing_file_password_is_an_actionable_error_without_ambient_fallback(self):
        self.env_file.write_text(self.env_file.read_text().replace("POSTGRES_PASSWORD=fixture-only\n", ""))
        with patch.dict(os.environ, {"PGPASSWORD": "ambient-only"}), self.assertRaisesRegex(ValueError, "password|PASSWORD"):
            command.local_test_settings(self.env_file, "sec_filings_test")


class DockerConnectionTargetTests(unittest.TestCase):
    class Connection:
        def __init__(self, database="sec_filings_test", client_address="127.0.0.1", server_address="172.18.0.2"):
            self.database = database
            self.server_address = server_address
            self.info = SimpleNamespace(hostaddr=client_address)

        def execute(self, statement):
            if statement == "SELECT current_database()":
                return SimpleNamespace(fetchone=lambda: (self.database,))
            if statement == "SELECT current_database(), inet_server_addr()::text":
                return SimpleNamespace(fetchone=lambda: (self.database, self.server_address))
            raise AssertionError("The target check must only read connection/database identity.")

    def options(self, address="127.0.0.1"):
        return {"dbname": "sec_filings_test", "hostaddr": address}

    def test_docker_bridge_address_does_not_replace_the_local_client_target(self):
        for client, server in (("127.0.0.1", "172.18.0.2"), ("127.0.0.1", "192.168.65.3"), ("::1", "172.18.0.2")):
            with self.subTest(client=client, server=server):
                live.check_connection_target(self.Connection(client_address=client, server_address=server), self.options(client))

    def test_direct_local_postgres_is_accepted(self):
        for address in ("127.0.0.1", "::1"):
            with self.subTest(address=address):
                live.check_connection_target(self.Connection(client_address=address, server_address=address), self.options(address))

    def test_remote_client_address_is_rejected_even_when_server_reports_loopback(self):
        with self.assertRaisesRegex(RuntimeError, "local"):
            live.check_connection_target(self.Connection(client_address="203.0.113.1", server_address="127.0.0.1"), self.options())

    def test_different_actual_database_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "database"):
            live.check_connection_target(self.Connection(database="sec_filings", server_address="127.0.0.1"), self.options())

    def test_actual_address_must_match_the_requested_loopback_address(self):
        with self.assertRaisesRegex(RuntimeError, "local"):
            live.check_connection_target(self.Connection(server_address="::1"), self.options("::1"))


if __name__ == "__main__":
    unittest.main()
