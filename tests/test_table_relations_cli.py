"""CLI boundaries: offline IDs, source containment, SSL and password handling."""
import json
import os
from types import SimpleNamespace

import psycopg
import pytest

from sec_pipeline.database import DatabaseError
from sec_pipeline import table_relations_cli as cli
from tests.test_table_relations import SOURCE, ACCESSION, fixture


def test_offline_CLI_erases_placeholder_UUIDs_and_preserves_source_locator(tmp_path):
    table, _, sections, _ = fixture()
    source, tables_file, sections_file = [tmp_path / name for name in ("source.htm", "table.json", "sections.json")]
    source.write_bytes(SOURCE)
    tables_file.write_text(json.dumps(table))
    sections_file.write_text(json.dumps(sections))
    output = tmp_path / "out"
    assert cli.main(["offline", "--tables", str(tables_file), "--sections", str(sections_file), "--html", str(source),
                     "--accession", ACCESSION, "--output", str(output)]) == 0
    mapping, = json.loads((output / "table-section-mapping.json").read_text())
    assert mapping["mapping_status"] == "mapped"
    assert mapping["table_id"] is mapping["section_uuid"] is mapping["report_id"] is None
    assert mapping["section_key"] == "part-i-item-1"
    assert json.loads((output / "table-chunk-mapping.json").read_text())["status"] == "unverified"


def options(tmp_path):
    ca = tmp_path / "synthetic-ca.pem"
    ca.write_text("synthetic CA path for option validation only; no network")
    return SimpleNamespace(ca=ca, env_file=None, passfile=None, prompt_password=False)


def test_shared_connection_pins_identity_and_verify_full_over_private_env_options(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("PG", "POSTGRES_")):
            monkeypatch.delenv(key)
    args = options(tmp_path)
    args.env_file = tmp_path / ".env.private-fixture"
    args.env_file.write_text("PGHOST="+cli.HOST+"\nPGUSER=sec_jazzy\nPGDATABASE=sec_filings\nPGPASSWORD=SYNTHETIC-TEST-ONLY\nPGSSLMODE=disable\n")
    configured = cli.shared_options(args)
    assert configured["sslmode"] == "verify-full" and configured["sslrootcert"] == str(args.ca)
    assert "default_transaction_read_only=on" in configured["options"]
    args.env_file.write_text("PGHOST=not-team-rds.invalid\nPGPASSWORD=SYNTHETIC-TEST-ONLY\n")
    with pytest.raises(DatabaseError, match="endpoint"):
        cli.shared_options(args)


def test_hidden_password_input_rejects_noninteractive_echo_fallback(tmp_path, monkeypatch):
    args = options(tmp_path)
    args.prompt_password = True
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(cli, "getpass", lambda prompt: pytest.fail("Must not read with echo fallback"))
    with pytest.raises(DatabaseError, match="echo fallback"):
        cli.shared_options(args)


def test_connection_error_redacts_password_and_does_not_claim_zero_row_success(tmp_path, monkeypatch):
    secret = "SYNTHETIC-CREDENTIAL-FOR-REDACTION-TEST"
    monkeypatch.setattr(cli, "shared_options", lambda args: {"password": secret})
    def fail(**kwargs):
        raise psycopg.OperationalError("Synthetic connection error containing "+secret)
    monkeypatch.setattr(cli.psycopg, "connect", fail)
    output = tmp_path / "out"
    assert cli.main(["shared", "--accession", ACCESSION, "--source-root", str(tmp_path), "--ca", str(tmp_path / "none"),
                     "--output", str(output)]) == 2
    content = (output / "connection-status.json").read_text()
    assert secret not in content and "[redacted]" in content
    result = json.loads(content)
    assert result["snapshot_completed"] is False and result["status"] == "unverified"


def test_source_receipt_cannot_escape_explicit_archive_root(tmp_path):
    with pytest.raises(DatabaseError, match="escapes"):
        cli.source_bytes({"sources": [{"id": "any", "local_path": "../private.env"}]}, tmp_path)
