"""Durable per-accession queue for a fixed membership snapshot and date window."""

from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
import time
from urllib.parse import urljoin

from .filing_sources import verify_filing
from .parser_policy import policy_metadata
from .universe import load_snapshot


def _fatal_runtime_error(error):
    """Infrastructure or storage-policy failures cannot use a content fallback."""
    import errno
    import httpx
    import psycopg
    from .processing import ParserRevisionConflict

    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, ParserRevisionConflict):
            return True
        state = getattr(error, 'sqlstate', None)
        if state and state[:2] in {'08', '28', '3D', '3F', '42', '53', '57', '58', 'XX'}:
            return True
        if isinstance(error, (ImportError, httpx.TransportError, psycopg.OperationalError, psycopg.InterfaceError)):
            return True
        if isinstance(error, OSError) and error.errno in {errno.EACCES, errno.EPERM, errno.ENOSPC, errno.EROFS}:
            return True
        error = error.__cause__ or error.__context__
    return False


def _failure_detail(error, phase):
    """Describe a failure with pipeline-owned messages and SQLSTATE codes only.

    Database and HTTP exception text can contain credentials or configuration,
    so only exceptions defined by this package contribute their message.
    """
    import psycopg

    detail = {"stage": getattr(error, "stage", phase), "error_type": type(error).__name__}
    if getattr(error, "failed_checks", None):
        detail["failed_checks"] = list(error.failed_checks)
    cause, seen = error, {id(error)}
    while cause.__cause__ is not None and id(cause.__cause__) not in seen:
        cause = cause.__cause__
        seen.add(id(cause))
    if cause is not error:
        detail["cause_type"] = type(cause).__name__
    if isinstance(cause, psycopg.Error):
        detail["sqlstate"] = cause.sqlstate
    elif type(cause).__module__.startswith("sec_pipeline."):
        detail["message"] = str(cause)[:300]
    return detail


def save_state(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        json.dump(value, output, indent=2)
        output.write("\n")
        temporary = Path(output.name)
    try:
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (2 ** attempt))
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def queue_lock(path):
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ValueError("This queue is locked. Confirm the previous process stopped before removing its .lock file.") from exc
    try:
        with handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        lock.unlink()


def open_state(path, universe_path, since, until, *, issuer_history_path=None, allow_history_extension=False):
    since, until = date.fromisoformat(str(since)), date.fromisoformat(str(until))
    if since > until:
        raise ValueError("The start date must not follow the end date.")
    universe_path = Path(universe_path)
    universe = load_snapshot(universe_path)
    config = {"universe_sha256": hashlib.sha256(universe_path.read_bytes()).hexdigest(),
              "since": since.isoformat(), "until": until.isoformat(), "forms": ["10-K", "10-Q"]}
    path = Path(path)
    existed = path.exists()
    state = json.loads(path.read_text(encoding="utf-8")) if existed else None
    saved_history = (state or {}).get("issuer_history", {})
    history_path = issuer_history_path or saved_history.get("path")
    history = None
    if history_path:
        from .issuer_history import load_history, history_jobs
        history = load_history(Path(history_path), universe)
        history_config = {**config, "issuer_history_sha256": history["source_sha256"]}
        previous_config = (state or {}).get("configuration")
        if existed and previous_config != history_config:
            if not allow_history_extension:
                raise ValueError("Adding issuer history requires an explicit discovery run.")
            base_config = {key: value for key, value in previous_config.items() if key != "issuer_history_sha256"}
            if base_config != config:
                raise ValueError("Queue configuration changed; use a new queue file.")
            if previous_config.get("issuer_history_sha256"):
                # A new reviewed file may add relationships, but cannot rewrite
                # or remove previously approved evidence or succession dates.
                previous = load_history(Path(saved_history["path"]), universe)
                if previous["source_sha256"] != previous_config["issuer_history_sha256"]:
                    raise ValueError("The previous issuer-history source changed; restore it before extending the queue.")
                if (len(history["records"]) <= len(previous["records"])
                        or any(record not in history["records"] for record in previous["records"])):
                    raise ValueError("Issuer history extensions must add a new relationship and preserve every existing relationship.")
            state["configuration"] = history_config
            state.setdefault("historical_issuers", {})
            invalidate_completion(state, "issuer_history_extended")
        if not existed or allow_history_extension:
            if state is not None:
                state["issuer_history"] = {"path": str(Path(history_path).resolve()),
                                           "source_sha256": history["source_sha256"]}
        config = history_config
        universe = {**universe, "issuer_history": history, "history_jobs": history_jobs(history, since, until)}
    if existed:
        if state.get("schema_version") != 1 or state.get("configuration") != config:
            raise ValueError("Queue configuration changed; use a new queue file.")
    else:
        state = {"schema_version": 1, "configuration": config,
                 "created_at_utc": datetime.now(timezone.utc).isoformat(),
                 "companies": {}, "filings": {}}
        if history is not None:
            state["issuer_history"] = {"path": str(Path(history_path).resolve()), "source_sha256": history["source_sha256"]}
            state["historical_issuers"] = {}
    if not existed or (history is not None and allow_history_extension):
        save_state(path, state)
    return state, universe


