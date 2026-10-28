"""Read-only inventory and local SEC downloads for Emma's ten-company shortlist.

Use the team's locked Python environment with PYTHONPATH pointing to repo/src.
This script never connects to or writes to PostgreSQL. Existing originals are
immutable. SEC identity is read only from the pre-existing EDGAR_IDENTITY env var.
"""
from __future__ import annotations

import argparse
from dataclasses import fields
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from lxml import html
from sec_pipeline.acquisition import (
    AccessBlocked, FilingIdentity, SecClient,
    _archive_link, discover_filings, download_filing,
)
from sec_pipeline.filing_sources import verify_filing


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def immutable_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('Existing source bytes differ; original was not overwritten.')
        return
    with path.open('xb') as handle:
        handle.write(data)


def load_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    if config.get('forms') != ['10-K', '10-Q'] or config.get('amendments_included') is not False:
        raise ValueError('Only original 10-K and 10-Q are supported by this workflow.')
    start, end = date.fromisoformat(config['since']), date.fromisoformat(config['until'])
    if start > end:
        raise ValueError('Date window is reversed.')
    companies = config['companies']
    if len(companies) != 10 or len({x['ticker'] for x in companies}) != 10 or len({x['cik'] for x in companies}) != 10:
        raise ValueError('Expected exactly ten distinct tickers and CIKs.')
    if any(not re.fullmatch(r'\d{10}', x['cik']) or int(x['cik']) == 0 for x in companies):
        raise ValueError('CIKs must be ten-digit nonzero identifiers.')
    return config


def scope_key(config):
    core = {k:config[k] for k in ('since', 'until', 'forms', 'companies')}
    return digest(json.dumps(core, sort_keys=True).encode())


def in_scope(record, config):
    return (record.get('cik') in {x['cik'] for x in config['companies']}
            and record.get('form') in config['forms']
            and config['since'] <= record.get('filing_date', '') <= config['until'])


def identity(record):
    return FilingIdentity(**{f.name:record[f.name] for f in fields(FilingIdentity)})


def manifest_candidates(root):
    # Each --archive-root is an explicit data root, not a scan of nested projects.
    candidates = list((root / 'data/raw/sec').glob('*/source_manifest.json'))
    if (root / 'source_manifest.json').is_file():
        candidates.append(root / 'source_manifest.json')
    return sorted(candidates)


def verify_tickers(payload, companies):
    """Require a fresh SEC ticker mapping before using the configured CIKs online."""
    rows = list(payload.values())
    for company in companies:
        matches = {str(row['cik_str']).zfill(10) for row in rows
                   if row.get('ticker') == company['ticker']}
        if matches != {company['cik']}:
            raise ValueError('SEC ticker/CIK mismatch for ' + company['ticker'])


class RecordedClient(SecClient):
    def __init__(self, evidence_root):
        super().__init__()
        self.evidence_root = Path(evidence_root)

    def get(self, url):
        data, receipt = super().get(url)
        if url.endswith('.json'):
            name = digest(url.encode())[:16] + '-' + receipt['sha256'][:16] + '.json'
            immutable_write(self.evidence_root / name, data)
            save_json(self.evidence_root / (name + '.receipt.json'),
                      {'source_url':url, 'local_path':name, **receipt})
        return data, receipt


def discover(config, output):
    path = output / 'discovery.json'
    if path.exists():
        state = json.loads(path.read_text())
        if state['scope_key'] != scope_key(config):
            raise ValueError('Changed scope requires a new output directory.')
    else:
        state = {'scope_key':scope_key(config), 'config':config, 'companies':{},
                 'started_at_utc':now(), 'discovery_complete':False}
    save_json(path, state)
    with RecordedClient(output / 'sec_api_evidence') as client:
        verify_tickers(client.get_json('https://www.sec.gov/files/company_tickers.json'), config['companies'])
        for company in config['companies']:
            ticker = company['ticker']
            if state['companies'].get(ticker, {}).get('status') == 'discovered':
                continue
            try:
                print('Discovering ' + ticker, flush=True)
                rows = discover_filings(client, company['cik'], config['since'], config['until'])
                state['companies'][ticker] = {'status':'discovered', 'checked_at_utc':now(), 'filings':rows}
            except (ValueError, OSError) as exc:
                state['companies'][ticker] = {'status':'discovery_failed', 'error_type':type(exc).__name__}
                state['updated_at_utc'] = now()
                save_json(path, state)
                raise
            state['updated_at_utc'] = now()
            save_json(path, state)
        state['discovery_complete'] = all(state['companies'].get(c['ticker'], {}).get('status') == 'discovered'
                                          for c in config['companies'])
        save_json(path, state)
    return state


