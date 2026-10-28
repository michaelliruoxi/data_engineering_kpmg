"""Draft table-side adapter; uses existing receiving-DB financial_tables.id.

No chunk is generated here. Synthetic inputs require explicit opt-in and remain
marked synthetic. Michael/Ruby must approve field names and actual producer
samples before this draft is treated as a shared end-to-end contract.
"""
import hashlib
from uuid import UUID

from lxml import html

from .table_sections import _nodes, canonical


CONTRACT = "draft-financial-table-id-v1"
FIELDS = ("chunk_id", "chunk_type", "table_ids", "filing_id", "accession", "processing_set",
          "source/document_set", "source_document_id", "table_version", "source_locator", "synthetic")
_MISSING = object()


def _field(record, path):
    value = record
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def resolve_table_chunks(chunks, tables, sources, *, input_fields=None, output_fields=None, allow_synthetic=False):
    """Return one mapping per explicit chunk/table pair; reject partial chunks.

    ``tables`` are receiving-DB mappings, enriched with accession, selected
    processing_set (contract+id or explicit null), and source/document_set.
    ``sources`` maps those source_document_id strings to unchanged HTML bytes.
    Field mappings are canonical-name -> dotted input path / output name.
    """
    aliases = dict(input_fields or {})
    if set(aliases) - set(FIELDS) or any(not isinstance(path, str) or not path for path in aliases.values()):
        raise ValueError("Unsupported input field mapping.")
    table_index = {}
    for table in tables:
        identifier = str(UUID(str(table["id"])))
        if identifier in table_index:
            raise ValueError("Receiving table IDs must be unique.")
        table_index[identifier] = canonical(table)
    results = []
    for chunk in chunks:
        values = {field: _field(chunk, aliases.get(field, field)) for field in FIELDS}
        chunk_id = values["chunk_id"]
        base = {"chunk_id": None if chunk_id is _MISSING else chunk_id, "table_id": None,
                "contract": CONTRACT, "contract_status": "pending_Michael_Ruby_confirmation",
                "synthetic": values["synthetic"] is True, "mapping_status": "rejected", "evidence": {}}

        def reject(reason):
            results.append(canonical(base | {"evidence": {"reason": reason}}))

        if chunk_id is _MISSING or not isinstance(chunk_id, str) or not chunk_id.strip():
            reject("missing_chunk_id")
            continue
        if values["chunk_type"] != "table":
            reject("not_a_table_chunk")
            continue
        if base["synthetic"] and not allow_synthetic:
            reject("synthetic_requires_explicit_opt_in")
            continue
        identifiers = values["table_ids"]
        if isinstance(identifiers, str):
            identifiers = [identifiers]
        try:
            if not isinstance(identifiers, list) or not identifiers:
                raise ValueError()
            identifiers = [str(UUID(value)) for value in identifiers]
        except (ValueError, TypeError, AttributeError):
            reject("table_ids_must_be_existing_UUIDs_not_keys_or_chunk_ids")
            continue
        if len(set(identifiers)) != len(identifiers):
            reject("duplicate_table_ids_in_chunk")
            continue
        if any(identifier not in table_index for identifier in identifiers):
            reject("unknown_receiving_table_id")
            continue
        selected = [table_index[identifier] for identifier in identifiers]
        scope_fields = ("filing_id", "accession", "processing_set", "source/document_set", "source_document_id", "table_version")
        missing = [field for field in scope_fields if values[field] is _MISSING]
        if missing:
            reject("missing_explicit_scope:" + ",".join(missing))
            continue
        wrong = [field for field in scope_fields if any(canonical(values[field]) != table.get(field, table.get("extraction_version") if field == "table_version" else None) for table in selected)]
        if wrong:
            reject("scope_mismatch:" + ",".join(wrong))
            continue
        source = sources.get(str(values["source_document_id"]))
        if source is None:
            reject("source_HTML_unavailable")
            continue
        if any(table["structure"].get("html_sha256") != hashlib.sha256(source).hexdigest() for table in selected):
            reject("source_hash_mismatch")
            continue
        locator = values["source_locator"]
        if not isinstance(locator, dict) or not locator.get("xpath"):
            reject("missing_chunk_source_locator")
            continue
        tree = html.document_fromstring(source, parser=html.HTMLParser(encoding="utf-8", no_network=True)).getroottree()
        fragment = _nodes(tree, locator["xpath"])
        parents = [_nodes(tree, table["structure"].get("source_xpath")) for table in selected]
        if len(fragment) != 1 or any(len(nodes) != 1 or nodes[0].tag != "table" for nodes in parents):
            reject("unresolved_chunk_or_table_locator")
            continue
        node = fragment[0]
        if any(parent[0] != node and parent[0] not in list(node.iterancestors()) and node not in list(parent[0].iterancestors()) for parent in parents):
            reject("chunk_locator_outside_declared_table")
            continue
        for table in selected:
            results.append(canonical(base | {"table_id": table["id"], "table_key": table["table_key"],
                "table_xpath": table["structure"]["source_xpath"],
                **{field: values[field] for field in scope_fields},
                "mapping_status": "synthetic_fixture_mapped" if base["synthetic"] else "draft_mapped",
                "evidence": {"table_identity": "existing financial_tables.id; never generated by adapter",
                             "html_sha256": hashlib.sha256(source).hexdigest(), "chunk_source_locator": locator,
                             "scope_selection": "explicit" if values["processing_set"] else "unselected_not_approved"}}))
    names = dict(output_fields or {})
    known = set(FIELDS) | {"table_id", "table_key", "evidence", "contract", "contract_status", "mapping_status", "table_xpath"}
    if set(names) - known or any(not isinstance(name, str) or not name for name in names.values()):
        raise ValueError("Unsupported output field mapping.")
    if len({names.get(key, key) for key in known}) != len(known):
        raise ValueError("Output field names collide.")
    return [{names.get(key, key): value for key, value in row.items()} for row in results]
