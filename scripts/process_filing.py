"""Verify the approved local SEC sample or parse it without database writes."""

import argparse
from contextlib import redirect_stdout
import importlib
import io
import json
from pathlib import Path
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Task 2: verify inputs or run the configured pure parsers."
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
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "source_manifest.json",
        help="Path to the approved source manifest.",
    )
    parser.add_argument("--data-root", type=Path, default=ROOT, help="Root containing manifest source paths.")
    parser.add_argument("--adapter-module", help="Pure adapter module exposing get_dry_run_plan().")
    for stage in ("facts", "tables", "reports", "chunks"):
        parser.add_argument(f"--{stage}-version", help=f"Explicit {stage} processing version.")
    parser.add_argument("--reference-file", type=Path, help="Optional JSON reference checks and expected_counts.")
    args = parser.parse_args(argv)

    if args.dry_run:
        versions = {
            name: getattr(args, f"{name}_version") for name in ("facts", "tables", "reports", "chunks")
        }
        if not args.adapter_module or any(value is None for value in versions.values()):
            parser.error("--dry-run requires --adapter-module and all four --*-version options.")

        # Import processing and configured adapters only when dry-run is requested.
        sys.path.insert(0, str(ROOT / "src"))
        from sec_pipeline.processing import DryRunPlan, ProcessingError, run_dry_run

        def verify_quietly(manifest_path, data_root):
            try:
                with redirect_stdout(io.StringIO()):
                    return verified_inputs(manifest_path, data_root)
            except (SampleError, OSError, ValueError, KeyError, TypeError, etree.Error) as exc:
                raise ProcessingError(f"Input verification failed: {exc}") from exc

        try:
            # Verify before importing an adapter module, so bad sources stop early.
            verify_quietly(args.manifest, args.data_root)
            try:
                module = importlib.import_module(args.adapter_module)
                factory = getattr(module, "get_dry_run_plan", None)
                if not callable(factory):
                    raise ProcessingError("The adapter module must expose get_dry_run_plan().")
                plan = factory()
            except ProcessingError:
                raise
            except Exception as exc:
                raise ProcessingError("The configured dry-run adapter module is unavailable or failed to load.") from exc
            if not isinstance(plan, DryRunPlan):
                raise ProcessingError("The adapter factory must return DryRunPlan.")
            reference = {}
            if args.reference_file is not None:
                try:
                    reference = json.loads(args.reference_file.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ProcessingError("Cannot read the JSON reference file.") from exc
            summary = run_dry_run(
                manifest_path=args.manifest, data_root=args.data_root, versions=versions,
                stages=plan.stages, required_checks=plan.required_checks,
                verify_inputs=verify_quietly, reference=reference,
            )
        except ProcessingError as exc:
            print(f"Dry-run stopped: {exc}", file=sys.stderr)
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