def exhibit_links(index_bytes, filing):
    tree = html.fromstring(index_bytes, parser=html.HTMLParser(no_network=True))
    urls = set()
    for row in tree.xpath('//table//tr'):
        cells = row.xpath('./td')
        if len(cells) < 4:
            continue
        kind = ' '.join(cells[3].text_content().split()).upper()
        if not re.fullmatch(r'EX-21(?:\.\d+)?', kind):
            continue
        links = cells[2].xpath('.//a/@href')
        if len(links) != 1:
            raise ValueError('Exhibit 21 index row has an ambiguous link.')
        urls.add(_archive_link(links[0], filing))
    return sorted(urls)


def verify_exhibit_receipt(receipt, manifest, root):
    if receipt.get('accession_number') != manifest['accession_number']:
        raise ValueError('Exhibit receipt belongs to another filing.')
    index = manifest['index_document']
    if receipt.get('index_sha256') != index['sha256']:
        raise ValueError('Exhibit receipt no longer matches the filing index.')
    filing = identity({**manifest, 'primary_document':next(x['source_url'].rsplit('/',1)[-1] for x in manifest['files']
                                                        if x['role']=='main_inline_xbrl_filing')})
    expected = exhibit_links((root / index['local_path']).read_bytes(), filing)
    if sorted(x['source_url'] for x in receipt.get('files', [])) != expected:
        raise ValueError('Exhibit sources differ from the verified filing index.')
    for record in receipt.get('files', []):
        path = (root / record['local_path']).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError('Exhibit path escapes the output directory.')
        data = path.read_bytes()
        if len(data) != record['size_bytes'] or digest(data) != record['sha256']:
            raise ValueError('Exhibit checksum mismatch.')
    status = 'downloaded' if expected else 'not_listed_in_index_review_required'
    if receipt.get('status') != status:
        raise ValueError('Exhibit status is inconsistent with index evidence.')
    return receipt


def save_exhibits(client, manifest, root):
    index = manifest['index_document']
    primary = next(x for x in manifest['files'] if x['role']=='main_inline_xbrl_filing')
    filing = identity({**manifest, 'primary_document':primary['source_url'].rsplit('/',1)[-1]})
    urls = exhibit_links((root / index['local_path']).read_bytes(), filing)
    receipt_path = root / 'data/raw/sec' / filing.accession_number / 'exhibit21_manifest.json'
    if receipt_path.exists():
        return verify_exhibit_receipt(json.loads(receipt_path.read_text()), manifest, root)
    receipt = {'accession_number':filing.accession_number, 'cik':filing.cik,
               'index_sha256':index['sha256'], 'checked_at_utc':now(), 'files':[],
               'status':'downloaded' if urls else 'not_listed_in_index_review_required',
               'note':'No direct EX-21 link requires manual review of incorporation by reference; it is not proof of unavailability.'}
    for url in urls:
        data, meta = client.get(url)
        relative = Path('data/raw/sec') / filing.accession_number / urlsplit(url).path.rsplit('/',1)[-1]
        immutable_write(root / relative, data)
        receipt['files'].append({'source_url':url, 'local_path':relative.as_posix(), **meta,
                                 'verification':'source link and byte integrity only; subsidiary extraction not performed'})
    save_json(receipt_path, receipt)
    return verify_exhibit_receipt(receipt, manifest, root)


def seed_archives(config, roots, output):
    """Reuse supplied original bytes after full verification, without overwriting."""
    copied = set()
    for source_root in roots:
        source_root = Path(source_root).resolve()
        if source_root == output.resolve():
            continue
        for source_manifest in manifest_candidates(source_root):
            raw = json.loads(source_manifest.read_text(encoding='utf-8'))
            if not in_scope(raw, config):
                continue
            manifest = verify_filing(source_manifest, source_root)
            destination = output / 'data/raw/sec' / manifest['accession_number'] / 'source_manifest.json'
            if destination.exists():
                existing = verify_filing(destination, output)
                for key in ('cik','accession_number','form','filing_date','reporting_period_end'):
                    if existing[key] != manifest[key]:
                        raise ValueError('Existing archive identity conflicts with supplied archive.')
                old_hashes = {r['role']:r['sha256'] for r in [existing['index_document'],*existing['files']]}
                new_hashes = {r['role']:r['sha256'] for r in [manifest['index_document'],*manifest['files']]}
                if old_hashes != new_hashes:
                    raise ValueError('Existing archive bytes conflict with supplied archive.')
                continue
            for record in [manifest['index_document'],*manifest['files']]:
                relative = Path(record['local_path'])
                target = (output / relative).resolve()
                if not target.is_relative_to(output.resolve()):
                    raise ValueError('Supplied archive path escapes output.')
                immutable_write(target, (source_root / relative).read_bytes())
            immutable_write(destination, source_manifest.read_bytes())
            verify_filing(destination, output)
            copied.add(manifest['accession_number'])
    return sorted(copied)


