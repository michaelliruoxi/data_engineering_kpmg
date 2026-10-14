"""Explicit successor-issuer history with original filing identity preserved.

Only reviewed mappings are read from configuration. No acquired businesses,
spin-off parents, or related filers are inferred. Names are descriptive evidence
metadata: discovery and loading must retain each original filing's CIK,
accession, registrant name, and source URLs.
"""

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from lxml import etree, html


class IssuerHistoryError(ValueError):
    """A history configuration or archived evidence receipt is inconsistent."""


_FIELDS = {'current_cik', 'predecessor_cik', 'effective_date', 'current_name',
           'predecessor_name', 'evidence_url', 'relationship'}
_RETURN_FIELDS = {'schema_version', 'records', 'source_sha256', 'configuration_sha256', 'current_ciks'}


def _require(condition, message):
    if not condition:
        raise IssuerHistoryError(message)


def _cik(value):
    _require(isinstance(value, str) and re.fullmatch(r'[0-9]{10}', value) and int(value) > 0,
             'History CIKs must be canonical ten-digit positive identifiers.')
    return value


def _date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    _require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value),
             'History dates must use YYYY-MM-DD.')
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IssuerHistoryError('History date is not a valid calendar date.') from exc


def _evidence_url(value, current_cik, predecessor_cik):
    _require(isinstance(value, str) and value.isascii() and not any(ord(char) < 33 for char in value),
             'History evidence needs an ordinary SEC archive URL.')
    parsed = urlsplit(value)
    _require(parsed.scheme == 'https' and parsed.netloc == 'www.sec.gov'
             and not parsed.query and not parsed.fragment,
             'History evidence must use direct HTTPS www.sec.gov archive URLs.')
    matched = re.fullmatch(r'/Archives/edgar/data/([0-9]{1,10})/([0-9]{18})/([A-Za-z0-9][A-Za-z0-9_.-]*\.(?:htm|html))', parsed.path)
    _require(matched is not None, 'History evidence URL has an invalid archive path or accession.')
    archive_cik, accession, _ = matched.groups()
    _require(archive_cik.zfill(10) in {current_cik, predecessor_cik},
             'History evidence archive must belong to a mapped issuer.')
    _require(int(accession[:10]) > 0 and int(accession[-6:]) > 0,
             'History evidence accession must identify a filing.')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'Duplicate field in issuer history JSON.')
        result[key] = value
    return result


def _records(values):
    _require(isinstance(values, list), 'History records must be a list.')
    records, edges, archive_paths = [], set(), {}
    for value in values:
        _require(isinstance(value, Mapping) and set(value) == _FIELDS,
                 'History records require exactly the supported succession fields.')
        record = dict(value)
        current, predecessor = _cik(record['current_cik']), _cik(record['predecessor_cik'])
        _require(current != predecessor, 'An issuer cannot be its own predecessor.')
        edge = (current, predecessor)
        _require(edge not in edges, 'Duplicate issuer-history edge.')
        edges.add(edge)
        record['effective_date'] = _date(record['effective_date']).isoformat()
        for field in ('current_name', 'predecessor_name'):
            name = record[field]
            _require(isinstance(name, str) and bool(name) and name == name.strip()
                     and all(char.isprintable() for char in name), 'History names must be nonempty single-line evidence labels.')
        _require(record['relationship'] == 'successor_issuer',
                 'Only explicitly reviewed successor-issuer relationships are supported.')
        _evidence_url(record['evidence_url'], current, predecessor)
        archive_key = (current, record['effective_date'])
        old_url = archive_paths.setdefault(archive_key, record['evidence_url'])
        _require(old_url == record['evidence_url'], 'Different evidence documents collide in one history event archive.')
        records.append(record)
    return sorted(records, key=lambda record: (record['current_cik'], record['predecessor_cik'], record['effective_date']))


