"""Verify local SEC source files before the ingestion stages."""

import argparse
from pathlib import Path
import sys

from lxml import etree

from download_sec_sample import SampleError, load_manifest, verify_sample


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(
        description="Task 2: verify the approved local filing inputs."
    )
    parser.add_argument(
        "--verify-inputs",
        action="store_true",
        required=True,
        help="Verify inputs only; extraction and database loading are not run.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "source_manifest.json",
        help="Path to the approved source manifest.",
    )
    args = parser.parse_args()

    try:
        manifest = load_manifest(args.manifest)
        records = [manifest["index_document"], *manifest["files"]]
        if manifest.get("status") != "verified":
            raise SampleError("The source manifest must have verified status.")
        for record in records:
            if record.get("status") != "verified":
                raise SampleError(
                    f"Source is not marked verified: {record['local_path']}"
                )
        verify_sample(ROOT, manifest)
    except (SampleError, OSError, ValueError, KeyError, TypeError, etree.Error) as exc:
        print(f"Input verification failed: {exc}", file=sys.stderr)
        return 1

    print(f"Filing: {manifest['accession_number']}")
    print(f"Input verification passed: {len(records)} source documents.")
    print("Completed stage: input verification only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())