def invalidate_completion(state, reason):
    if "last_run" in state:
        state["last_run"].update(complete=False, invalidated_by=reason)


def refresh_discovery(path, state):
    """Start a resumable fresh submissions pass without discarding filing work."""
    for bucket in ("companies", "historical_issuers"):
        for record in state.get(bucket, {}).values():
            record["status"] = "pending"
    state["discovery_refresh_started_at_utc"] = datetime.now(timezone.utc).isoformat()
    invalidate_completion(state, "discovery_refresh")
    save_state(path, state)


def remember_discovery(state, filing):
    """Keep one accession while retaining independently verifiable filer bindings."""
    accession = filing["accession_number"]
    entry = state["filings"].get(accession)
    if entry is None:
        state["filings"][accession] = {"filing": dict(filing), "status": "pending", "attempts": 0}
        invalidate_completion(state, "new_filing")
        return
    previous = entry["filing"]
    identity_fields = ("accession_number", "form", "filing_date", "reporting_period_end", "primary_document")
    if any(previous.get(key) != filing.get(key) for key in identity_fields):
        raise ValueError("The same accession was discovered with conflicting filing identity.")
    if previous["cik"] == filing["cik"]:
        if previous.get("filing_index_url") != filing.get("filing_index_url"):
            raise ValueError("The same filing identity has conflicting index URLs.")
        return
    from .acquisition import FilingIdentity
    for candidate in (previous, filing):
        identity = FilingIdentity(**{name: candidate[name] for name in FilingIdentity.__dataclass_fields__})
        if candidate.get("filing_index_url") != identity.filing_index_url:
            raise ValueError("Discovered index URL disagrees with its registrant.")
    bindings = entry.setdefault("discovery_bindings", [dict(previous)])
    if all(binding["cik"] != filing["cik"] for binding in bindings):
        bindings.append(dict(filing))
        # A new binding must be verified against the saved index and DEI facts.
        entry["status"] = "pending"
        invalidate_completion(state, "new_registrant_binding")