def _graph(records, roots):
    outgoing = defaultdict(list)
    for record in records:
        outgoing[record['current_cik']].append(record)
    visited, active = set(), set()

    def visit(cik):
        _require(cik not in active, 'Issuer history contains a CIK cycle.')
        if cik in visited:
            return
        active.add(cik)
        for record in outgoing[cik]:
            for older in outgoing[record['predecessor_cik']]:
                _require(older['effective_date'] <= record['effective_date'],
                         'Successor-chain effective dates are reversed.')
            visit(record['predecessor_cik'])
        active.remove(cik)
        visited.add(cik)

    for cik in tuple(outgoing):
        visit(cik)
    reachable = set()

    def mark(cik):
        if cik in reachable:
            return
        reachable.add(cik)
        for record in outgoing[cik]:
            mark(record['predecessor_cik'])

    for cik in roots:
        mark(cik)
    _require(all(record['current_cik'] in reachable for record in records),
             'Every history edge must be reachable from the current membership snapshot.')
    return outgoing


def _configuration_hash(records, roots):
    payload = {'schema_version': 1, 'records': records, 'current_ciks': roots}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def load_history(path, universe):
    """Read reviewed configuration and validate its graph against current members.

    Source URLs are validated structurally here; legal continuity was reviewed
    before a mapping was approved. No network or database operations occur.
    """
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IssuerHistoryError('Issuer history is not valid UTF-8 JSON.') from exc
    _require(isinstance(value, dict) and set(value) == {'schema_version', 'records'}
             and type(value['schema_version']) is int and value['schema_version'] == 1,
             'Unsupported issuer-history configuration schema.')
    _require(isinstance(universe, Mapping) and isinstance(universe.get('companies'), list),
             'A validated current-company snapshot is required.')
    members = [_cik(company.get('cik')) for company in universe['companies'] if isinstance(company, Mapping)]
    _require(len(members) == len(universe['companies']) and len(set(members)) == len(members),
             'Current membership CIKs must be distinct.')
    records = _records(value['records'])
    if universe.get('as_of') is not None:
        as_of = _date(universe['as_of'])
        _require(all(_date(record['effective_date']) <= as_of for record in records),
                 'A successor must be effective by the membership snapshot date.')
    roots = sorted({record['current_cik'] for record in records} & set(members))
    _graph(records, roots)
    return {'schema_version': 1, 'records': records, 'current_ciks': roots,
            'source_sha256': hashlib.sha256(raw).hexdigest(),
            'configuration_sha256': _configuration_hash(records, roots)}


def _validated_history(history):
    _require(isinstance(history, Mapping) and set(history) == _RETURN_FIELDS
             and type(history['schema_version']) is int and history['schema_version'] == 1,
             'Use the validated issuer-history object returned by load_history.')
    records = _records(history['records'])
    roots = history['current_ciks']
    _require(isinstance(roots, list) and roots == sorted({_cik(cik) for cik in roots}),
             'History snapshot roots must be distinct canonical CIKs.')
    _require(isinstance(history['source_sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', history['source_sha256']),
             'History source SHA-256 is missing or invalid.')
    _require(history['configuration_sha256'] == _configuration_hash(records, roots),
             'Issuer-history configuration changed after validation.')
    return records, roots, _graph(records, roots)


def history_jobs(history, since, until):
    """Produce inclusive filing-date jobs; never relabel predecessor filings.

    Each job starts at the original window start and ends at the earlier of the
    window end and approved succession cutoffs. Jobs merge only when both CIK
    and complete date window match, keeping current-business associations exact.
    """
    _, roots, outgoing = _validated_history(history)
    start, end = _date(since), _date(until)
    _require(start <= end, 'History start date must not follow its end date.')
    jobs = {}

    def walk(root, cik, cutoff, route):
        for record in outgoing[cik]:
            stop = min(cutoff, _date(record['effective_date']))
            if stop < start:
                continue
            chain = [*route, record]
            key = (record['predecessor_cik'], start.isoformat(), stop.isoformat())
            job = jobs.setdefault(key, {'job_key': 'predecessor:' + ':'.join(key), 'cik': key[0],
                'current_ciks': set(), 'since': key[1], 'until': key[2], 'forms': ['10-K', '10-Q'],
                'history_source_sha256': history['source_sha256'], 'routes': []})
            job['current_ciks'].add(root)
            job['routes'].append({'current_cik': root, 'records': deepcopy(chain)})
            walk(root, record['predecessor_cik'], stop, chain)

    for root in roots:
        walk(root, root, end, [])
    result = []
    for key in sorted(jobs):
        job = jobs[key]
        job['current_ciks'] = sorted(job['current_ciks'])
        job['routes'].sort(key=lambda route: (route['current_cik'], tuple(record['predecessor_cik'] for record in route['records'])))
        job['evidence_urls'] = sorted({record['evidence_url'] for route in job['routes'] for record in route['records']})
        result.append(job)
    return result


def _retrieval_time(value):
    _require(isinstance(value, str) and bool(value), 'Evidence retrieval time is missing.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise IssuerHistoryError('Evidence retrieval time is invalid.') from exc
    _require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0),
             'Evidence retrieval time must be timezone-aware UTC.')
    return value


