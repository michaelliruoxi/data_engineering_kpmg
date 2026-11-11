"""Separate re-acquisition receipts from immutable processing identity."""

import json
import re


MANIFEST_PROVENANCE = frozenset({'document_set_manifest_sha256', 'core_manifest_sha256'})
STABLE_IDENTITY = frozenset({'versions', 'parser_revisions', 'source_hashes', 'source_documents', 'document_set'})


def _same_json(stored, current):
    # Python equality conflates booleans with numbers; JSON/JSONB does not.
    try:
        return json.dumps(stored, sort_keys=True, allow_nan=False) == json.dumps(current, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return False


def same_processing_identity(stored, current):
    """Ignore only manifest digests when both receipts prove the full identity.

    Source hashes, document membership, parser revisions, versions, profiles and
    every other configuration field remain exact. Sparse historical metadata
    cannot prove digest-only equivalence, so it retains exact replay semantics.
    This comparison never replaces the first receipt's provenance.
    """
    if not isinstance(stored, dict) or not isinstance(current, dict):
        return False
    if _same_json(stored, current):
        return True
    for configuration in (stored, current):
        if any(not isinstance(configuration.get(key), dict) or not configuration[key]
               for key in STABLE_IDENTITY):
            return False
        if any(not isinstance(configuration.get(key), str)
               or re.fullmatch(r'[0-9a-f]{64}', configuration[key]) is None
               for key in MANIFEST_PROVENANCE):
            return False
    return _same_json({key: value for key, value in stored.items() if key not in MANIFEST_PROVENANCE},
                      {key: value for key, value in current.items() if key not in MANIFEST_PROVENANCE})