def discover_queue(path, state, universe, client, *, company_limit=None, emit=print):
    from .acquisition import AccessBlocked, discover_filings

    attempted = 0
    ordered_companies = sorted(universe["companies"], key=lambda c:
        state["companies"].get(c["cik"], {}).get("status") in {"failed", "access_blocked"})
    for company in ordered_companies:
        cik = company["cik"]
        if state["companies"].get(cik, {}).get("status") == "discovered":
            continue
        if company_limit is not None and attempted >= company_limit:
            break
        attempted += 1
        try:
            filings = discover_filings(client, cik, state["configuration"]["since"], state["configuration"]["until"])
            for filing in filings:
                remember_discovery(state, filing)
            state["companies"][cik] = {"status": "discovered", "filing_count": len(filings), "tickers": company["tickers"]}
            emit(json.dumps({"event": "discovered", "cik": cik, "filings": len(filings)}))
        except AccessBlocked:
            state["companies"][cik] = {"status": "access_blocked"}
            save_state(path, state)
            raise
        except Exception as exc:
            state["companies"][cik] = {"status": "failed", "error_type": type(exc).__name__}
            emit(json.dumps({"event": "discovery_failed", "cik": cik, "error_type": type(exc).__name__}))
        save_state(path, state)
    if universe.get("history_jobs"):
        histories = state.setdefault("historical_issuers", {})
        jobs = sorted(universe["history_jobs"], key=lambda job:
            histories.get(job["job_key"], {}).get("status") in {"failed", "access_blocked"})
        for job in jobs:
            key = job["job_key"]
            if histories.get(key, {}).get("status") == "discovered":
                continue
            if company_limit is not None and attempted >= company_limit:
                break
            attempted += 1
            try:
                filings = discover_filings(client, job["cik"], job["since"], job["until"])
                for filing in filings:
                    remember_discovery(state, filing)
                histories[key] = {"status": "discovered", "filing_count": len(filings),
                                  "cik": job["cik"], "current_ciks": job["current_ciks"],
                                  "since": job["since"], "until": job["until"]}
                emit(json.dumps({"event": "predecessor_discovered", "cik": job["cik"], "filings": len(filings)}))
            except AccessBlocked:
                histories[key] = {"status": "access_blocked", "cik": job["cik"]}
                save_state(path, state)
                raise
            except Exception as exc:
                histories[key] = {"status": "failed", "cik": job["cik"], "error_type": type(exc).__name__}
                emit(json.dumps({"event": "predecessor_discovery_failed", "cik": job["cik"], "error_type": type(exc).__name__}))
            save_state(path, state)
    return summarize(state, universe)


def summarize(state, universe):
    return {"membership_as_of": universe.get("as_of"), "membership_source": universe.get("source_url"),
            "companies_expected": len(universe["companies"]),
            "companies": dict(Counter(c["status"] for c in state["companies"].values())),
            "predecessor_jobs_expected": len(universe.get("history_jobs", [])),
            "predecessor_jobs": dict(Counter(job["status"] for job in state.get("historical_issuers", {}).values())),
            "filings_discovered": len(state["filings"]),
            "filings": dict(Counter(f["status"] for f in state["filings"].values())),
            "last_run": state.get("last_run")}


def adapter_profile(tables_version):
    if tables_version == "tables-v1":
        from . import filing_adapter
        return filing_adapter, "filing-import-v1"
    if tables_version == "tables-v2":
        from . import filing_adapter_v2
        return filing_adapter_v2, "filing-import-v2"
    if tables_version == "tables-v3":
        from . import filing_adapter_v3
        return filing_adapter_v3, "filing-import-v3"
    if tables_version == "tables-v4":
        from . import filing_ingestion_v4
        return filing_ingestion_v4, "filing-import-v4"
    raise ValueError("Unsupported table extraction version.")


def text_fallback_profile(tables_version):
    """The profile that loads facts and text when this version's tables fail, if any."""
    if tables_version == "tables-v3":
        from . import filing_adapter_v3_text
        return filing_adapter_v3_text
    return None


