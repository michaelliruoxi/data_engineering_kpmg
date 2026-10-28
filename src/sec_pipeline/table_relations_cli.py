"""Read-only table lineage checks and deterministic JSON/CSV exports.

Shared mode is pinned to sec_jazzy on the team's RDS database. No code path
here writes database rows, runs migrations, selects a set, or assigns IDs.
"""
import argparse
import csv
from getpass import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import warnings

import psycopg
from psycopg.rows import dict_row

from .database import DatabaseError, connection_settings
from .table_chunk_links import resolve_table_chunks
from .table_relations import audit_snapshot, read_snapshot
from .table_sections import canonical, map_table_section


HOST = "sec-filings.cghyw6082mug.us-east-1.rds.amazonaws.com"
COLUMNS = ("accession", "processing_set", "source/document_set", "table_id", "table_key", "table_xpath",
           "section_uuid", "section_key", "section_title", "section_locator", "mapping_status", "evidence")


def save_json(path, value):
    path.write_text(json.dumps(canonical(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def write_outputs(output, result, *, snapshot=None, chunk_mappings=None):
    output.mkdir(parents=True, exist_ok=True)
    save_json(output / "relation-checks.json", result)
    mappings = result.get("section_mappings", [])
    save_json(output / "table-section-mapping.json", mappings)
    with (output / "table-section-mapping.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        for mapping in mappings:
            writer.writerow({key: json.dumps(mapping[key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                             if isinstance(mapping.get(key), (dict, list)) else mapping.get(key) for key in COLUMNS})
    save_json(output / "trace-examples.json", result.get("trace_examples", []))
    save_json(output / "table-chunk-mapping.json", chunk_mappings if chunk_mappings is not None else {
        "status": "unverified", "reason": "No actual table chunk supplied; paragraph chunks are not table chunks."})
    if snapshot is not None:
        save_json(output / "snapshot.json", snapshot)


def source_bytes(snapshot, root):
    """Read only source receipts under the explicitly chosen archive root."""
    root = root.resolve()
    result = {}
    for receipt in snapshot["sources"]:
        path = (root / receipt["local_path"]).resolve()
        if not path.is_relative_to(root):
            raise DatabaseError("Source receipt escapes the supplied archive root.")
        if path.is_file():
            result[str(receipt["id"])] = path.read_bytes()
    return result


def shared_options(args):
    options = {"host": HOST, "port": 5432, "dbname": "sec_filings", "user": "sec_jazzy"}
    if args.env_file:
        options.update(connection_settings(args.env_file))
    if (options["host"], int(options["port"]), options["dbname"], options["user"]) != (HOST, 5432, "sec_filings", "sec_jazzy"):
        raise DatabaseError("Shared checks require the named RDS endpoint, sec_filings and sec_jazzy.")
    if not args.ca.is_file():
        raise DatabaseError("An existing AWS CA bundle is required.")
    options.update(sslmode="verify-full", sslrootcert=str(args.ca), connect_timeout=10,
                   application_name="jazzy-table-relations-readonly",
                   options="-c default_transaction_read_only=on -c statement_timeout=20000")
    if args.passfile:
        options["passfile"] = str(args.passfile)
    if args.prompt_password:
        if not sys.stdin.isatty():
            raise DatabaseError("Hidden input requires your interactive terminal; echo fallback is prohibited.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            options["password"] = getpass("sec_jazzy password (hidden, memory only): ")
    # libpq also supports existing PGPASSFILE/PGPASSWORD/PGSERVICE settings.
    return options


def _chunk_output(args, snapshot, sources):
    if args.chunks is None:
        return None
    config = json.loads(args.field_config.read_text()) if args.field_config else {}
    selected = snapshot.get("selected_processing_set") or {}
    tables = [row | {"accession": snapshot["accession"], "processing_set": snapshot.get("selection"),
                      "source/document_set": selected.get("document_set_id")} for row in snapshot["tables"]]
    chunks = json.loads(args.chunks.read_text())
    if not isinstance(chunks, list):
        raise DatabaseError("Chunk input must be an explicit JSON array of producer records.")
    return resolve_table_chunks(chunks, tables, sources, input_fields=config.get("input_fields"),
                                output_fields=config.get("output_fields"), allow_synthetic=args.allow_synthetic)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    offline = sub.add_parser("offline", help="Reuse exported tables/sections; database UUIDs stay empty")
    offline.add_argument("--tables", type=Path, nargs="+", required=True)
    offline.add_argument("--sections", type=Path, required=True)
    offline.add_argument("--html", type=Path, required=True)
    offline.add_argument("--report-version", default="report-v1")
    offline.add_argument("--accession", required=True)
    offline.add_argument("--output", type=Path, required=True)
    for name in ("shared", "replay"):
        command = sub.add_parser(name)
        command.add_argument("--source-root", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--chunks", type=Path)
        command.add_argument("--field-config", type=Path)
        command.add_argument("--allow-synthetic", action="store_true")
        if name == "replay":
            command.add_argument("--snapshot", type=Path, required=True)
        else:
            command.add_argument("--accession", required=True)
            command.add_argument("--processing-set", help="Actual receiving-DB UUID, never a local fixture UUID")
            command.add_argument("--contract", choices=("legacy", "source-v4"))
            command.add_argument("--ca", type=Path, required=True)
            command.add_argument("--env-file", type=Path)
            command.add_argument("--passfile", type=Path)
            command.add_argument("--prompt-password", action="store_true")
    args = parser.parse_args(argv)
    if args.mode == "offline":
        source = args.html.read_bytes()
        sections = json.loads(args.sections.read_text())
        sections = [{k: v for k, v in section.items() if k not in {"id", "filing_id", "report_id"}} for section in sections]
        report = {"id": None, "filing_id": None, "source_document_id": None, "extraction_version": args.report_version}
        mappings = []
        for path in args.tables:
            table = json.loads(path.read_text())
            table.update(id=None, filing_id=None, source_document_id=None)
            mappings.append(map_table_section(table, report, sections, source, accession=args.accession))
        result = {"status": "offline_key_only", "section_mappings": mappings, "trace_examples": [],
                  "database_UUIDs": "not_read", "selection": None, "html_sha256": hashlib.sha256(source).hexdigest()}
        write_outputs(args.output, result)
        return 0 if all(m["mapping_status"] == "mapped" for m in mappings) else 1
    identity = None
    if args.mode == "shared":
        if bool(args.processing_set) != bool(args.contract):
            parser.error("Supply both --processing-set and --contract, or neither to discover a real legacy preference.")
        options = shared_options(args)
        secret = options.get("password") or os.environ.get("PGPASSWORD")
        try:
            with psycopg.connect(**options, autocommit=True, row_factory=dict_row) as conn:
                with conn.transaction():
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    identity = conn.execute("SELECT current_user,current_database(),current_setting('transaction_read_only') AS read_only").fetchone()
                    ssl = conn.execute("SELECT ssl,version,cipher FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone()
                    if identity["current_user"] != "sec_jazzy" or not ssl or ssl["ssl"] is not True:
                        raise DatabaseError("Unexpected database identity or non-SSL session.")
                    identity["ssl"] = ssl
                    identity["permissions"] = conn.execute("""SELECT c.relname,has_table_privilege(c.oid,'SELECT') AS can_select
                        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                        WHERE n.nspname='sec' AND c.relkind IN ('r','v') ORDER BY c.relname""").fetchall()
                    identity["schema"] = conn.execute("SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='sec' ORDER BY table_name,ordinal_position").fetchall()
                    snapshot = read_snapshot(conn, args.accession, selection={"contract": args.contract, "id": args.processing_set} if args.processing_set else None)
        except (psycopg.Error, DatabaseError) as error:
            message = str(error).replace(secret, "[redacted]") if secret else str(error)
            args.output.mkdir(parents=True, exist_ok=True)
            save_json(args.output / "connection-status.json", {"status": "unverified", "snapshot_completed": False,
                "identity": identity, "error_type": type(error).__name__, "message": message})
            print("Shared audit not completed; see connection-status.json. No database writes were attempted.", file=sys.stderr)
            return 2
    else:
        snapshot = json.loads(args.snapshot.read_text())
    sources = source_bytes(snapshot, args.source_root)
    result = audit_snapshot(snapshot, sources)
    result["evidence_origin"] = "live_shared_readonly" if args.mode == "shared" else "saved_snapshot_replay_not_live_shared"
    if identity is not None:
        result["connection"] = identity
    chunks = _chunk_output(args, snapshot, sources)
    write_outputs(args.output, result, snapshot=snapshot, chunk_mappings=chunks)
    print(json.dumps({key: result[key] for key in ("status", "selection", "tables")}, ensure_ascii=False))
    return 0 if result["status"] == "passed" and not any(row["mapping_status"] == "rejected" for row in chunks or []) else 1


if __name__ == "__main__":
    raise SystemExit(main())
