"""Check one receiving set against an independent downstream export."""
import argparse
import json
from pathlib import Path

from sec_pipeline.fiscal_table_qa import verify_fiscal_outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--downstream", type=Path)
    parser.add_argument("--verified-dei", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    def load(path):
        return json.loads(path.read_text()) if path is not None else None

    result = verify_fiscal_outputs(load(args.snapshot), load(args.downstream), load(args.verified_dei))
    with args.output.open("x") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    print(result["consistency"], result["correctness"])
    if result["consistency"] == "failed" or result["correctness"] == "disagrees_with_verified_dei":
        return 1
    return 0 if (result["consistency"], result["correctness"]) == ("passed", "matched_verified_dei") else 2


if __name__ == "__main__":
    raise SystemExit(main())
