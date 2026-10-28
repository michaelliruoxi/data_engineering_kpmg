import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('shortlist_acquisition', ROOT/'scripts/shortlist_acquisition.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@pytest.fixture
def config():
    return m.load_config(ROOT/'config/shortlist.json')


@pytest.fixture
def filing():
    return m.FilingIdentity(cik='0000320193', company='Apple Inc.',
       accession_number='0000320193-25-000079', form='10-K',
       filing_date='2025-10-31', reporting_period_end='2025-09-27',
       primary_document='aapl-20250927.htm')


def index_row(kind, href):
    return f'<tr><td>1</td><td>Subsidiaries</td><td><a href="{href}">file</a></td><td>{kind}</td></tr>'


def test_unknown_expected_is_not_zero(config, tmp_path):
    result=m.inventory(config,[tmp_path])
    assert len(result['companies'])==10
    assert all(r['sec_expected_count'] is None and r['shared_database_count'] is None
               and r['not_in_supplied_archives'] is None for r in result['companies'])


def test_filing_date_boundary_and_amendment(config):
    row={'cik':'0000320193','form':'10-K','filing_date':config['since']}
    assert m.in_scope(row,config)
    row['filing_date']=config['until']
    assert m.in_scope(row,config)
    row['filing_date']='2021-10-12'
    assert not m.in_scope(row,config)
    row.update(filing_date='2025-10-31',form='10-K/A')
    assert not m.in_scope(row,config)


def test_duplicate_shortlist_rejected(config,tmp_path):
    config['companies'][1]=dict(config['companies'][0])
    p=tmp_path/'config.json';p.write_text(json.dumps(config))
    with pytest.raises(ValueError,match='distinct'):
        m.load_config(p)


def test_current_ticker_conflict_rejected(config):
    payload={str(i):{'ticker':c['ticker'],'cik_str':int(c['cik'])} for i,c in enumerate(config['companies'])}
    m.verify_tickers(payload,config['companies'])
    payload['0']['cik_str']=789019
    with pytest.raises(ValueError,match='AAPL'):
        m.verify_tickers(payload,config['companies'])


def test_ex21_exact_types_and_duplicate_links(filing):
    data=('<table>'+index_row('EX-21.1','ex21.htm')+index_row('EX-21','ex21.htm')
          +index_row('EX-210','unrelated.htm')+'</table>').encode()
    assert m.exhibit_links(data,filing)==[filing.archive_url+'ex21.htm']


def test_ex21_external_link_rejected(filing):
    data=('<table>'+index_row('EX-21','https://example.org/ex21.htm')+'</table>').encode()
    with pytest.raises(ValueError):m.exhibit_links(data,filing)


def test_ex21_other_accession_rejected(filing):
    data=('<table>'+index_row('EX-21','/Archives/edgar/data/320193/000032019324000123/ex21.htm')+'</table>').encode()
    with pytest.raises(ValueError):m.exhibit_links(data,filing)


def test_absent_ex21_requires_review(filing):
    assert m.exhibit_links(b'<table><tr><td>10-K</td></tr></table>',filing)==[]


def test_existing_bytes_never_overwritten(tmp_path):
    p=tmp_path/'original.html'
    m.immutable_write(p,b'original')
    m.immutable_write(p,b'original')
    with pytest.raises(ValueError):m.immutable_write(p,b'changed')
    assert p.read_bytes()==b'original'


def test_mismatched_discovery_scope_rejected(config,tmp_path):
    with pytest.raises(ValueError,match='scope'):
        m.inventory(config,[tmp_path],{'scope_key':'wrong'})


def test_verified_discovery_missing_local_is_not_missing_shared(config,tmp_path):
    d={'scope_key':m.scope_key(config),'companies':{'AAPL':{'status':'discovered','filings':[
        {'accession_number':'0000320193-25-000079'}]}}}
    result=m.inventory(config,[tmp_path],d)
    aapl=result['companies'][0]
    assert aapl['sec_expected_count']==1
    assert aapl['not_in_supplied_archives']==['0000320193-25-000079']
    assert aapl['shared_database_count'] is None


def test_incomplete_database_window_rejected(config,tmp_path):
    database={'exported_at':'2026-10-28', 'export_window':{'basis':'filing_date','since':'2025-01-01','until':'2026-10-28'},
              'companies':[], 'filings':[]}
    with pytest.raises(ValueError,match='full configured window'):
        m.inventory(config,[tmp_path],database=database)


def test_explicit_missing_company_is_zero_not_unknown(config,tmp_path):
    database={'exported_at':'2026-10-28', 'export_window':{'basis':'filing_date','since':'2021-01-01','until':'2026-10-28'},
              'companies':[{'requested_cik':'0000320193','company_found':False}], 'filings':[]}
    result=m.inventory(config,[tmp_path],database=database)
    assert result['companies'][0]['shared_database_count']==0
    assert result['companies'][1]['shared_database_count'] is None


def test_duplicate_database_accessions_rejected(config,tmp_path):
    database={'exported_at':'2026-10-28', 'export_window':{'basis':'filing_date','since':'2021-01-01','until':'2026-10-28'},
              'companies':[], 'filings':[{'accession_number':'x'},{'accession_number':'x'}]}
    with pytest.raises(ValueError,match='duplicate accessions'):
        m.inventory(config,[tmp_path],database=database)


def test_real_archive_reuse_resume_and_corruption_rejected(config, tmp_path, monkeypatch):
    """Exercise the repository sample through the wrapper with HTTP forbidden."""
    accession = '0001193125-26-191507'
    assert m.seed_archives(config, [ROOT], tmp_path) == [accession]
    assert m.seed_archives(config, [ROOT], tmp_path) == []
    source = tmp_path / 'data/raw/sec' / accession / 'source_manifest.json'
    manifest = m.verify_filing(source, tmp_path)
    primary = next(r for r in manifest['files'] if r['role'] == 'main_inline_xbrl_filing')
    row = {**manifest, 'primary_document':primary['source_url'].rsplit('/', 1)[-1]}
    m.save_json(tmp_path / 'discovery.json', {
        'scope_key':m.scope_key(config), 'discovery_complete':False,
        'companies':{'MSFT':{'status':'discovered', 'filings':[row]}}})

    class NoNetwork:
        def __init__(self, *_): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get(self, *_): raise AssertionError('Existing archive triggered HTTP')

    monkeypatch.setattr(m, 'RecordedClient', NoNetwork)
    first = m.download(config, tmp_path, 1)
    assert first['filings'][accession]['status'] == 'verified'
    assert first['download_stage_complete'] is False
    second = m.download(config, tmp_path, 1)
    assert second['filings'] == first['filings']

    # A previously successful-looking checkpoint cannot hide corrupted bytes.
    second['download_stage_complete'] = True
    m.save_json(tmp_path / 'download_status.json', second)
    (tmp_path / primary['local_path']).write_bytes(b'corrupted')
    with pytest.raises(ValueError):
        m.download(config, tmp_path, 1)
    assert json.loads((tmp_path / 'download_status.json').read_text())['download_stage_complete'] is False


@pytest.mark.parametrize('has_exhibit', [True, False])
def test_exhibit_archive_receipt_roundtrip_and_review(filing, tmp_path, has_exhibit):
    index_bytes = ('<table>' + (index_row('EX-21.1', 'ex21.htm') if has_exhibit else '') + '</table>').encode()
    relative = Path('data/raw/sec') / filing.accession_number / 'index.html'
    m.immutable_write(tmp_path / relative, index_bytes)
    manifest = {**filing.to_dict(), 'index_document':{
        'local_path':relative.as_posix(), 'sha256':m.digest(index_bytes)},
        'files':[{'role':'main_inline_xbrl_filing',
                  'source_url':filing.archive_url + filing.primary_document}]}
    original_manifest = json.dumps(manifest, sort_keys=True)

    class ExhibitClient:
        calls = []
        def get(self, url):
            self.calls.append(url)
            data = b'<html><body>Example subsidiary</body></html>'
            return data, {'sha256':m.digest(data), 'size_bytes':len(data),
                          'retrieved_at_utc':'2026-10-28T00:00:00Z'}

    client = ExhibitClient()
    receipt = m.save_exhibits(client, manifest, tmp_path)
    assert receipt['status'] == ('downloaded' if has_exhibit else 'not_listed_in_index_review_required')
    assert client.calls == ([filing.archive_url + 'ex21.htm'] if has_exhibit else [])
    assert m.save_exhibits(client, manifest, tmp_path) == receipt
    assert len(client.calls) == int(has_exhibit)
    assert json.dumps(manifest, sort_keys=True) == original_manifest
    if has_exhibit:
        (tmp_path / receipt['files'][0]['local_path']).write_bytes(b'changed')
        with pytest.raises(ValueError, match='checksum'):
            m.save_exhibits(client, manifest, tmp_path)
