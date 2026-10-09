"""Verify, dry-run, or explicitly import the approved local SEC sample."""

import argparse
from contextlib import contextmanager, redirect_stdout
import importlib
import ipaddress
import io
import json
import os
from pathlib import Path
import re
import sys

from lxml import etree

from download_sec_sample import SampleError, load_manifest, verify_sample


ROOT = Path(__file__).resolve().parents[1]


def verified_inputs(manifest_path, data_root):
    """Reuse the existing approved-sample loader and full local verification."""
    manifest = load_manifest(manifest_path)
    records = [manifest["index_document"], *manifest["files"]]
    if manifest.get("status") != "verified":
        raise SampleError("The source manifest must have verified status.")
    for record in records:
        if record.get("status") != "verified":
            raise SampleError(f"Source is not marked verified: {record['local_path']}")
    verify_sample(data_root, manifest)
    return manifest


def verify_quietly(manifest_path, data_root):
    from sec_pipeline.processing import ProcessingError

    try:
        with redirect_stdout(io.StringIO()):
            return verified_inputs(manifest_path, data_root)
    except (SampleError, OSError, ValueError, KeyError, TypeError, etree.Error) as exc:
        raise ProcessingError(f"Input verification failed: {exc}") from exc


def load_plan(module_name, mode):
    from sec_pipeline.processing import DryRunPlan, IngestionPlan, ProcessingError

    factory_name = "get_ingestion_plan" if mode == "ingest" else "get_dry_run_plan"
    plan_type = IngestionPlan if mode == "ingest" else DryRunPlan
    try:
        with redirect_stdout(io.StringIO()):
            module = importlib.import_module(module_name)
            factory = getattr(module, factory_name, None)
            if not callable(factory):
                raise ProcessingError(f"The adapter module must expose {factory_name}().")
            plan = factory()
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError(f"The configured {mode} adapter module is unavailable or failed to load.") from exc
    if not isinstance(plan, plan_type):
        raise ProcessingError(f"The adapter factory must return {plan_type.__name__}.")
    return plan


def read_reference(path):
    from sec_pipeline.processing import ProcessingError

    if path is None:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProcessingError("Cannot read the JSON reference file.") from exc


@contextmanager
def explicit_database_environment():
    """Use the selected file, without ambient PostgreSQL/libpq redirection."""
    saved = {key: value for key, value in os.environ.items() if key.startswith(("PG", "POSTGRES_"))}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        for key in list(os.environ):
            if key.startswith(("PG", "POSTGRES_")):
                del os.environ[key]
        os.environ.update(saved)


def ingestion_settings(env_file, database):
    from sec_pipeline.database import DatabaseError, connection_settings
    from sec_pipeline.processing import ProcessingError

    if not env_file.is_file():
        raise ProcessingError("The selected environment file does not exist.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", database):
        raise ProcessingError("--database must be a database name, not a URL or connection string.")
    try:
        settings = connection_settings(env_file)
    except (DatabaseError, OSError, UnicodeError) as exc:
        raise ProcessingError("Cannot read database settings; check the selected environment file.") from exc
    host = settings["host"]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
            raise ProcessingError("PGHOST must select one TCP host or IP address.")
    settings.update(dbname=database, application_name="sec-filing-ingestion")
    if address is not None:
        settings["hostaddr"] = str(address)
    elif host.lower() == "localhost":
        settings["hostaddr"] = "127.0.0.1"
    return settings


