"""Store explicit issuer continuity without changing original filing ownership.

Callers verify and archive the official evidence before passing its receipt.
This helper validates identity/receipt shape and immutable replay, but does not
fetch evidence, infer successions, expand a queue, or own transaction completion.
"""

from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
import re

from .database import CompanyInput, DataConflict, DatabaseError, register_company


_FIELDS = {
    "current_cik", "predecessor_cik", "current_name", "predecessor_name",
    "effective_date", "evidence_url", "evidence_sha256", "evidence_local_path",
    "evidence_retrieved_at",
}
_EVIDENCE_URL = re.compile(
    r"https://www[.]sec[.]gov/Archives/edgar/data/([1-9][0-9]{0,9})/[0-9]{18}/[A-Za-z0-9][A-Za-z0-9_.-]*\Z"
)


def _checked(record):
    if (not isinstance(record, Mapping)
            or set(record) not in (_FIELDS, _FIELDS | {"relationship"})):
        raise DatabaseError("Issuer history records require exactly the supported identity and evidence fields.")
    result = dict(record)
    if "relationship" in result and result.pop("relationship") != "successor_issuer":
        raise DatabaseError("Issuer history supports only the successor_issuer relationship.")
    for key in ("current_cik", "predecessor_cik"):
        value = result[key]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{10}", value) or int(value) == 0:
            raise DatabaseError("Issuer history CIKs must be nonzero ten-digit strings.")
    if result["current_cik"] == result["predecessor_cik"]:
        raise DatabaseError("A successor and predecessor must be distinct issuer CIKs.")
    for key in ("current_name", "predecessor_name"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise DatabaseError("Both issuer names must be nonempty text.")

    effective = result["effective_date"]
    if isinstance(effective, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", effective):
        try:
            effective = date.fromisoformat(effective)
        except ValueError as exc:
            raise DatabaseError("The succession effective date must be a calendar date.") from exc
    if type(effective) is not date:
        raise DatabaseError("The succession effective date must be an ISO calendar date.")
    result["effective_date"] = effective

    url = result["evidence_url"]
    match = _EVIDENCE_URL.fullmatch(url) if isinstance(url, str) else None
    if match is None or match[1].zfill(10) not in (result["current_cik"], result["predecessor_cik"]):
        raise DatabaseError("Evidence must use a direct HTTPS SEC archive URL for one of the two issuer CIKs.")
    digest = result["evidence_sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise DatabaseError("Evidence SHA-256 must contain 64 lowercase hexadecimal characters.")
    path = result["evidence_local_path"]
    if not isinstance(path, str) or not path.strip() or any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise DatabaseError("Evidence needs a repository-relative local file path.")
    portable = path.replace("\\", "/")
    if (PureWindowsPath(path).drive or PureWindowsPath(path).root or PurePosixPath(portable).is_absolute()
            or any(part in {"", ".", ".."} for part in portable.split("/"))):
        raise DatabaseError("Evidence needs a repository-relative local file path without traversal.")

    received = result["evidence_retrieved_at"]
    if isinstance(received, str):
        try:
            received = datetime.fromisoformat(received)
        except ValueError as exc:
            raise DatabaseError("Evidence retrieval time must be an ISO timestamp with a timezone.") from exc
    if not isinstance(received, datetime) or received.tzinfo is None or received.utcoffset() is None:
        raise DatabaseError("Evidence retrieval time must include a timezone.")
    result["evidence_retrieved_at"] = received.astimezone(timezone.utc)
    return result


def store_issuer_history(conn, records):
    """Return succession IDs in input order; preserve first receipt and profiles.

    A successor/predecessor pair is immutable: changed effective date, evidence
    URL, or source hash conflicts. Re-fetching identical evidence may supply a
    new local locator or receipt time; the stored original receipt is retained.
    All input records and duplicate-pair consistency are checked before SQL.
    """
    if conn.autocommit:
        raise DatabaseError("Issuer history writes require autocommit=False and caller-managed transactions.")
    if isinstance(records, (str, bytes, Mapping)):
        raise DatabaseError("Issuer history requires an iterable of record mappings.")
    try:
        items = [_checked(record) for record in records]
    except TypeError as exc:
        raise DatabaseError("Issuer history requires an iterable of record mappings.") from exc
    identities = {}
    for record in items:
        pair = record["current_cik"], record["predecessor_cik"]
        immutable = record["effective_date"], record["evidence_url"], record["evidence_sha256"]
        if identities.setdefault(pair, immutable) != immutable:
            raise DataConflict("Conflicting evidence or effective dates for one issuer pair.")

    company_ids, result = {}, []
    for record in items:
        for cik_key, name_key in (("current_cik", "current_name"), ("predecessor_cik", "predecessor_name")):
            cik = record[cik_key]
            if cik not in company_ids:
                company_ids[cik] = register_company(conn, CompanyInput(cik=cik, name=record[name_key]))
        successor = company_ids[record["current_cik"]]
        predecessor = company_ids[record["predecessor_cik"]]
        if successor == predecessor:
            raise DataConflict("Distinct issuer CIKs unexpectedly resolved to the same company identity.")
        immutable = (successor, predecessor, record["effective_date"], record["evidence_url"], record["evidence_sha256"])
        row = conn.execute(
            """INSERT INTO sec.issuer_successions
               (successor_company_id, predecessor_company_id, effective_date,
                evidence_url, evidence_sha256, evidence_local_path, evidence_retrieved_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (successor_company_id, predecessor_company_id) DO NOTHING RETURNING id""",
            (*immutable, record["evidence_local_path"], record["evidence_retrieved_at"]),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """SELECT id FROM sec.issuer_successions
                   WHERE successor_company_id=%s AND predecessor_company_id=%s
                     AND effective_date=%s AND evidence_url=%s AND evidence_sha256=%s FOR SHARE""",
                immutable,
            ).fetchone()
        if row is None:
            raise DataConflict("Conflicting issuer succession evidence or effective date; preserve the original relationship.")
        result.append(row[0])
    return result
