"""Validate, publish or withdraw the reviewed sets a queue ingested, one filing at a time.

Every filing goes through the existing single-filing review command, so the
stored-output audit, the expected-selection check, the evidence written before
commit and the exact rollback are unchanged. This command only supplies the
accessions and processing-set IDs from the queue and keeps a batch record.

  plan      Read-only. Audit each ingested corrected/reviewed set against the
            receiving database and its original sources, and record the
            selection each filing serves now. Writes one evidence file per
            filing and plan.json, and prints the plan's SHA-256.
  publish   Publish the filings of a reviewed plan.json that passed, each only
            if it still serves the selection recorded in the plan. The plan's
            SHA-256 is required: what is published is what was reviewed.
  rollback  Withdraw the filings a publish run switched, restoring each exact
            earlier selection.

Publication changes the finance SQL selection only. External retrieval indexes
are not refreshed. Exit 0: every selected filing reached the requested state.
Exit 2: some did not; see the summary. Exit 3: stopped on an unconfirmed commit
or an infrastructure error; inspect the database before continuing. Exit 1: setup.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import UUID

import review_section_upgrade as review
from process_filing import ingestion_settings
from sec_pipeline.backfill import _fatal_runtime_error
from sec_pipeline.database import DataConflict


REVIEWED_TABLES = frozenset({'tables-v5', 'tables-v6'})
ACCESSION = re.compile(r'\d{10}-\d{2}-\d{6}')
PLAN_KIND, SUMMARY_KIND = 'reviewed_publication_plan', 'reviewed_publication_summary'


class Stop(Exception):
    """End the batch now and keep what was recorded; never guess an outcome."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as output:
        output.write(json.dumps(value, indent=2) + '\n')


def _destination(args):
    settings = ingestion_settings(args.env_file, args.database)
    return {key: settings[key] for key in ('host', 'port', 'dbname')}


def _target(args):
    return ['--env-file', str(args.env_file), '--database', args.database]


def eligibility(entry, destination):
    """Why a queue entry is not a publication candidate, or None when it is."""
    if entry.get('receipt_status') == 'stale_receipt' or entry.get('status') == 'stale_receipt':
        return 'stale_receipt'
    if entry.get('status') == 'ingested_review_required':
        return 'review_required'
    if entry.get('status') != 'ingested':
        return 'not_ingested'
    if entry.get('processing_versions', {}).get('tables') not in REVIEWED_TABLES:
        return 'not_a_reviewed_profile'
    try:
        UUID(entry.get('result', {}).get('processing_set_id'))
    except (TypeError, ValueError, AttributeError):
        return 'no_processing_set_id'
    if entry.get('destination') != destination:
        return 'ingested_into_another_database'
    return None


def _review(arguments):
    """Run one single-filing review. Returns (exit code, error); stops on unsafe errors."""
    try:
        return review.main(arguments), None
    except DataConflict as error:
        return None, error
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt) or _fatal_runtime_error(error) or not isinstance(error, Exception):
            raise Stop(type(error).__name__) from error
        return None, error


