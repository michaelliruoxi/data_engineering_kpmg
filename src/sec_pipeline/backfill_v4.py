"""Verify additive source-set receipts in the actual receiving database."""

from copy import deepcopy


def stored_receipt(conn, accession, set_key, expected, required_checks):
    """Return current sealed metadata without retaining an older queue result."""
    with conn.transaction():
        row = conn.execute("""SELECT s.configuration,s.versions,s.source_checks,s.semantic_checks,
            s.validation_status,s.source_validation_status,s.counts,d.manifest_sha256,
            s.contract_version,s.filing_id,s.id,s.document_set_id,s.coverage
            FROM sec.source_processing_sets s
            JOIN sec.source_document_sets d ON d.id=s.document_set_id AND d.filing_id=s.filing_id
            JOIN sec.filings f ON f.id=s.filing_id
            WHERE f.accession_number=%s AND s.set_key=%s""", (accession, set_key)).fetchone()
        if not row:
            return None
        configuration, versions, checks, semantic, status, source_status, counts, digest, contract, filing, selection_id, document_set_id, coverage = row
        if (configuration != expected or versions != expected['versions'] or contract != 2
                or digest != expected['document_set_manifest_sha256'] or source_status != 'passed'
                or status not in {'passed', 'review_required'}
                or not isinstance(checks, dict) or not isinstance(semantic, dict) or not isinstance(counts, dict)
                or not all(checks.get(key) is True for key in required_checks)
                or any(value is not True for value in checks.values())
                or not all(type(counts.get(key)) is int and counts[key] > 0
                           for key in ('facts', 'tables', 'reports', 'chunks', 'inline_documents'))
                or type(counts.get('unaccounted_cells')) is not int or counts['unaccounted_cells'] != 0
                or type(counts.get('quality_issues')) is not int or counts['quality_issues'] < 0
                or type(counts.get('review_required_cells')) is not int or counts['review_required_cells'] < 0
                or not isinstance(coverage, dict) or set(coverage) != set(versions)
                or any(value not in ({'unknown', 'partial', 'complete'} if key == 'reports' else {'partial', 'complete'})
                       for key, value in coverage.items())):
            return None
        review = counts['quality_issues'] > 0
        if (review != (status == 'review_required') or review != (counts['review_required_cells'] > 0)
                or semantic.get('period_header_consistency') is not (not review)
                or any(type(value) is not bool or (value is False and key != 'period_header_consistency')
                       for key, value in semantic.items())):
            return None
        expected_documents = expected.get('source_documents')
        if (not isinstance(expected_documents, dict) or set(expected_documents) != set(expected['source_hashes'])
                or any(not isinstance(record, dict) or set(record) != {'role', 'source_url', 'sha256', 'size_bytes'}
                       or record['sha256'] != expected['source_hashes'][path]
                       or not isinstance(record['role'], str) or not record['role']
                       or not isinstance(record['source_url'], str) or not record['source_url']
                       or type(record['size_bytes']) is not int or record['size_bytes'] < 0
                       for path, record in expected_documents.items())):
            return None
        documents = conn.execute('SELECT local_path,role,source_url,sha256,size_bytes FROM sec.source_documents WHERE filing_id=%s',
                                 (filing,)).fetchall()
        actual = {path: {'role': role, 'source_url': url, 'sha256': sha, 'size_bytes': size}
                  for path, role, url, sha, size in documents}
        if len(actual) != len(documents) or any(actual.get(path) != record for path, record in expected_documents.items()):
            return None
        return {'mode': 'stored_receipt', 'accession_number': accession, 'versions': deepcopy(versions),
                'document_set_manifest_sha256': digest, 'source_hashes': deepcopy(expected['source_hashes']),
                'source_documents': deepcopy(expected_documents),
                'configuration': deepcopy(configuration), 'counts': deepcopy(counts), 'coverage': deepcopy(coverage),
                'source_validation_status': source_status, 'validation_status': status,
                'source_checks': deepcopy(checks), 'semantic_checks': deepcopy(semantic),
                'validation': {'passed': status == 'passed', 'required_checks': list(required_checks), 'checks': deepcopy(checks)},
                'processing_set_id': str(selection_id), 'document_set_id': str(document_set_id), 'filing_id': str(filing),
                'contract_version': contract, 'preferred': False}


def stored_status(conn, accession, set_key, expected, required_checks):
    """Compatibility view of the same fully validated receiving-database receipt."""
    receipt = stored_receipt(conn, accession, set_key, expected, required_checks)
    return receipt['validation_status'] if receipt is not None else None