def _stored_text_set(conn, accession, text_profile, expected):
    """Return the recorded table failure of a sealed text-only set matching these sources."""
    with conn.transaction():
        row = conn.execute("""SELECT s.configuration, s.validation_status, s.counts, s.coverage, s.validation,
            fd.local_path, fd.sha256, rd.local_path, rd.sha256, s.fact_extraction_version,
            r.extraction_version, s.chunking_version, s.table_source_document_id, s.table_extraction_version
            FROM sec.filing_processing_sets s JOIN sec.filings f ON f.id=s.filing_id
            JOIN sec.source_documents fd ON fd.id=s.fact_source_document_id
            JOIN sec.reports r ON r.id=s.report_id
            JOIN sec.source_documents rd ON rd.id=r.source_document_id
            WHERE f.accession_number=%s AND s.set_key=%s
              AND fd.role='extracted_xbrl_instance' AND rd.role='main_inline_xbrl_filing'""",
            (accession, text_profile.SET_KEY)).fetchone()
    if not row:
        return None
    (configuration, status, counts, coverage, validation, fact_path, fact_sha, report_path, report_sha,
     fact_version, report_version, chunk_version, table_document, table_version) = row
    unavailable = configuration.get("tables_unavailable")
    versions = expected["versions"]
    checks = validation.get("checks", {})
    if ({key: value for key, value in configuration.items() if key != "tables_unavailable"} != expected
            or not isinstance(unavailable, dict) or status != "passed" or coverage != text_profile.COVERAGE
            or table_document is not None or table_version is not None
            or (fact_version, report_version, chunk_version) != (versions["facts"], versions["reports"], versions["chunks"])
            or expected["source_hashes"].get(fact_path) != fact_sha
            or expected["source_hashes"].get(report_path) != report_sha
            or not all(counts.get(key, 0) > 0 for key in ("facts", "reports", "chunks")) or counts.get("tables") != 0
            or not all(checks.get(check) is True for check in text_profile.REQUIRED_CHECKS)):
        return None
    return unavailable


def _stored_set(conn, accession, set_key, expected):
    """Only trust committed, sealed selections in the actual receiving database."""
    with conn.transaction():
        row = conn.execute("""SELECT s.configuration, s.validation_status, s.counts,
            fd.local_path, fd.sha256, td.local_path, td.sha256,
            s.fact_extraction_version, s.table_extraction_version,
            r.extraction_version, s.chunking_version, s.validation
            FROM sec.filing_processing_sets s JOIN sec.filings f ON f.id=s.filing_id
            JOIN sec.source_documents fd ON fd.id=s.fact_source_document_id
            JOIN sec.source_documents td ON td.id=s.table_source_document_id
            JOIN sec.reports r ON r.id=s.report_id
            WHERE f.accession_number=%s AND s.set_key=%s
              AND fd.role='extracted_xbrl_instance' AND td.role='main_inline_xbrl_filing'
              AND r.source_document_id=td.id""", (accession, set_key)).fetchone()
        if not row or row[0] != expected or row[1] != "passed":
            return False
        versions = expected["versions"]
        try:
            profile, _ = adapter_profile(versions.get("tables"))
        except ValueError:
            return False
        required_checks = profile.REQUIRED_CHECKS
        checks = row[11].get("checks", {})
        return (all(row[2].get(k, 0) > 0 for k in ("facts", "tables", "reports", "chunks"))
                and expected["source_hashes"].get(row[3]) == row[4]
                and expected["source_hashes"].get(row[5]) == row[6]
                and tuple(row[7:11]) == tuple(versions[k] for k in ("facts", "tables", "reports", "chunks"))
                and all(checks.get(check) is True for check in required_checks))


