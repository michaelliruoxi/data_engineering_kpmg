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
  `max_chars`. The cleaner retains paragraph boundaries as `\n\n`, while explicit
  line breaks inside a paragraph or table cell remain `\n`. If no usable boundary
  advances past the preceding chunk, the chunker splits at the character limit.
- Defaults are `max_chars=1500` and `overlap=150`; both can be configured.
- Chunk indexes start at zero and increase across the report. Section ID, title,
  and source locator are kept in chunk metadata.
- Offsets refer to the final cleaned report. Do not normalize the text after
  generating chunks.

## Source citations and processing versions

The cleaner records each section's `source_locator` as a JSON object containing
`source_file` and an absolute `xpath`, plus `element_id` when the original element
has an ID. The XPath identifies an original HTML block or table row and is captured
before text cleanup. Parse the unchanged HTML with lxml's HTML parser to resolve
it. Hidden siblings must still be present when resolving the original XPath.
Chunk metadata preserves this object unchanged.

The cleaner omits document metadata, scripts, styles, comments, hidden XBRL,
HTML `hidden` elements, and subtrees hidden by inline `display` or `visibility`
declarations. It does not evaluate stylesheet selectors or browser layout.

There are two distinct locations in a chunk citation: the chunk's character range
locates its exact text in the finalized report; the section locator identifies
the original HTML section heading or fallback content block. The section locator
does not identify every original character in a chunk, and cleaned-report offsets
are not HTML offsets. Use the report's source document path, URL, and checksum to
identify the original filing, then inspect the relevant section there.

The paragraph and visibility cleanup changes alter report text, section offsets,
and chunk boundaries. Store that text under a new report extraction version;
do not reuse a version already associated with different text. `reports-v2` is
an example for the integration plan, not a frozen or deployed release label.
If changing chunking settings or policy for an existing report, select a new
chunking version as well. The default `paragraph-v1` names the current chunking
algorithm; it does not establish full-filing acceptance or shared deployment.

## Storage

Store the report first so it has a `report_id`. Then call `chunk_report()` with
that report ID and store the returned records with `store_chunks()` in the same
database transaction. The database citation view can then resolve each chunk
through its report and source document.

## Offline verification

Run `uv run --locked python -m pytest tests/test_report_cleaning.py tests/test_chunking.py`.
This includes the cleaner's pytest tests and the chunker's unittest tests. The
integration checks use both a small HTML fixture and the supplied Microsoft
filing to verify original-source locator resolution, paragraph boundaries,
determinism, exact slices, section containment, size limits, and complete
non-whitespace coverage. These checks do not write to a database or contact SEC;
they do not replace independent content review and stored-citation acceptance.
