"""Offline continuation QA on an exported report; never connects to a database."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from uuid import UUID

from sec_pipeline.chunking import chunk_report
from sec_pipeline.table_context import build_table_contexts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source = json.loads(args.input.expanduser().read_text())
    text, sections = source["full_text"], source["sections"]
    options = dict(filing_id=UUID(source["filing_id"]), report_id=UUID(source["report_id"]),
                   full_text=text, sections=sections, max_chars=1500, overlap=150)
    baseline = chunk_report(**options, chunking_version="paragraph-v1")
    stored = source["chunks"]
    baseline_matches = len(baseline) == len(stored) and all(
        all(getattr(c, key) == old[key] for key in ("text", "chunk_index", "offset_start", "offset_end"))
        for c, old in zip(baseline, stored))
    if not baseline_matches:
        raise ValueError("Current v1 implementation does not reproduce the provided export; scope needs review")
    chunks = chunk_report(**options, chunking_version="paragraph-v2")
    before = [asdict(c) for c in chunks]
    contexts = build_table_contexts(full_text=text, sections=sections, chunks=chunks)
    assert [asdict(c) for c in chunks] == before, "Sidecar mutated original chunks"
    by_key = {s["section_id"]: s for s in sections}
    covered = bytearray(len(text))
    invalid, mismatch, outside, empty, oversized = [], [], [], [], []
    for c in chunks:
        a, b = c.offset_start, c.offset_end
        if not (0 <= a < b <= len(text)):
            invalid.append(c.chunk_index)
            continue
        covered[a:b] = b"\1" * (b - a)
        if c.text != text[a:b]: mismatch.append(c.chunk_index)
        if not c.text.strip(): empty.append(c.chunk_index)
        if len(c.text) > 1500: oversized.append(c.chunk_index)
        section = by_key[c.section]
        if not (section["offset_start"] <= a < b <= section["offset_end"]): outside.append(c.chunk_index)
    segments = [s for record in contexts.values() for t in record["table_contexts"] for s in t["segments"]]
    context_slice_errors = sum(s["text"] != text[s["offset_start"]:s["offset_end"]] for s in segments)
    identity = [(c.report_id, c.chunking_version, c.chunk_index) for c in chunks]
    target = contexts[61]
    product = [t for t in target["table_contexts"] if any(
        "Cost of Product Revenues (in millions, except percentages):" in s["text"] for s in t["segments"])]
    target_text = "\n".join(s["text"] for t in product for s in t["segments"])
    required = ["Cost of Product Revenues (in millions, except percentages):",
                "Three Months Ended", "July 31, 2026", "July 25, 2025", "% Change",
                "Cost of product revenues | | $ | 450 | | | $ | 302 | | | | 49 | % |"]
    missing_context = [value for value in required if value not in target_text]
    service_context = "\n".join(s["text"] for t in contexts[62]["table_contexts"]
                               for s in t["segments"])
    missing_service_context = [value for value in (
        "Cost of Services Revenues (in millions, except percentages):",
        "July 31, 2026", "July 25, 2025",
        "Cost of services revenues | | $ | 156 | | | $ | 159") if value not in service_context]
    checks = {
        "baseline_v1_exact_match": baseline_matches,
        "v2_chunk_count": len(chunks), "invalid_offsets": invalid,
        "slice_mismatches": mismatch, "outside_section": outside,
        "empty_chunks": empty, "oversized_chunks": oversized,
        "uncovered_nonwhitespace": sum(not flag and not ch.isspace() for ch, flag in zip(text, covered)),
        "index_sequence_ok": [c.chunk_index for c in chunks] == list(range(len(chunks))),
        "duplicate_identity_tuples": len(identity) - len(set(identity)),
        "rerun_deterministic": chunks == chunk_report(**options, chunking_version="paragraph-v2"),
        "core_chunk_unchanged_by_context": [asdict(c) for c in chunks] == before,
        "context_slice_errors": context_slice_errors,
        "chunk61_product_table_candidates": len(product),
        "chunk61_missing_required_context": missing_context,
        "chunk62_missing_required_context": missing_service_context,
        "chunks_with_table_context": sum(bool(r["table_contexts"]) for r in contexts.values()),
        "context_warnings_by_chunk": {i:r["warnings"] for i,r in contexts.items() if r["warnings"]},
        "database_uuid_status": "NOT_GENERATED: ChunkInput has no UUID; actual IDs require storage output",
    }
    structural_pass = not any((invalid, mismatch, outside, empty, oversized,
        checks["uncovered_nonwhitespace"], not checks["index_sequence_ok"],
        checks["duplicate_identity_tuples"], not checks["rerun_deterministic"],
        context_slice_errors, len(product) != 1, missing_context, missing_service_context))
    evidence = {
        "scope": "Offline exported report-v1 replay with uploaded chunker; inferred cleaned-text table context only",
        "report_id": source["report_id"], "filing_id": source["filing_id"],
        "cleaned_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "passed_structural_and_chunk61_context_checks": structural_pass,
        "release_acceptance": False, "checks": checks,
        "chunk61": {"text": chunks[61].text, "offset_start": chunks[61].offset_start,
                    "offset_end": chunks[61].offset_end, "context": target},
    }
    output = args.output_dir.expanduser()
    output.mkdir(parents=True, exist_ok=True)
    (output/"ntap_continuation_evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+"\n")
    with (output/"ntap_v2_context_preview.jsonl").open("w") as stream:
        for chunk in chunks:
            row = asdict(chunk)
            row.update(chunk_id=None, review_only=True, table_context=contexts[chunk.chunk_index])
            stream.write(json.dumps(row, ensure_ascii=False, default=str)+"\n")
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print("Structural + chunk61 continuation checks:", "PASSED" if structural_pass else "FAILED")
    print("Review-only output:", output.resolve())
    return 0 if structural_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