def process_queue(path, state, client, data_root, *, mode="dry-run", connection=None,
                  destination=None, limit=None, replay=False, retry_failed_only=False,
                  validated_only=False, max_database_bytes=None, tables_version="tables-v1", emit=print):
    from .acquisition import AccessBlocked, download_filing
    from .processing import run_dry_run, run_ingestion

    if mode not in {"download", "dry-run", "ingest"}:
        raise ValueError("Unsupported queue operation.")
    if mode == "ingest" and connection is None:
        raise ValueError("Ingestion requires an explicit database connection.")
    if validated_only and (mode != "ingest" or retry_failed_only):
        raise ValueError("Validated-only selection requires ingestion and cannot be combined with failure-only retry.")
    if max_database_bytes is not None and (mode != "ingest" or type(max_database_bytes) is not int or max_database_bytes < 1):
        raise ValueError("A database size limit requires ingestion and a positive integer byte limit.")
    data_root = Path(data_root).resolve()
    for accession, entry in state["filings"].items():
        if (not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)
                or entry["filing"].get("accession_number") != accession):
            raise ValueError("Queue accession keys must be canonical and match their filing records.")
    profile, set_key = adapter_profile(tables_version)
    text_profile = text_fallback_profile(tables_version) if mode != "download" else None
    is_document_set = tables_version == "tables-v4"
    versions = profile.VERSIONS
    required_checks = profile.REQUIRED_SOURCE_CHECKS if is_document_set else profile.REQUIRED_CHECKS
    verified_successes = set()
    state["last_run"] = {"mode": mode, "versions": dict(versions), "destination": destination,
                         "configuration": dict(state["configuration"]),
                         "started_at_utc": datetime.now(timezone.utc).isoformat(),
                         "verified_successes": 0, "queue_size": len(state["filings"]), "complete": False}
    if max_database_bytes is not None:
        state["last_run"]["max_database_bytes"] = max_database_bytes
    save_state(path, state)

    def record_success(accession):
        verified_successes.add(accession)
        state['last_run']['verified_successes'] = len(verified_successes)
        state['last_run']['review_required_successes'] = sum(
            state['filings'][key].get('status') in {'validated_review_required', 'ingested_review_required'}
            for key in verified_successes)

    attempted = 0
    ordered_filings = sorted(state["filings"].items(), key=lambda item: (
        item[1].get("status") not in {"failed", "access_blocked"}, item[1]["filing"]["filing_date"], item[0]), reverse=True)
    for accession, entry in ordered_filings:
        if validated_only and entry.get("status") not in {"validated", "validated_review_required"}:
            continue
        if retry_failed_only and entry.get("status") not in {"failed", "access_blocked"}:
            continue
        if limit is not None and attempted >= limit:
            break
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or entry["filing"].get("accession_number") != accession:
            raise ValueError("Queue accession keys must be canonical and match their filing records.")
        manifest_path = data_root / "data" / "raw" / "sec" / accession / "source_manifest.json"
        counted = False
        tables_unavailable = None
        phase = "source acquisition"
        try:
            if not manifest_path.exists():
                if client is None:
                    continue
                attempted += 1
                entry["attempts"] += 1
                counted = True
                manifest_path = download_filing(client, entry["filing"], data_root)
            phase = "source verification"
            manifest = verify_filing(manifest_path, data_root)
            phase = "registrant binding"
            alias = manifest.get("cik") != entry["filing"].get("cik")
            if alias:
                from .registrants import verify_discovery_binding
                verify_discovery_binding(manifest, entry["filing"], data_root)
            elif any(manifest.get(key) != entry["filing"].get(key) for key in
                     ("accession_number", "cik", "form", "filing_date", "reporting_period_end", "filing_index_url")):
                raise ValueError("Archived identity differs from the queued filing.")
            for binding in entry.get("discovery_bindings", []):
                if binding["cik"] != entry["filing"]["cik"]:
                    from .registrants import verify_discovery_binding
                    verify_discovery_binding(manifest, binding, data_root)
            primary = next(r for r in manifest["files"] if r["role"] == "main_inline_xbrl_filing")
            if primary["source_url"] != urljoin(manifest["filing_index_url"], entry["filing"]["primary_document"]):
                raise ValueError("Archived primary document differs from discovery.")
            if is_document_set:
                from .document_sets import download_document_set, verify_document_set
                phase = "document set acquisition"
                manifest_path = download_document_set(client, manifest_path, data_root)
                phase = "document set verification"
                manifest = verify_document_set(manifest_path, data_root)
            source_hashes = {r["local_path"]: r["sha256"] for r in [manifest["index_document"], *manifest["files"]]}
            expected = {"versions": versions, "source_hashes": source_hashes, **policy_metadata(versions)}
            phase = "processing metadata"
            if mode != "download" and is_document_set:
                expected = profile.configuration_from_manifest(manifest, hashlib.sha256(manifest_path.read_bytes()).hexdigest())
            elif mode != "download" and hasattr(profile, "configuration_metadata"):
                try:
                    expected.update(profile.configuration_metadata(manifest, data_root))
                except Exception as exc:
                    # The v3 receipt identity parses every table; a table failure
                    # leaves the facts and text to the text-only fallback.
                    if text_profile is None or _fatal_runtime_error(exc):
                        raise
                    tables_unavailable = _failure_detail(exc, phase)
            previous = entry.get("result", {})
            if (mode == "dry-run" and not is_document_set and not replay and entry.get("status") in {"validated", "ingested"}
                    and previous.get("versions") == versions and previous.get("source_hashes") == source_hashes
                    and previous.get('parser_revisions') == expected.get('parser_revisions')
                    and previous.get("validation", {}).get("passed") is True
                    and all(previous.get("validation", {}).get("checks", {}).get(check) is True for check in required_checks)):
                record_success(accession)
                continue
            phase = "stored receipt verification"
            if mode == "ingest" and not replay:
                text_receipt = None
                if is_document_set:
                    from .backfill_v4 import stored_receipt
                    receipt = stored_receipt(connection, accession, set_key, expected, required_checks)
                    receipt_status = receipt['validation_status'] if receipt else None
                else:
                    receipt_status = None
                    if tables_unavailable is None and _stored_set(connection, accession, set_key, expected):
                        receipt_status = "passed"
                    elif text_profile is not None:
                        text_expected = {"versions": versions, "source_hashes": source_hashes, **policy_metadata(versions),
                                         **text_profile.configuration_metadata(manifest, data_root)}
                        text_receipt = _stored_text_set(connection, accession, text_profile, text_expected)
                        if text_receipt is not None:
                            receipt_status = "review_required"
                if receipt_status:
                    entry.update(status="ingested_review_required" if receipt_status == "review_required" else "ingested",
                                 destination=destination, processing_versions=dict(versions))
                    if is_document_set:
                        entry.update(result=receipt, manifest=str(manifest_path.relative_to(data_root)))
                    elif text_receipt is not None:
                        entry.update(manifest=str(manifest_path.relative_to(data_root)), result={
                            "mode": "stored_receipt", "accession_number": accession, "versions": dict(versions),
                            "source_hashes": source_hashes, "set_key": text_profile.SET_KEY,
                            "tables_unavailable": text_receipt})
                    entry.pop("error_type", None)
                    entry.pop("error_stage", None)
                    entry.pop("error_detail", None)
                    record_success(accession)
                    save_state(path, state)
                    continue
            if mode == "ingest" and max_database_bytes is not None:
                with connection.transaction():
                    size = connection.execute('SELECT pg_database_size(current_database())').fetchone()[0]
                if size >= max_database_bytes:
                    state["last_run"].update(stopped_reason="database_size_limit", observed_database_bytes=size)
                    emit(json.dumps({"event": "database_size_limit", "observed_bytes": size,
                                     "limit_bytes": max_database_bytes}))
                    break
            if mode == "download" and entry.get("status") in {"downloaded", "validated", "ingested", "validated_review_required", "ingested_review_required"}:
                entry["manifest"] = str(manifest_path.relative_to(data_root))
                record_success(accession)
                save_state(path, state)
                continue
            if not counted:
                attempted += 1
                entry["attempts"] += 1
                counted = True
            entry["manifest"] = str(manifest_path.relative_to(data_root))
            phase = mode
            if mode == "download":
                result = {"accession_number": accession}
                entry["status"] = "downloaded"
            elif mode == "dry-run":
                if is_document_set:
                    result = profile.run_dry_run(manifest_path=manifest_path, data_root=data_root)
                else:
                    result = None
                    if tables_unavailable is None:
                        try:
                            plan = profile.get_dry_run_plan()
                            result = run_dry_run(manifest_path=manifest_path, data_root=data_root,
                                versions=versions, stages=plan.stages, required_checks=plan.required_checks, verify_inputs=verify_filing)
                        except Exception as exc:
                            if text_profile is None or _fatal_runtime_error(exc):
                                raise
                            tables_unavailable = _failure_detail(exc, phase)
                    if result is None:
                        phase = "text-only dry-run"
                        plan = text_profile.get_dry_run_plan()
                        result = run_dry_run(manifest_path=manifest_path, data_root=data_root,
                            versions=versions, stages=plan.stages, required_checks=plan.required_checks, verify_inputs=verify_filing)
                        result.update(set_key=text_profile.SET_KEY, tables_unavailable=tables_unavailable)
                review = result.get("validation_status") == "review_required" or "tables_unavailable" in result
                entry["status"] = "validated_review_required" if review else "validated"
            else:
                if is_document_set:
                    result = profile.run_ingestion(connection, manifest_path=manifest_path, data_root=data_root,
                        processing_set_key=set_key)
                else:
                    result = None
                    if tables_unavailable is None:
                        try:
                            result = run_ingestion(connection, manifest_path=manifest_path, data_root=data_root,
                                versions=versions, plan=profile.get_ingestion_plan(), verify_inputs=verify_filing,
                                processing_set_key=set_key,
                                coverage={"facts": "complete", "tables": "partial", "reports": "unknown", "chunks": "complete"})
                        except Exception as exc:
                            if text_profile is None or _fatal_runtime_error(exc):
                                raise
                            tables_unavailable = _failure_detail(exc, phase)
                            connection.rollback()
                    if result is None:
                        phase = "text-only ingest"
                        result = run_ingestion(connection, manifest_path=manifest_path, data_root=data_root,
                            versions=versions, plan=text_profile.get_ingestion_plan(tables_unavailable),
                            verify_inputs=verify_filing, processing_set_key=text_profile.SET_KEY,
                            coverage=text_profile.COVERAGE)
                        result.update(set_key=text_profile.SET_KEY, tables_unavailable=tables_unavailable)
                review = result.get("validation_status") == "review_required" or "tables_unavailable" in result
                entry.update(status="ingested_review_required" if review else "ingested", destination=destination)
            entry["result"] = result
            if mode != "download":
                entry["processing_versions"] = dict(versions)
            record_success(accession)
            entry.pop("error_type", None)
            entry.pop("error_stage", None)
            entry.pop("error_detail", None)
            event = {"event": entry["status"], "accession": accession, "counts": result.get("counts")}
            if "tables_unavailable" in result:
                event["tables"] = "absent"
            emit(json.dumps(event))
        except AccessBlocked:
            if not counted:
                attempted += 1
                entry["attempts"] += 1
            entry.update(status="access_blocked")
            save_state(path, state)
            raise
        except Exception as exc:
            # Connection/adapter diagnostics can contain credentials or source
            # text. Persist only the type and a pipeline-owned stage message.
            rollback_failed = False
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    rollback_failed = True
            if not counted:
                attempted += 1
                entry["attempts"] += 1
            entry.update(status="failed", error_type=type(exc).__name__,
                         error_stage=f"Backfill failed during {phase}.",
                         error_detail=_failure_detail(exc, phase))
            if tables_unavailable is not None:
                entry["error_detail"]["full_import"] = tables_unavailable
            from .processing import ParserRevisionConflict, ProcessingError
            if isinstance(exc, ProcessingError):
                entry["error_stage"] = str(exc)[:250]
            emit(json.dumps({"event": "failed", "accession": accession, "error_type": type(exc).__name__}))
            if isinstance(exc, ParserRevisionConflict):
                state['last_run'].update(stopped_reason='parser_revision_mismatch', complete=False,
                    finished_at_utc=datetime.now(timezone.utc).isoformat())
                save_state(path, state)
                raise
            if rollback_failed or _fatal_runtime_error(exc):
                state['last_run'].update(stopped_reason='infrastructure_error', complete=False,
                    finished_at_utc=datetime.now(timezone.utc).isoformat())
                save_state(path, state)
                raise ProcessingError('Backfill stopped after an infrastructure error; the checkpoint was preserved.') from exc
        save_state(path, state)
    state["last_run"].update(verified_successes=len(verified_successes),
        review_required_successes=sum(state['filings'][key].get('status') in {'validated_review_required', 'ingested_review_required'}
                                      for key in verified_successes),
        complete=bool(state["filings"]) and len(verified_successes) == len(state["filings"]),
        finished_at_utc=datetime.now(timezone.utc).isoformat())
    save_state(path, state)
    return attempted