def download(config, output, limit, roots=()):
    discovery = json.loads((output / 'discovery.json').read_text())
    if discovery['scope_key'] != scope_key(config):
        raise ValueError('Discovery and requested scope differ.')
    rows = [row for c in discovery['companies'].values() if c.get('status')=='discovered' for row in c['filings']]
    if any(not in_scope(row, config) for row in rows):
        raise ValueError('Discovery contains a filing outside the configured scope.')
    seed_archives(config, roots, output)
    state_path = output / 'download_status.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        'scope_key':scope_key(config), 'filings':{}, 'download_stage_complete':False}
    if state['scope_key'] != scope_key(config):
        raise ValueError('Download checkpoint has another scope.')
    state['download_stage_complete'] = False
    save_json(state_path, state)
    attempted = 0
    with RecordedClient(output / 'sec_api_evidence') as client:
        for row in sorted(rows, key=lambda r:(r['cik'],r['filing_date'],r['accession_number'])):
            accession = row['accession_number']
            previous = state['filings'].get(accession, {})
            if previous.get('status') in {'verified', 'exhibit_review_required'}:
                manifest = verify_filing(output / previous['manifest'], output)
                if any(manifest.get(k) != row.get(k) for k in ('cik','accession_number','form','filing_date','reporting_period_end')):
                    raise ValueError('Saved archive identity disagrees with discovery.')
                if row['form']=='10-K':
                    receipt = json.loads((output / previous['exhibit_manifest']).read_text())
                    verify_exhibit_receipt(receipt, manifest, output)
                continue
            if attempted >= limit:
                break
            attempted += 1
            try:
                print('Archiving ' + row['cik'] + ' ' + accession, flush=True)
                manifest_path = download_filing(client, row, output)
                manifest = verify_filing(manifest_path, output)
                result = {'status':'verified', 'manifest':manifest_path.relative_to(output).as_posix()}
                if row['form']=='10-K':
                    receipt = save_exhibits(client, manifest, output)
                    result['exhibit_manifest'] = f'data/raw/sec/{accession}/exhibit21_manifest.json'
                    if receipt['status'] != 'downloaded':
                        result['status'] = 'exhibit_review_required'
                state['filings'][accession] = result
            except (ValueError, OSError) as exc:
                state['filings'][accession] = {'status':'failed', 'error_type':type(exc).__name__}
                save_json(state_path, state)
                raise
            state['updated_at_utc'] = now()
            save_json(state_path, state)
    state['download_stage_complete'] = (discovery.get('discovery_complete') is True
        and bool(rows) and all(state['filings'].get(r['accession_number'],{}).get('status')=='verified' for r in rows))
    state['scope_acceptance_status'] = config.get('scope_status', 'unconfirmed')
    state['updated_at_utc'] = now()
    save_json(state_path, state)
    return state


