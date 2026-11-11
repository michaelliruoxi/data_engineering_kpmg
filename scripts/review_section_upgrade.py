"""Inspect, validate, publish or roll back one corrected filing selection."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from sec_pipeline.corrected_acceptance import audit_corrected_set
from sec_pipeline.database import connect
from sec_pipeline.publication import publish_corrected_set, rollback_corrected_set, served_selection
from process_filing import check_connection_target, explicit_database_environment, ingestion_settings


def _write_evidence(output, result):
    output.seek(0)
    output.write(json.dumps(result, indent=2, default=str) + "\n")
    output.truncate()
    output.flush()
    os.fsync(output.fileno())


def _change_selection(conn, output, result, change):
    prepared = False
    try:
        with conn.transaction():
            result["served_selection_after"] = change()
            result["transaction_status"] = "prepared"
            _write_evidence(output, result)
            prepared = True
    except Exception as error:
        if prepared:
            # A lost commit acknowledgement cannot establish either commit or
            # rollback. Retain the attempted selection without certifying it.
            result["attempted_served_selection"] = result.pop("served_selection_after")
            result.update(status="outcome_unknown", transaction_status="unknown",
                          error_type=type(error).__name__)
            try:
                _write_evidence(output, result)
            except OSError as evidence_error:
                print(json.dumps({"status": "outcome_unknown", "transaction_status": "unknown",
                                  "error_type": type(error).__name__,
                                  "evidence_error_type": type(evidence_error).__name__}))
        raise
    result["transaction_status"] = "committed"


def _review(args, output):
    with explicit_database_environment():
        settings = ingestion_settings(args.env_file, args.database)
        with connect(args.env_file, **settings) as conn:
            check_connection_target(conn, settings)
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                row = conn.execute("SELECT id FROM sec.filings WHERE accession_number=%s", (args.accession,)).fetchone()
                if row is None:
                    raise ValueError("Filing is absent from this receiving database.")
                filing_id = row[0]
                current = served_selection(conn, filing_id)
                if args.action == "inventory":
                    result = {"accession_number": args.accession, "filing_id": str(filing_id), "served_selection": current,
                        "database_size_bytes": conn.execute("SELECT pg_database_size(current_database())").fetchone()[0],
                        "active_other_sessions": conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND state<>'idle'").fetchone()[0],
                        "migration_ledger": dict(conn.execute("SELECT name,sha256 FROM sec.schema_migrations ORDER BY name").fetchall()),
                        "versions": [r[0] for r in conn.execute("SELECT to_jsonb(v) FROM finance.filing_version v WHERE filing_id=%s ORDER BY created_at", (filing_id,))],
                        "source_receipts": [r[0] for r in conn.execute("SELECT jsonb_build_object('id',id,'url',source_url,'role',role,'sha256',sha256,'size_bytes',size_bytes,'local_path',local_path) FROM sec.source_documents WHERE filing_id=%s", (filing_id,))]}
                elif args.action in {"validate", "publish"}:
                    result = audit_corrected_set(conn, args.accession, args.processing_set_id, args.data_root)
                    result["served_selection_before"] = current
                else:
                    result = {"filing_id": str(filing_id), "served_selection_before": current}
            if args.action == "publish" and result["status"] == "passed":
                expected = (None if args.expected_family == "none" else args.expected_family, args.expected_set_id)
                _change_selection(conn, output, result,
                    lambda: publish_corrected_set(conn, filing_id, args.processing_set_id, expected_current=expected))
            elif args.action == "rollback":
                result["status"] = "rolled_back"
                _change_selection(conn, output, result,
                    lambda: rollback_corrected_set(conn, filing_id, args.processing_set_id))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inventory", "validate", "publish", "rollback"))
    parser.add_argument("--accession", required=True)
    parser.add_argument("--processing-set-id", type=UUID)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--data-root", type=Path, default=Path("."))
    parser.add_argument("--expected-family", choices=("legacy", "source", "none"))
    parser.add_argument("--expected-set-id", type=UUID)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action != "inventory" and args.processing_set_id is None:
        parser.error("This action requires --processing-set-id.")
    if args.action == "publish" and (args.expected_family is None or
            (args.expected_family == "none") != (args.expected_set_id is None)):
        parser.error("Publish requires the reviewed --expected-family and its --expected-set-id, or family none.")
    # Reserve a fresh evidence file before touching the database. Never truncate
    # an earlier acceptance record or a source file on a failed run.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output = args.output.open("x", encoding="utf-8")
    try:
        with output:
            result = _review(args, output)
            try:
                _write_evidence(output, result)
            except OSError as error:
                if result.get("transaction_status") == "committed":
                    print(json.dumps({"action": args.action, "status": "committed_evidence_incomplete",
                                      "error_type": type(error).__name__, "output": str(args.output)}))
                    return 3
                raise
    except Exception:
        # The exclusive open succeeded, so this is this run's reservation.
        # Close first for platforms that cannot unlink an open file.
        if args.output.stat().st_size == 0:
            args.output.unlink()
        raise
    print(json.dumps({"action": args.action, "status": result.get("status", "inventoried"), "output": str(args.output)}))
    return 2 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Never echo raw connection exceptions or private configuration.
        print(json.dumps({"status": "stopped", "error_type": type(error).__name__}))
        raise SystemExit(1)
