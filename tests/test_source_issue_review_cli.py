"""Offline review output must retain source conflicts and protect source files."""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('source_issue_review_cli', ROOT / 'scripts/review_filing_source_issues.py')
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


@pytest.mark.parametrize('filename', ['original.htm', 'original.xml'])
def test_cli_refuses_to_replace_original_sources(tmp_path, filename):
    source = tmp_path / filename
    source.write_text('Original source bytes')
    with pytest.raises(SystemExit) as error:
        review.main(['--html', str(tmp_path / 'original.htm'), '--xml', str(tmp_path / 'original.xml'),
                     '--output', str(source)])
    assert error.value.code == 2
    assert source.read_text() == 'Original source bytes'


@pytest.mark.parametrize('filename', ['original.htm', 'original.xml'])
def test_cli_refuses_output_hard_linked_to_original_source(tmp_path, filename, monkeypatch):
    sources = [tmp_path / 'original.htm', tmp_path / 'original.xml']
    for source in sources:
        source.write_text('Original source bytes: ' + source.name)
    output = tmp_path / 'review.json'
    output.hardlink_to(tmp_path / filename)
    monkeypatch.setattr(review, 'review_sources', lambda *_: pytest.fail('Original-source alias must be rejected before review.'))
    with pytest.raises(SystemExit) as error:
        review.main(['--html', str(sources[0]), '--xml', str(sources[1]), '--output', str(output)])
    assert error.value.code == 2
    assert output.samefile(tmp_path / filename)
    for source in sources:
        assert source.read_text() == 'Original source bytes: ' + source.name


def test_unrelated_html_cannot_produce_a_clean_review(tmp_path, monkeypatch):
    unrelated = tmp_path / 'unrelated.htm'
    unrelated.write_text('<html><body>Not the original filing.</body></html>')
    xml = ROOT / 'data/raw/sec/0001193125-26-191507/msft-20260331_htm.xml'
    def no_tables_before_correspondence(*args, **kwargs):
        raise AssertionError('Unverified sources must not reach table reconstruction')
    monkeypatch.setattr(review, 'extract_tables', no_tables_before_correspondence)
    result = review.review_sources(unrelated, xml)
    assert result['status'] == 'failed'
    assert result['reason'] == 'original_html_xml_correspondence_failed'


def test_committed_microsoft_originals_are_a_clean_control():
    directory = ROOT / 'data/raw/sec/0001193125-26-191507'
    result = review.review_sources(directory / 'msft-20260331.htm', directory / 'msft-20260331_htm.xml')
    assert result['status'] == 'no_issue_found_by_bounded_checks'
    assert result['inline_correspondence']['matched'] == 1622
    assert result['tables_reconstructed'] == 3
    assert result['cash_period_issues'] == []
    assert result['corrections_applied'] is False
    assert result['database_checked'] is False
