"""Registrant facts are recognised under every form the DEI namespace takes."""

import pytest

from sec_pipeline import tables as t
from sec_pipeline.dei_namespaces import is_dei_concept, is_dei_namespace
from sec_pipeline.registrants import RegistrantBindingError
from tests import test_joint_statements as joint
from tests.test_registrants import HTML, XML, check_fixture, joint_fixture


VERSIONS = ('2026', '2021q4', '2020-01-31')
NOT_DEI = ('2021q5', '21', '2021-q4', '2021q', '2020-01', '')


@pytest.mark.parametrize('version', VERSIONS)
def test_every_release_form_is_a_dei_namespace(version):
    namespace = 'http://xbrl.sec.gov/dei/' + version
    assert is_dei_namespace(namespace) and is_dei_namespace(namespace.replace('http://', 'https://'))
    assert is_dei_concept('{' + namespace + '}EntityCentralIndexKey')
    assert is_dei_concept('{' + namespace + '}LegalEntityAxis', 'LegalEntityAxis')
    assert not is_dei_concept('{' + namespace + '}EntityCentralIndexKey', 'LegalEntityAxis')


@pytest.mark.parametrize('namespace', ['http://xbrl.sec.gov/dei/' + version for version in NOT_DEI] + [
    'http://xbrl.sec.gov/ecd/2023', 'http://example.test/xbrl.sec.gov/dei/2023', 'http://xbrl.sec.gov/dei/2023/extra', None])
def test_other_namespaces_are_not_dei(namespace):
    assert not is_dei_namespace(namespace)
    assert not is_dei_concept('{' + str(namespace) + '}EntityCentralIndexKey')


def renamed(version):
    """The joint-statement fixture with its DEI facts and axis on another release of the taxonomy."""
    tree, facts = joint.fixture()
    namespace = 'http://xbrl.sec.gov/dei/' + version
    tree.getroot().set('xmlns:dei', namespace)
    for fact in facts.values():
        fact['concept'] = fact['concept'].replace('{' + joint.DEI + '}', '{' + namespace + '}')
        fact['dimensions'] = [{**dimension, 'dimension': dimension['dimension'].replace('{' + joint.DEI + '}', '{' + namespace + '}')}
                              for dimension in fact['dimensions']]
    return tree, facts


@pytest.mark.parametrize('version', VERSIONS)
def test_a_combined_filing_is_selected_as_one_under_every_release_form(version):
    tree, facts = renamed(version)
    selected = t.select_statement_segments(tree, facts)
    assert [segment.element.get('id') for segment in selected] == ['1-income_statement', '1-balance_sheet', '1-cash_flow']
    assert all(segment.group['joint_statement']['registrant_cik'] == '0000000001' for segment in selected)
    assert all(set(segment.group['joint_statement']['dei_xml_ids']) == {
        'EntityCentralIndexKey', 'EntityRegistrantName', 'DocumentType', 'DocumentPeriodEndDate'} for segment in selected)


@pytest.mark.parametrize('version', VERSIONS)
def test_a_combined_filing_download_is_verified_under_every_release_form(version):
    manifest, discovered, payloads = joint_fixture()
    for role in (XML, HTML):
        assert b'http://xbrl.sec.gov/dei/2025' in payloads[role]
        payloads[role] = payloads[role].replace(b'http://xbrl.sec.gov/dei/2025', b'http://xbrl.sec.gov/dei/' + version.encode())
    assert check_fixture(manifest, discovered, payloads)


@pytest.mark.parametrize('version', ['2021q5', '2021-q4'])
def test_a_namespace_that_is_no_dei_release_still_identifies_no_registrant(version):
    manifest, discovered, payloads = joint_fixture()
    for role in (XML, HTML):
        payloads[role] = payloads[role].replace(b'http://xbrl.sec.gov/dei/2025', b'http://xbrl.sec.gov/dei/' + version.encode())
    with pytest.raises(RegistrantBindingError):
        check_fixture(manifest, discovered, payloads)