def check_connection_target(conn, settings):
    from sec_pipeline.processing import ProcessingError

    if (
        conn.info.dbname != settings["dbname"]
        or conn.info.host != settings["host"]
        or conn.info.port != settings["port"]
        or ("hostaddr" in settings and conn.info.hostaddr != settings["hostaddr"])
    ):
        raise ProcessingError("The connection does not match the explicitly selected database target.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Task 2: verify inputs, dry-run pure parsers, or import with explicit adapters."
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--verify-inputs",
        action="store_true",
        help="Verify inputs only; extraction and database loading are not run.",
    )
    modes.add_argument(
        "--dry-run", action="store_true",
        help="Verify, parse, chunk, and validate without a database connection.",
    )
    modes.add_argument(
        "--ingest", action="store_true",
        help="Import with configured write adapters and validation in one transaction.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "source_manifest.json",
        help="Path to the approved source manifest.",
    )
    parser.add_argument("--data-root", type=Path, default=ROOT, help="Root containing manifest source paths.")
    parser.add_argument("--adapter-module", help="Module exposing get_dry_run_plan() and/or get_ingestion_plan().")
    for stage in ("facts", "tables", "reports", "chunks"):
        parser.add_argument(f"--{stage}-version", help=f"Explicit {stage} processing version.")
    parser.add_argument("--reference-file", type=Path, help="Optional JSON reference checks and expected_counts.")
    parser.add_argument("--env-file", type=Path, help="Explicit database environment file, required for --ingest.")
    parser.add_argument("--database", help="Explicit destination database name, required for --ingest.")
    args = parser.parse_args(argv)

    if args.dry_run or args.ingest:
        versions = {
            name: getattr(args, f"{name}_version") for name in ("facts", "tables", "reports", "chunks")
        }
        mode = "ingest" if args.ingest else "dry-run"
        if not args.adapter_module or any(value is None or not value.strip() for value in versions.values()):
            parser.error(f"--{mode} requires --adapter-module and all four --*-version options.")
        if args.ingest and (args.env_file is None or not args.database):
            parser.error("--ingest requires an explicit --env-file and --database.")

        # Help and verify-only do not load processing, adapters, or database configuration.
        sys.path.insert(0, str(ROOT / "src"))
        from sec_pipeline.processing import ProcessingError, check_ingestion_options, run_dry_run, run_ingestion

        try:
            # Verify before importing an adapter module, so bad sources stop early.
            verify_quietly(args.manifest, args.data_root)
            plan = load_plan(args.adapter_module, mode)
            reference = read_reference(args.reference_file)
            if args.dry_run:
                with redirect_stdout(io.StringIO()):
                    summary = run_dry_run(
                        manifest_path=args.manifest, data_root=args.data_root, versions=versions,
                        stages=plan.stages, required_checks=plan.required_checks,
                        verify_inputs=verify_quietly, reference=reference,
                    )
            else:
                check_ingestion_options(plan, versions, reference)
                from sec_pipeline.database import connect

                with explicit_database_environment():
                    settings = ingestion_settings(args.env_file, args.database)
                    try:
                        conn = connect(args.env_file, **settings)
                    except Exception as exc:
                        raise ProcessingError(
                            "Cannot connect to the selected database; check the environment file, "
                            "host, credentials, and database access."
                        ) from exc
                    try:
                        check_connection_target(conn, settings)
                        with redirect_stdout(io.StringIO()):
                            summary = run_ingestion(
                                conn, manifest_path=args.manifest, data_root=args.data_root,
                                versions=versions, plan=plan, verify_inputs=verify_quietly,
                                reference=reference,
                            )
                    finally:
                        conn.close()
                summary["destination"] = {
                    "host": settings["host"], "port": settings["port"], "database": settings["dbname"],
                }
        except ProcessingError as exc:
            print(f"{'Ingestion' if args.ingest else 'Dry-run'} stopped: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, indent=2))
        return 0

    try:
        manifest = verified_inputs(args.manifest, args.data_root)
        records = [manifest["index_document"], *manifest["files"]]
    except (SampleError, OSError, ValueError, KeyError, TypeError, etree.Error) as exc:
        print(f"Input verification failed: {exc}", file=sys.stderr)
        return 1

    print(f"Filing: {manifest['accession_number']}")
    print(f"Input verification passed: {len(records)} source documents.")
    print("Completed stage: input verification only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