def _evidence(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def plan(args):
    destination = _destination(args)
    queue = json.loads(args.state.read_text(encoding='utf-8'))
    wanted = set(args.accession or ())
    unknown = wanted - set(queue.get('filings', {}))
    if unknown:
        raise ValueError('Requested accession is absent from the queue.')
    skipped, candidates = Counter(), []
    for accession in sorted(queue.get('filings', {})):
        entry = queue['filings'][accession]
        if wanted and accession not in wanted:
            continue
        reason = 'invalid_accession' if not ACCESSION.fullmatch(accession) else eligibility(entry, destination)
        if reason:
            skipped[reason] += 1
        else:
            candidates.append((accession, str(UUID(entry['result']['processing_set_id']))))
    deferred = len(candidates[args.limit:]) if args.limit else 0
    rows, stopped = [], None
    for accession, set_id in candidates[:args.limit] if args.limit else candidates:
        evidence = args.output_dir / 'validate' / f'{accession}.json'
        row = {'accession_number': accession, 'processing_set_id': set_id,
               'evidence': evidence.relative_to(args.output_dir).as_posix()}
        try:
            code, error = _review(['validate', '--accession', accession, '--processing-set-id', set_id,
                                   *_target(args), '--data-root', str(args.data_root), '--output', str(evidence)])
        except Stop as stop:
            stopped = str(stop)
            break
        recorded = _evidence(evidence)
        before = recorded.get('served_selection_before')
        if error is not None or code not in (0, 2) or not isinstance(before, list) or len(before) != 2:
            row.update(validation='error', action='none', error_type=type(error).__name__ if error else 'UnreadableEvidence')
        else:
            row['served_selection_before'] = before
            row['currently_served'] = before == ['source', set_id]
            row['validation'] = 'passed' if code == 0 and recorded.get('status') == 'passed' else 'failed'
            if row['validation'] == 'failed':
                row['failed_checks'] = sorted({item.get('check') for item in recorded.get('errors', [])
                                              if isinstance(item, dict) and item.get('check')})
            row['action'] = ('none' if row['validation'] != 'passed'
                             else 'already_published' if row['currently_served'] else 'publish')
        rows.append(row)
    result = {'schema_version': 1, 'kind': PLAN_KIND, 'created_at_utc': _now(),
              'scope': 'stored_output_audit_not_owner_approval_or_index_refresh',
              'queue': {'path': str(args.state), 'sha256': _sha256(args.state)},
              'destination': destination, 'complete': stopped is None and not deferred,
              'stopped_reason': stopped, 'deferred_by_limit': deferred,
              'counts': dict(sorted(Counter(row['action'] if row['validation'] == 'passed' else row['validation']
                                            for row in rows).items())),
              # A set that is served now and no longer passes needs review; this
              # command never withdraws it on its own.
              'served_sets_failing_audit': [row['accession_number'] for row in rows
                                            if row.get('currently_served') and row['validation'] != 'passed'],
              'skipped_queue_entries': dict(sorted(skipped.items())), 'filings': rows}
    path = args.output_dir / 'plan.json'
    _write_new(path, result)
    print(json.dumps({'plan': str(path), 'plan_sha256': _sha256(path), 'complete': result['complete'],
                      'counts': result['counts'], 'skipped_queue_entries': result['skipped_queue_entries'],
                      'deferred_by_limit': deferred, 'stopped_reason': stopped}, indent=2))
    if stopped:
        return 3
    return 0 if rows and all(row['validation'] == 'passed' for row in rows) else 2


def _load(path, kind, digest=None):
    """Parse exactly the bytes that were hashed; return the file and its digest."""
    content = Path(path).read_bytes()
    actual = hashlib.sha256(content).hexdigest()
    if digest is not None and actual != digest.lower():
        raise ValueError('The file differs from the reviewed SHA-256; nothing was changed.')
    try:
        value = json.loads(content.decode('utf-8'))
    except ValueError:
        value = None
    rows = value.get('filings') if isinstance(value, dict) else None
    if (value is None or value.get('schema_version') != 1 or value.get('kind') != kind or not isinstance(rows, list)
            or any(not isinstance(row, dict) or not isinstance(row.get('accession_number'), str)
                   or not ACCESSION.fullmatch(row['accession_number']) for row in rows)):
        raise ValueError('Unsupported batch file.')
    return value, actual


def _set_id(row):
    try:
        return str(UUID(row['processing_set_id']))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError('Unsupported batch file.') from None


def _same_destination(recorded, args):
    if recorded.get('destination') != _destination(args):
        raise ValueError('The batch file was produced for a different receiving database.')


def _finish(args, name, summary, outcomes, stopped, wanted, deferred=0):
    summary.update(counts=dict(sorted(Counter(row['outcome'] for row in outcomes).items())),
                   complete=stopped is None and not deferred, stopped_reason=stopped,
                   deferred_by_limit=deferred, filings=outcomes)
    path = args.output_dir / name
    _write_new(path, summary)
    print(json.dumps({'summary': str(path), 'counts': summary['counts'], 'stopped_reason': stopped}, indent=2))
    if stopped:
        return 3
    return 0 if outcomes and all(row['outcome'] in wanted for row in outcomes) else 2


def publish(args):
    reviewed, plan_sha256 = _load(args.plan, PLAN_KIND, args.plan_sha256)
    _same_destination(reviewed, args)
    selected = [row for row in reviewed['filings'] if row.get('action') == 'publish']
    deferred = len(selected[args.limit:]) if args.limit else 0
    if args.limit:
        selected = selected[:args.limit]
    outcomes, stopped = [], None
    for row in selected:
        before = row.get('served_selection_before')
        if (not isinstance(before, list) or len(before) != 2 or before[0] not in ('legacy', 'source', None)
                or (before[0] is None) != (before[1] is None)):
            raise ValueError('Unsupported batch file.')
        _set_id(row)
    for row in selected:
        accession, set_id = row['accession_number'], _set_id(row)
        family, previous = row['served_selection_before']
        evidence = args.output_dir / 'publish' / f'{accession}.json'
        outcome = {'accession_number': accession, 'processing_set_id': set_id,
                   'served_selection_before': [family, previous],
                   'evidence': evidence.relative_to(args.output_dir).as_posix()}
        arguments = ['publish', '--accession', accession, '--processing-set-id', set_id,
                     '--expected-family', family or 'none', *_target(args),
                     '--data-root', str(args.data_root), '--output', str(evidence)]
        if family is not None:
            arguments += ['--expected-set-id', previous]
        try:
            code, error = _review(arguments)
        except Stop as stop:
            code, error, stopped = None, None, str(stop)
        recorded = _evidence(evidence)
        if code == 3:
            # The review command confirmed the commit and could not finish its file.
            outcome['outcome'], stopped = 'published_evidence_incomplete', 'evidence_write_failed_after_commit'
        elif recorded.get('status') == 'outcome_unknown' or recorded.get('transaction_status') in {'prepared', 'unknown'}:
            # The commit was sent and not acknowledged. Do not touch another filing.
            outcome['outcome'], stopped = 'outcome_unknown', stopped or 'commit_not_acknowledged'
        elif code == 0 and recorded.get('transaction_status') == 'committed':
            outcome['outcome'] = 'published'
        elif code == 2:
            outcome['outcome'] = 'validation_failed'
        elif isinstance(error, DataConflict):
            # The filing no longer serves the reviewed selection, or readers did
            # not select the set; the review command applied nothing.
            outcome['outcome'] = 'selection_conflict'
        elif isinstance(error, FileExistsError):
            outcome['outcome'] = 'evidence_file_exists'
        elif stopped:
            # Evidence is written inside the transaction before its commit, so a
            # stop that left no prepared evidence never reached the commit.
            outcome['outcome'] = 'stopped_before_commit'
        else:
            outcome.update(outcome='error', error_type=type(error).__name__ if error else 'UnconfirmedResult')
        outcomes.append(outcome)
        if stopped:
            break
    summary = {'schema_version': 1, 'kind': SUMMARY_KIND, 'created_at_utc': _now(),
               'plan': {'path': str(args.plan), 'sha256': plan_sha256},
               'destination': reviewed['destination'],
               'external_retrieval_index_status': 'not_refreshed'}
    return _finish(args, 'publish-summary.json', summary, outcomes, stopped, {'published'}, deferred)


def rollback(args):
    published, published_sha256 = _load(args.published, SUMMARY_KIND)
    _same_destination(published, args)
    wanted = set(args.accession or ())
    selected = [row for row in published['filings'] if row.get('outcome') == 'published'
                and (not wanted or row['accession_number'] in wanted)]
    if wanted - {row['accession_number'] for row in selected}:
        raise ValueError('Requested accession was not published by that run.')
    outcomes, stopped = [], None
    for row in selected:
        accession, set_id = row['accession_number'], _set_id(row)
        evidence = args.output_dir / 'rollback' / f'{accession}.json'
        outcome = {'accession_number': accession, 'processing_set_id': set_id,
                   'evidence': evidence.relative_to(args.output_dir).as_posix()}
        try:
            code, error = _review(['rollback', '--accession', accession, '--processing-set-id', set_id,
                                   *_target(args), '--output', str(evidence)])
        except Stop as stop:
            code, error, stopped = None, None, str(stop)
        recorded = _evidence(evidence)
        if code == 3:
            outcome['outcome'], stopped = 'rolled_back_evidence_incomplete', 'evidence_write_failed_after_commit'
        elif recorded.get('status') == 'outcome_unknown' or recorded.get('transaction_status') in {'prepared', 'unknown'}:
            outcome['outcome'], stopped = 'outcome_unknown', stopped or 'commit_not_acknowledged'
        elif code == 0 and recorded.get('transaction_status') == 'committed':
            outcome.update(outcome='rolled_back', served_selection_after=recorded.get('served_selection_after'))
        elif isinstance(error, DataConflict):
            outcome['outcome'] = 'not_current_or_baseline_changed'
        elif isinstance(error, FileExistsError):
            outcome['outcome'] = 'evidence_file_exists'
        elif stopped:
            # Evidence is written inside the transaction before its commit, so a
            # stop that left no prepared evidence never reached the commit.
            outcome['outcome'] = 'stopped_before_commit'
        else:
            outcome.update(outcome='error', error_type=type(error).__name__ if error else 'UnconfirmedResult')
        outcomes.append(outcome)
        if stopped:
            break
    summary = {'schema_version': 1, 'kind': 'reviewed_rollback_summary', 'created_at_utc': _now(),
               'published': {'path': str(args.published), 'sha256': published_sha256},
               'destination': published['destination'],
               'external_retrieval_index_status': 'not_refreshed'}
    return _finish(args, 'rollback-summary.json', summary, outcomes, stopped, {'rolled_back'})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest='action', required=True)
    commands = {}
    for name in ('plan', 'publish', 'rollback'):
        command = commands[name] = actions.add_parser(name)
        command.add_argument('--env-file', type=Path, required=True)
        command.add_argument('--database', required=True)
        command.add_argument('--output-dir', type=Path, required=True,
                             help='Directory for this run only; earlier evidence is never replaced.')
    for name in ('plan', 'publish'):
        commands[name].add_argument('--data-root', type=Path, default=Path('.'))
        commands[name].add_argument('--limit', type=int, help='Process at most this many filings.')
    for name in ('plan', 'rollback'):
        commands[name].add_argument('--accession', action='append', help='Restrict to this accession; repeatable.')
    commands['plan'].add_argument('--state', type=Path, required=True, help='Queue whose ingested reviewed sets to audit.')
    commands['publish'].add_argument('--plan', type=Path, required=True)
    commands['publish'].add_argument('--plan-sha256', required=True, help='SHA-256 printed by the reviewed plan run.')
    commands['rollback'].add_argument('--published', type=Path, required=True, help='publish-summary.json of the run to withdraw.')
    args = parser.parse_args(argv)
    if getattr(args, 'limit', None) is not None and args.limit < 1:
        parser.error('--limit must be a positive integer.')
    record = {'plan': 'plan.json', 'publish': 'publish-summary.json', 'rollback': 'rollback-summary.json'}[args.action]
    if (args.output_dir / record).exists():
        parser.error(f'{record} already exists in --output-dir; use a new directory for each run.')
    try:
        return {'plan': plan, 'publish': publish, 'rollback': rollback}[args.action](args)
    except Exception as error:
        # Never echo raw connection exceptions or private configuration.
        message = str(error) if type(error) is ValueError else type(error).__name__
        print(json.dumps({'status': 'stopped', 'error': message}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
