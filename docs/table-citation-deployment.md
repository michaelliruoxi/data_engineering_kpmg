# Table citation integration: local verification and deployment gate

## Actual producer/consumer evidence

On 2026-10-29 current main was `c70ce554`. Available remote branch
`ruby/chunking-citations` at `b0a3b22999493092f552b4ba34c5b4dc6c451b4a`
documents ordinary report `ChunkInput` slices with half-open Python character
offsets and section association. It does **not** supply actual table-chunk
records or the financial table UUID contract. All inspected remote branch trees
have only the known Microsoft original archive, not the missing historical
originals. Therefore the table-specific producer contract remains draft.

Required from Ruby/Michael: actual records containing two chunks of one table,
a multi-table/continuation example, producer/version and stable chunk-ID field,
scalar/list table-ID semantics, original HTML hash/locator and receiving DB
filing/source/processing/document-set scopes. Existing paragraph chunk IDs and
cleaned offsets cannot substitute for those records. No new chunk identity or
shared table→chunk persistence schema is invented while these are unavailable.

## Implemented independent integration

`table_citations.integrate_table_citations` composes the existing relationship
and original-source audit, original-DOM section mapper, and draft producer
adapter. It returns one citation record per existing table, all associated
chunks (not a one-to-one assumption), section evidence, source-linked fact/cell
trace examples and explicit blockers. Ambiguous sections remain unresolved.
An unpassed audit, missing selection or rejected producer record blocks published
chunk associations. Exact repeated chunk payloads are idempotent; conflicting
payloads using the same chunk ID reject that identity. Inputs and DB rows are
never rewritten. Synthetic records require opt-in and remain incomplete.

```sh
uv run --locked python scripts/replay_table_citations.py \
  --snapshot /private/receiving-snapshot.json --source-root /private/archive \
  --chunks /private/actual-producer-records.json \
  --output /private/citation-replay.json
```

Use `--input-fields /private/producer-field-mapping.json` for documented field
aliases. Omit `--chunks` to expose missing producer evidence rather than invent
it. Exit 1 means blocked; exit 2 means incomplete or draft awaiting contract
confirmation. This candidate workflow intentionally has no final-approval exit.
Real public Microsoft local traces are demonstrated in
`evidence/msft-local-citation-example.json`; all DB UUIDs are explicitly local,
not shared RDS IDs. Section associations are real local DB mappings. Actual
producer chunk associations are absent; the separate synthetic replay is not
misrepresented as actual producer acceptance.

## Migration / reviewed deployment sequence (NOT performed on shared RDS)

1. Confirm final accessions and receiving processing/document-set IDs. Obtain
   the actual producer records above; finalize the contract with both owners.
2. Review `docs/proposals/table_section_mappings.sql`: additive existing
   `(table_id,report_id)` identity, composite parent/source checks, immutable
   evidence. It remains outside packaged migrations. Do not edit an already
   applied migration or assume this proposal is installed in shared RDS.
3. Review mapping scope/key semantics before promotion: the proposal rejects
   changed selection evidence for the same table/report pair. If reuse across
   selected sets is required, owners must approve a scope-aware key migration;
   do not overwrite existing evidence to make replay pass. Decide reader grants
   explicitly; do not broaden shared permissions by inference.
4. Once authorized, the deployment owner checks migration ledger/checksums,
   backups, current relation names and privileges. Promote reviewed SQL to the
   next unused migration number in a separate reviewed change; test against an
   empty disposable DB and an upgrade fixture. Shared application requires
   separate approval. No shared migration is executed by this review.
5. Load section mappings only with `store_table_section_mapping` under a
   caller-owned transaction. It rereads receiving identities, selected scope,
   hashes and original DOM; any conflict rolls back the transaction. Replay
   identical evidence, verify row counts and checks before commit. Do not
   install mappings from this cloud test's UUIDs in another database.
6. Prepare a table→chunk relation only after the producer identity/storage and
   owner-approved contract are known. Use existing receiving IDs and explicit
   scopes; test split/continuation/multiple chunks, conflicts and atomic rollback.
   Do not bind draft external chunk strings to `sec.chunks.id` by guessing.
7. Pre-commit rollback is the caller transaction rollback (locally tested).
   After shared commit, do not delete immutable mappings ad hoc: prepare a
   reviewed compensating migration/consumer rollback with dependency checks.
   Keep original tables/cells/facts and preferences unchanged.

Independent reviewer acceptance, actual producer replay and shared deployment
remain outstanding. Local success does not establish any of those outcomes.
