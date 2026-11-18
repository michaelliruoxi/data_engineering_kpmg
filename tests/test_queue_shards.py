"""Shard queues cover unattempted work once, keep issuers together and merge back."""
from copy import deepcopy
import importlib
import json
from pathlib import Path

import pytest

from sec_pipeline import backfill
from sec_pipeline.queue_shards import merge_states, shard_paths, split_state
from tests.test_backfill import cli_queue, queue  # noqa: F401


def state(issuers=6, per_issuer=4):
    filings = {}
    for issuer in range(issuers):
        cik = f"{1000 + issuer:010d}"
        for number in range(per_issuer + issuer):
            accession = f"{cik}-26-{number:06d}"
            filings[accession] = {"filing": {"cik": cik, "accession_number": accession,
                                             "form": "10-K" if number == 0 else "10-Q",
                                             "filing_date": f"2026-0{number % 9 + 1}-01"},
                                  "status": "pending", "attempts": 0}
    return {"schema_version": 1, "configuration": {"since": "2026-01-01"}, "companies": {"x": {}},
            "last_run": {"mode": "ingest"}, "filings": filings}


def test_split_covers_unattempted_filings_once_and_keeps_issuers_together():
    main = state()
    done, failed = list(main["filings"])[:2]
    main["filings"][done]["status"] = "ingested"
    main["filings"][failed]["status"] = "failed"
    before = deepcopy(main)
    shards = split_state(main, 3)
    assert main == before
    accessions = [accession for shard in shards for accession in shard["filings"]]
    assert sorted(accessions) == sorted(set(main["filings"]) - {done, failed})
    owners = {}
    for shard in shards:
        assert shard["configuration"] == main["configuration"] and shard["companies"] == main["companies"]
        assert "last_run" not in shard and shard["shard"]["of"] == 3
        for entry in shard["filings"].values():
            assert owners.setdefault(entry["filing"]["cik"], shard["shard"]["index"]) == shard["shard"]["index"]
    sizes = [len(shard["filings"]) for shard in shards]
    assert min(sizes) > 0 and max(sizes) - min(sizes) <= 8
    assert split_state(main, 3) is not shards and [s["filings"] for s in split_state(main, 3)] == [s["filings"] for s in shards]


@pytest.mark.parametrize("workers", [1, 9, 7, "4"])
def test_split_rejects_unusable_worker_counts(workers):
    with pytest.raises(ValueError):
        split_state(state(), workers)


def test_shards_cannot_be_split_again():
    with pytest.raises(ValueError, match="cannot be split"):
        split_state(split_state(state(), 2)[0], 2)


def test_merge_copies_shard_results_and_leaves_other_entries():
    main = state()
    kept = next(iter(main["filings"]))
    main["filings"][kept]["status"] = "ingested"
    shards = split_state(main, 2)
    loaded = next(iter(shards[0]["filings"]))
    shards[0]["filings"][loaded].update(status="ingested", attempts=1, result={"mode": "ingest"})
    kept_before = deepcopy(main["filings"][kept])
    assert merge_states(main, shards) == {("pending", "ingested"): 1}
    assert main["filings"][loaded]["result"] == {"mode": "ingest"}
    assert main["filings"][kept] == kept_before
    assert sum(entry["status"] == "pending" for entry in main["filings"].values()) == len(main["filings"]) - 2


def test_merge_rejects_foreign_duplicate_or_changed_shards():
    main = state()
    shards = split_state(main, 2)
    other = deepcopy(shards)
    other[0]["configuration"] = {"since": "2025-01-01"}
    with pytest.raises(ValueError, match="different queue configuration"):
        merge_states(deepcopy(main), other)
    with pytest.raises(ValueError, match="exactly once"):
        merge_states(deepcopy(main), [shards[0], shards[0]])
    changed = deepcopy(shards)
    next(iter(changed[1]["filings"].values()))["filing"]["form"] = "8-K"
    with pytest.raises(ValueError, match="no longer matches"):
        merge_states(deepcopy(main), changed)
    with pytest.raises(ValueError, match="Only shard queues"):
        merge_states(deepcopy(main), [main])


def test_cli_split_then_merge_round_trip_and_locks(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    cli = importlib.import_module("shard_backfill_queue")
    path = tmp_path / "queue.json"
    backfill.save_state(path, state())
    assert cli.main(["split", "--state", str(path), "--workers", "2"]) == 0
    paths = shard_paths(path)
    assert [item.name for item in paths] == ["queue-01.json", "queue-02.json"]
    assert cli.main(["split", "--state", str(path), "--workers", "2"]) == 1
    assert "already exist" in capsys.readouterr().err
    shard = json.loads(paths[0].read_text(encoding="utf-8"))
    loaded = next(iter(shard["filings"]))
    shard["filings"][loaded]["status"] = "ingested"
    backfill.save_state(paths[0], shard)
    lock = Path(str(paths[1]) + ".lock")
    lock.write_text("1", encoding="utf-8")
    assert cli.main(["merge", "--state", str(path)]) == 1
    assert "locked" in capsys.readouterr().err
    lock.unlink()
    assert cli.main(["merge", "--state", str(path)]) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["filings"][loaded]["status"] == "ingested"
    assert shard_paths(path) == [] and len(list((tmp_path / "shards").glob("merged-*/queue-*.json"))) == 2
    assert not Path(str(path) + ".lock").exists()


def test_main_queue_cannot_be_imported_while_split(cli_queue, capsys):  # noqa: F811
    shard = cli_queue.queue.path.parent / "shards" / "queue-01.json"
    shard.parent.mkdir()
    shard.write_text("{}", encoding="utf-8")
    assert cli_queue.cli.main(["--dry-run", "--offline", *cli_queue.arguments]) == 1
    assert "ValueError" in capsys.readouterr().err
    assert cli_queue.cli.main(["--status", *cli_queue.arguments]) == 0


def test_parallel_launcher_starts_pending_only_workers_per_shard():
    text = (Path(__file__).parents[1] / "scripts/start_parallel_backfill.ps1").read_text(encoding="utf-8")
    assert "[ValidateRange(2, 8)][int]$Workers = 4" in text
    assert "'scripts\\shard_backfill_queue.py' 'split'" in text
    assert "'--ingest', '--pending-only', '--tables-version', 'tables-v3'" in text
    assert "A process is still importing the main queue." in text
