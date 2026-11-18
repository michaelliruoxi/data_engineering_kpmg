"""Split a backfill queue into per-worker shard queues, or merge them back.

split: writes shards/queue-NN.json beside the queue, one per worker, holding the
filings not attempted yet. merge: copies shard results into the main queue and
moves the shard files to shards/merged-<timestamp>/. Neither contacts the SEC
or a database. Both refuse to run while any involved queue is locked.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from sec_pipeline.backfill import queue_lock, save_state
from sec_pipeline.queue_shards import SHARD_DIRECTORY, merge_states, shard_paths, split_state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("split", "merge"))
    parser.add_argument("--state", type=Path, required=True, help="The main queue file.")
    parser.add_argument("--workers", type=int, default=4, help="Number of shard queues to write (split only).")
    args = parser.parse_args(argv)
    try:
        if not args.state.is_file():
            raise ValueError("The main queue does not exist.")
        existing = shard_paths(args.state)
        if any(Path(str(path) + ".lock").exists() for path in existing):
            raise ValueError("A shard queue is locked. Wait for its worker to stop first.")
        with queue_lock(args.state):
            state = json.loads(args.state.read_text(encoding="utf-8"))
            if args.action == "split":
                if existing:
                    raise ValueError("Shard queues already exist. Resume them or merge them first.")
                shards = split_state(state, args.workers, source_name=args.state.name)
                directory = args.state.parent / SHARD_DIRECTORY
                for shard in shards:
                    save_state(directory / f"queue-{shard['shard']['index']:02d}.json", shard)
                print(json.dumps({"event": "split", "workers": len(shards),
                                  "filings": [len(shard["filings"]) for shard in shards]}))
            else:
                if not existing:
                    raise ValueError("There are no shard queues to merge.")
                shards = [json.loads(path.read_text(encoding="utf-8")) for path in existing]
                changes = merge_states(state, shards)
                save_state(args.state, state)
                archive = args.state.parent / SHARD_DIRECTORY / ("merged-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
                archive.mkdir(parents=True)
                for path in existing:
                    path.replace(archive / path.name)
                print(json.dumps({"event": "merged", "shards": len(shards),
                                  "status_changes": {f"{before} -> {after}": count for (before, after), count in sorted(changes.items())},
                                  "filings": dict(Counter(entry["status"] for entry in state["filings"].values()))}))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