def inventory(config, roots, discovery=None, database=None):
    records, errors = {}, []
    for root in roots:
        root = Path(root).resolve()
        candidates = manifest_candidates(root)
        for path in candidates:
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
                if not in_scope(raw, config):
                    continue
                verified = verify_filing(path, root)
                accession = verified['accession_number']
                fingerprint = {x['role']:x['sha256'] for x in [verified['index_document'], *verified['files']]}
                if accession in records:
                    old = records[accession]
                    if old['cik'] != verified['cik'] or old['source_hashes'] != fingerprint:
                        raise ValueError('Conflicting archives for the same accession.')
                    old['copies_found'] += 1
                    continue
                records[accession] = {k:verified[k] for k in ('cik','company','accession_number','form','filing_date','reporting_period_end','filing_index_url')}
                records[accession].update({'source_hashes':fingerprint, 'source_file_count':len(fingerprint),
                                          'archive_status':'verified', 'copies_found':1})
            except (ValueError, OSError, KeyError, TypeError) as exc:
                errors.append({'manifest_name':path.name, 'error_type':type(exc).__name__, 'reason':str(exc)[:180]})
    if discovery and discovery.get('scope_key') != scope_key(config):
        raise ValueError('Inventory discovery scope does not match.')
    if database is not None:
        window = database.get('export_window', {})
        if (window.get('basis') != 'filing_date' or window.get('since','9999') > config['since']
                or window.get('until','') < config['until'] or not database.get('exported_at')):
            raise ValueError('Database export does not cover the full configured window.')
        if not isinstance(database.get('companies'),list) or not isinstance(database.get('filings'),list):
            raise ValueError('Database export must contain company and filing arrays.')
        if len({r['accession_number'] for r in database['filings']}) != len(database['filings']):
            raise ValueError('Database export contains duplicate accessions.')
    rows = []
    for company in config['companies']:
        known = [r for r in records.values() if r['cik']==company['cik']]
        found = (discovery or {}).get('companies',{}).get(company['ticker'],{})
        expected = found.get('filings') if found.get('status')=='discovered' else None
        missing = None if expected is None else sorted(r['accession_number'] for r in expected if r['accession_number'] not in records)
        company_evidence = [r for r in (database or {}).get('companies',[])
                            if r.get('requested_cik')==company['cik']]
        db_known = len(company_evidence)==1 and isinstance(company_evidence[0].get('company_found'),bool)
        db_rows = [r for r in (database or {}).get('filings',[]) if r.get('cik')==company['cik'] and in_scope(r,config)]
        db_ids = {r['accession_number'] for r in db_rows}
        db_missing = None if not db_known or expected is None else sorted(r['accession_number'] for r in expected if r['accession_number'] not in db_ids)
        rows.append({**company, 'verified_in_supplied_archives':len(known),
                     'local_10k':sum(r['form']=='10-K' for r in known), 'local_10q':sum(r['form']=='10-Q' for r in known),
                     'sec_expected_count':None if expected is None else len(expected),
                     'not_in_supplied_archives':missing, 'shared_database_count':len(db_rows) if db_known else None,
                     'not_in_shared_database':db_missing,
                     'shared_database_status':'export_compared' if db_known else 'not_verified',
                     'exhibit21_history_status':'not_verified'})
    return {'checked_at_utc':now(), 'scope_key':scope_key(config), 'config':config,
            'status':'partial_evidence_only', 'companies':rows, 'verified_filings':list(records.values()),
            'archive_errors':errors, 'limits':['Supplied archives are not a complete team inventory.',
              'Missing in supplied archives does not mean missing from shared database.',
              'Expected counts require completed SEC discovery; unknown is not zero.',
              'Zero local 10-K files is not proof that Exhibit 21 is unavailable.']}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['inventory','discover','download'])
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--archive-root', type=Path, action='append', default=[])
    p.add_argument('--database-export', type=Path, help='JSON cell exported by the supplied read-only SQL')
    p.add_argument('--limit', type=int, default=10, help='Maximum new filing attempts per download run')
    args = p.parse_args(argv)
    config = load_config(args.config)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.limit < 1:
        p.error('--limit must be positive')
    if args.mode=='inventory':
        if not args.archive_root:
            p.error('inventory requires at least one --archive-root')
        discovery_path = output / 'discovery.json'
        result = inventory(config, args.archive_root, json.loads(discovery_path.read_text()) if discovery_path.exists() else None,
                           json.loads(args.database_export.read_text()) if args.database_export else None)
        save_json(output/'inventory.json', result)
        print(json.dumps({'result':'partial_evidence_only', 'verified_unique_filings':len(result['verified_filings']),
                          'errors':len(result['archive_errors'])}))
        return 0 if not result['archive_errors'] else 2
    try:
        result = discover(config, output) if args.mode=='discover' else download(config, output, args.limit, args.archive_root)
        complete = result.get('discovery_complete') if args.mode=='discover' else result.get('download_stage_complete')
        print(json.dumps({'stage':args.mode, 'stage_complete':complete, 'task_acceptance':'not_claimed'}))
        return 0 if complete else 2
    except AccessBlocked:
        print('SEC access blocked; stopped without retries. Resolve the cause before resuming.', file=sys.stderr)
        return 3
    except (ValueError, OSError) as exc:
        save_json(output/'last_failure.json', {'at_utc':now(), 'stage':args.mode, 'error_type':type(exc).__name__})
        print('Stage stopped: '+type(exc).__name__+'. Verify network and runtime EDGAR_IDENTITY; no database writes occurred.', file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
