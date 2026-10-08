# Chunking and citations

`sec_pipeline.chunking.chunk_report()` takes the finalized report text and its
ordered section ranges, then returns `ChunkInput` records. It does not clean or
normalize report text.

## Boundary policy

- Section offsets are zero-based, half-open Python character positions into
  `full_text`.
- Each chunk is an exact slice:
  `chunk.text == full_text[chunk.offset_start:chunk.offset_end]`.
- A chunk stays within one section. Section ranges must be ordered, nonoverlapping,
  and cover every non-whitespace character in the report.
- The chunker prefers paragraph breaks, then whitespace, when splitting before
  `max_chars`. If neither is available, it splits at the character limit.
- Defaults are `max_chars=1500` and `overlap=150`; both can be configured.
- Chunk indexes start at zero and increase across the report. Section ID, title,
  and source locator are kept in chunk metadata.
- Offsets refer to the final cleaned report. Do not normalize the text after
  generating chunks.

## Storage

Store the report first so it has a `report_id`. Then call `chunk_report()` with
that report ID and store the returned records with `store_chunks()` in the same
database transaction. The database citation view can then resolve each chunk
through its report and source document.