def _registration_bytes(data):
    from .acquisition import _BLOCK_MARKERS
    _require(isinstance(data, bytes) and bool(data) and not any(marker in data.lower() for marker in _BLOCK_MARKERS),
             'Registration evidence is empty or an access-block response.')
    try:
        document = html.fromstring(data, parser=html.HTMLParser(encoding='utf-8', no_network=True))
    except (ValueError, TypeError, etree.Error) as exc:
        raise IssuerHistoryError('Registration evidence is not readable HTML.') from exc
    visible = ' '.join(' '.join(document.itertext()).split())
    _require(re.search(r'\b8\s*-\s*K(?:12B)?\b', visible, re.I), 'Registration evidence does not identify a Form 8-K.')


def archive_history_evidence(client, history, data_root):
    """Archive approved 8-K bytes and immutable receipts, returning enriched records.

    A verified receipt is reused without HTTP. Existing unreceipted bytes must
    agree with freshly fetched bytes; changed originals are never overwritten.
    This function does not change history, queues, filing identities or a DB.
    """
    from .acquisition import _immutable_write
    records, _, _ = _validated_history(history)
    root = Path(data_root).resolve()
    results, cached = [], {}
    for record in records:
        relative = Path('data/raw/issuer_history') / record['current_cik'] / record['effective_date']
        directory = (root / relative).resolve()
        _require(directory.is_relative_to(root), 'History archive path escapes its data root.')
        evidence_path, receipt_path = directory / 'registration.htm', directory / 'receipt.json'
        _require(evidence_path.resolve().parent == directory and receipt_path.resolve().parent == directory,
                 'History evidence files must remain in their event archive directory.')
        expected = {'schema_version': 1, 'current_cik': record['current_cik'], 'effective_date': record['effective_date'],
                    'evidence_url': record['evidence_url'], 'evidence_local_path': (relative / 'registration.htm').as_posix()}
        cache_key = (record['current_cik'], record['effective_date'])
        if cache_key in cached:
            receipt = cached[cache_key]
        elif receipt_path.exists():
            try:
                receipt = json.loads(receipt_path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise IssuerHistoryError('History evidence receipt is invalid JSON.') from exc
            _require(isinstance(receipt, dict) and all(receipt.get(key) == value for key, value in expected.items()),
                     'History evidence receipt identity differs from approved configuration.')
            _require(evidence_path.is_file(), 'History evidence bytes are missing.')
            data = evidence_path.read_bytes()
            _registration_bytes(data)
            _require(receipt.get('evidence_size_bytes') == len(data)
                     and receipt.get('evidence_sha256') == hashlib.sha256(data).hexdigest(),
                     'History evidence hash or size differs from its receipt.')
            _retrieval_time(receipt.get('evidence_retrieved_at'))
            cached[cache_key] = receipt
        else:
            _require(client is not None, 'Unarchived history evidence requires an approved SEC client.')
            data, metadata = client.get(record['evidence_url'])
            _registration_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            _require(metadata.get('http_status') == 200 and metadata.get('size_bytes') == len(data)
                     and metadata.get('sha256') == digest, 'SEC evidence transport metadata differs from received bytes.')
            retrieved = _retrieval_time(metadata.get('retrieved_at_utc'))
            receipt = {**expected, 'evidence_sha256': digest, 'evidence_size_bytes': len(data),
                       'evidence_retrieved_at': retrieved, 'content_type': metadata.get('content_type')}
            _immutable_write(evidence_path, data)
            _immutable_write(receipt_path, (json.dumps(receipt, sort_keys=True, indent=2) + '\n').encode('utf-8'))
            cached[cache_key] = receipt
        results.append({**record, **{key: receipt[key] for key in
                                   ('evidence_sha256', 'evidence_local_path', 'evidence_retrieved_at')}})
    return results
