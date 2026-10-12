"""Run opt-in ingestion transaction tests against an empty local test database."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def local_test_settings(env_file: Path, database: str) -> dict:
    """Use the explicit file; ambient PostgreSQL settings must not redirect tests."""
    if not re.fullmatch(r"[A-Za-z0-9_]+_test(?:_[A-Za-z0-9_]+)?", database):
        raise ValueError("Choose a dedicated database name ending in _test or _test_SUFFIX.")
    if not env_file.is_file():
        raise ValueError("The local environment file does not exist.")
    sys.path.insert(0, str(ROOT / "src"))
    from sec_pipeline.database import DatabaseError, connection_settings

    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("PG", "POSTGRES_"))
    }
    with patch.dict(os.environ, environment, clear=True):
        try:
            settings = connection_settings(env_file)
        except DatabaseError as exc:
            raise ValueError(str(exc)) from exc
    if settings["host"] not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("These write tests require a local loopback database host.")
    if database == settings["dbname"]:
        raise ValueError("The test database must differ from the application database.")
    return settings | {
        "dbname": database,
        "hostaddr": "::1" if settings["host"] == "::1" else "127.0.0.1",
        "application_name": "sec-ingestion-fixture-tests",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env.local"))
    parser.add_argument("--database", default="sec_filings_test")
    args = parser.parse_args(argv)
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("PG", "POSTGRES_"))
    }
    with patch.dict(os.environ, environment, clear=True):
        try:
            settings = local_test_settings(args.env_file, args.database)
        except ValueError as exc:
            print(f"Local database tests stopped: {exc}", file=sys.stderr)
            return 2
        spec = importlib.util.spec_from_file_location(
            "test_ingestion_postgres", ROOT / "tests" / "test_ingestion_postgres.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module.IngestionPostgresTests.connection_options = settings
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(module.IngestionPostgresTests)
        expected_tests = suite.countTestCases()
        print(
            f"Local PostgreSQL: {settings['host']}:{settings['port']} / {settings['dbname']}",
            flush=True,
        )
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if result.skipped or result.testsRun != expected_tests:
            print("Live database tests did not all run.", file=sys.stderr)
            return 1
        if not result.wasSuccessful():
            return 1
        print("Fixture records cleaned; original source files unchanged.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
