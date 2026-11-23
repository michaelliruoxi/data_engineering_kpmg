"""Compare real pre- and post-deployment table, cell and fact snapshots."""
import argparse
import json
from pathlib import Path

from sec_pipeline.fiscal_table_qa import compare_lineage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("before", "after", "before-capture", "after-capture"):
        parser.add_argument(f"--{name}", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    def load(path):
        return json.loads(path.read_text()) if path is not None else None

    result = compare_lineage(*(load(getattr(args, name)) for name in
                              ("before", "after", "before_capture", "after_capture")))
    with args.output.open("x") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    print(result["status"])
    return {"lineage_unchanged": 0, "lineage_changed": 1}.get(result["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
