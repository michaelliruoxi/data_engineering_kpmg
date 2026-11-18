"""Split a backfill queue's unattempted filings into per-worker shard queues.

Each shard is an ordinary queue file with its own writer lock, so one importer
process per shard can run at the same time. Every filing of an issuer stays in
one shard: two workers never write the same company's rows. Merging copies the
shard entries back into the main queue; nothing here touches a database.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

UNATTEMPTED = ("pending", "downloaded")
SHARD_DIRECTORY = "shards"
# Annual reports took about three times as long as quarterly ones to process.
FORM_WEIGHT = {"10-K": 3}


def shard_paths(queue_path):
    """Shard queue files belonging to a main queue, in worker order."""
    return sorted((Path(queue_path).parent / SHARD_DIRECTORY).glob("queue-*.json"))


def split_state(state, workers, *, source_name="queue.json"):
    """Return one queue state per worker covering every unattempted filing once."""
    if type(workers) is not int or not 2 <= workers <= 8:
        raise ValueError("Use between two and eight workers.")
    if "shard" in state:
        raise ValueError("A shard queue cannot be split again.")
    issuers = {}
    for accession, entry in state["filings"].items():
        if entry.get("status") in UNATTEMPTED:
            issuer = issuers.setdefault(entry["filing"]["cik"], {"weight": 0, "filings": {}})
            issuer["filings"][accession] = entry
            issuer["weight"] += FORM_WEIGHT.get(entry["filing"].get("form"), 1)
    if len(issuers) < workers:
        raise ValueError("There are fewer issuers with unattempted filings than workers.")
    base = {key: deepcopy(value) for key, value in state.items() if key not in {"filings", "last_run"}}
    created = datetime.now(timezone.utc).isoformat()
    shards = [{**deepcopy(base), "shard": {"index": index + 1, "of": workers, "source": source_name,
                                           "created_at_utc": created}, "filings": {}}
              for index in range(workers)]
    loads = [0] * workers
    # Heaviest issuer first into the lightest shard; ties resolve by CIK and index.
    for cik, issuer in sorted(issuers.items(), key=lambda item: (-item[1]["weight"], item[0])):
        target = loads.index(min(loads))
        shards[target]["filings"].update(deepcopy(issuer["filings"]))
        loads[target] += issuer["weight"]
    return shards


def merge_states(state, shards):
    """Copy every shard entry back into the main queue state; return the status changes."""
    seen, changes = set(), {}
    for shard in shards:
        if shard.get("schema_version") != state.get("schema_version") or shard.get("configuration") != state.get("configuration"):
            raise ValueError("A shard was created for a different queue configuration.")
        if "shard" not in shard:
            raise ValueError("Only shard queues can be merged.")
        for accession, entry in shard["filings"].items():
            if accession not in state["filings"] or accession in seen:
                raise ValueError("Shard filings must each belong to the main queue exactly once.")
            if entry["filing"] != state["filings"][accession]["filing"]:
                raise ValueError("A shard filing no longer matches its main queue record.")
            seen.add(accession)
    for shard in shards:
        for accession, entry in shard["filings"].items():
            before = state["filings"][accession].get("status")
            if entry.get("status") != before:
                changes[(before, entry.get("status"))] = changes.get((before, entry.get("status")), 0) + 1
            state["filings"][accession] = deepcopy(entry)
    return changes
