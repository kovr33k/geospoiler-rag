"""LightRAG document ingestion from enriched card graph_text."""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lightrag import LightRAG

from geospoiler import config
from geospoiler.operations.release_identity import (
    assert_no_release_secrets,
    atomic_write_json,
    safe_exception_diagnostic,
    safe_exception_token,
    safe_resolve_release_path,
    sanitize_release_value,
    strict_json_loads,
)
from geospoiler.providers.runtime import LLM_ROLE as _LLM_ROLE
from geospoiler.providers.runtime import logger
from geospoiler.retrieval.lightrag.manifest_contract import load_manifest_file, snapshot_mismatches
from geospoiler.retrieval.lightrag.storage import (
    _HEADER_LINE_RE,
    _canonical_source_path,
    _cleanup_skipped_doc,
    _doc_status_field,
    _doc_status_value,
    _load_document_metadata,
    _rag_file_path,
    _remove_source_metadata_index,
    _skipped_insert_report_path,
    _source_doc_id,
    _sync_source_metadata_index,
    _wait_for_doc_terminal_status,
    _write_skipped_insert_report,
)

_POSTHOLDER_LINE_RE = re.compile(
    r"^\[(?:Видео:|Аудио:|Transcript|Voice transcript|Video transcript|AI-диалог:|"
    r"Внешняя ссылка:|Малоинформативный пост:|Instagram Reel:.*очередь|"
    r"Отправлено в очередь на ручной просмотр:|Уже обработано:).*\]$"
)
_INSTAGRAM_REEL_WRAPPER_RE = re.compile(
    "^\\[(?:Instagram Reel:|\u0414\u043b\u0438\u043d\u043d\u044b\u0439 Instagram Reel:).*\\]$",
    re.IGNORECASE,
)


class AcceptedManifestReportError(RuntimeError):
    """Raised when an accepted-load report cannot be safely published."""


def _safe_report_error(prefix: str, exc: BaseException) -> str:
    """Return a report-safe diagnostic without copying exception details."""
    return (
        f"{prefix}:"
        + safe_exception_token(
            "accepted_manifest_error",
            exc,
            component="loader.ingest",
        )
    )


def _accepted_load_failure(
    primary_message: str,
    report_error: BaseException | None = None,
) -> RuntimeError:
    """Build one detached public failure without retaining lower-level errors."""
    safe_message, _redactions = sanitize_release_value(primary_message)
    if not isinstance(safe_message, str):  # pragma: no cover - scalar contract
        safe_message = "accepted manifest load failed"
    if report_error is not None:
        diagnostic = safe_exception_diagnostic(
            "accepted_manifest_report_publication_failed",
            report_error,
            component="loader.ingest",
        )
        safe_message = ";".join(
            (
                safe_message,
                diagnostic["error_code"],
                diagnostic["component"],
                diagnostic["exception_type"],
                diagnostic["message_sha256"],
            )
        )
    return RuntimeError(safe_message)


def _attempt_accepted_manifest_report(**kwargs: Any) -> BaseException | None:
    """Publish a report and return a detached publication failure, if any."""
    report_error: BaseException | None = None
    try:
        _write_accepted_manifest_report(**kwargs)
    except Exception as exc:
        report_error = exc
    return report_error


def _failed_report_payload(
    base_payload: dict[str, Any],
    diagnostic: str,
) -> dict[str, Any]:
    """Turn an uncommitted report payload into a sanitized failed diagnostic."""
    failed_payload = dict(base_payload)
    failed_payload["status"] = "failed"
    failed_count = failed_payload.get("failed")
    if not isinstance(failed_count, int) or isinstance(failed_count, bool):
        failed_count = 0
    failed_payload["failed"] = max(1, failed_count)
    failed_payload["errors"] = [
        *list(failed_payload.get("errors") or []),
        diagnostic,
    ]
    return failed_payload


def _write_non_atomic_diagnostic(report_path: Path, payload: dict[str, Any]) -> None:
    """Create fallback evidence without overwriting an existing report."""
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    assert_no_release_secrets(data, label="accepted_manifest_diagnostic")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8") as handle:
        handle.write(data)


def _persist_report(
    report_path: Path,
    payload: dict[str, Any],
    *,
    fallback_payload: dict[str, Any],
) -> BaseException | None:
    """Persist a report and return an atomic-write error if fallback was needed."""
    atomic_error: BaseException | None = None
    public_error: AcceptedManifestReportError | None = None
    try:
        assert_no_release_secrets(
            json.dumps(payload, ensure_ascii=False),
            label="accepted_manifest_load_report",
        )
        atomic_write_json(report_path, payload)
    except Exception as exc:
        atomic_error = exc
        try:
            _write_non_atomic_diagnostic(report_path, fallback_payload)
        except Exception as fallback_error:
            public_error = AcceptedManifestReportError(
                _safe_report_error(
                    "accepted manifest diagnostic report write failed",
                    fallback_error,
                )
            )
    if public_error is not None:
        raise public_error from None
    return atomic_error


def _manifest_sha256(manifest_path: Path, manifest_bytes: bytes | None) -> str | None:
    """Return a manifest hash without allowing diagnostic generation to mask a load error."""
    if manifest_bytes is not None:
        return hashlib.sha256(manifest_bytes).hexdigest()
    if not manifest_path.is_file():
        return None
    try:
        return _sha256_file(manifest_path)
    except OSError:
        return None



def _accepted_manifest_report_path() -> Path:
    """Return the machine-readable report path for the latest manifest load."""
    return config.PROJECT_ROOT / "artifacts" / "accepted_manifest_load_report.json"


def _write_accepted_manifest_report(
    *,
    run_id: str | None,
    manifest_path: Path,
    status: str,
    expected: int,
    inserted: int,
    skipped: int,
    failed: int,
    errors: list[str],
    manifest_payload: dict[str, Any] | None = None,
    manifest_bytes: bytes | None = None,
    loaded_sources: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Persist a deterministic report for automation and operator recovery."""
    report_path = _accepted_manifest_report_path()
    base_payload = {
        "schema_version": "accepted_manifest_load_report_v1",
        "release_schema_version": "release_evidence_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _manifest_sha256(manifest_path, manifest_bytes),
        "status": status,
        "expected": expected,
        "inserted": inserted,
        "skipped": skipped,
        "failed": failed,
        "loaded_sources": sorted(
            list(loaded_sources or []),
            key=lambda item: item.get("source_id", ""),
        ),
        "errors": list(errors),
    }
    base_payload, _redactions = sanitize_release_value(base_payload)
    if not isinstance(base_payload, dict):  # pragma: no cover - recursive contract
        raise AcceptedManifestReportError("accepted manifest report sanitization failed")
    payload = base_payload
    preparation_error: BaseException | None = None
    try:
        from geospoiler.operations.release_identity import build_identity, stamp_report

        if manifest_payload is None:
            validation = load_manifest_file(manifest_path)
            if validation.errors:
                raise ValueError("accepted_manifest_validation_failed")
            manifest_payload = validation.payload
        identity = build_identity(
            str(run_id or ""),
            manifest_path,
            manifest_payload,
            manifest_bytes=manifest_bytes,
        )
        payload = stamp_report(
            payload,
            identity,
            f"python main.py load --manifest {manifest_path}",
            producer="loader.ingest",
            producer_schema_version="accepted_manifest_load_report_v1",
            source_inputs={
                "manifest_sha256": identity["manifest_sha256"],
                "corpus_identity_sha256": identity["corpus_identity_sha256"],
            },
            command_argv=["python", "main.py", "load", "--manifest", str(manifest_path)],
        )
    except Exception as exc:
        preparation_error = exc

    if preparation_error is not None:
        diagnostic = _failed_report_payload(
            base_payload,
            _safe_report_error("accepted manifest report preparation failed", preparation_error),
        )
        write_error = _persist_report(
            report_path,
            diagnostic,
            fallback_payload=diagnostic,
        )
        if status == "accepted":
            if write_error is not None:
                public_error = AcceptedManifestReportError(
                    _safe_report_error("accepted manifest report write failed", write_error)
                )
            else:
                public_error = AcceptedManifestReportError(
                    _safe_report_error(
                        "accepted manifest report preparation failed",
                        preparation_error,
                    )
                )
            raise public_error from None
        return diagnostic

    fallback_payload = _failed_report_payload(
        base_payload,
        "accepted manifest report write failed",
    )
    write_error = _persist_report(
        report_path,
        payload,
        fallback_payload=fallback_payload,
    )
    if write_error is not None:
        if status == "accepted":
            raise AcceptedManifestReportError(
                _safe_report_error("accepted manifest report write failed", write_error)
            ) from None
        return fallback_payload
    return payload


def _sha256_file(path: Path) -> str:
    """Hash file bytes without applying text decoding or newline conversion."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_text_for_rag(text: str) -> str:
    """Remove metadata wrappers and placeholders before graph extraction."""
    lines = text.splitlines()
    if lines and _HEADER_LINE_RE.match(lines[0].strip()):
        lines = lines[1:]

    kept_lines = []
    for line in lines:
        stripped = line.strip()
        if _POSTHOLDER_LINE_RE.match(stripped) or _INSTAGRAM_REEL_WRAPPER_RE.match(stripped):
            continue
        kept_lines.append(line)

    cleaned = "\n".join(kept_lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned

async def _upsert_text(rag: LightRAG, source_path: str, text: str) -> None:
    """
    Replace an existing LightRAG document for this source path, then insert the new text.

    LightRAG's default doc_id is content-based, which duplicates logical documents when the
    same file is reloaded after edits. We instead key documents by source path.
    """
    canonical_path = _canonical_source_path(source_path)
    doc_id = _source_doc_id(canonical_path)
    rag_file_path = _rag_file_path(canonical_path)
    metadata = _load_document_metadata(source_path, text)
    metadata["canonical_path"] = canonical_path
    _sync_source_metadata_index(source_path, metadata)
    if rag_file_path != canonical_path:
        _sync_source_metadata_index(rag_file_path, metadata)

    rag_text = _prepare_text_for_rag(text)
    if not rag_text.strip():
        raise RuntimeError(f"document became empty after RAG cleanup: {canonical_path}")

    existing_doc = await rag.doc_status.get_by_id(doc_id)
    if existing_doc:
        deletion = await rag.adelete_by_doc_id(doc_id)
        if deletion.status != "success":
            raise RuntimeError(
                f"failed to replace existing doc_id={doc_id} for {canonical_path}: {deletion.message}"
            )

    token = _LLM_ROLE.set("build")
    try:
        await rag.ainsert([rag_text], ids=[doc_id], file_paths=[rag_file_path])
    finally:
        _LLM_ROLE.reset(token)


async def _existing_processed_matches(
    rag: LightRAG,
    source_path: str,
    text: str,
) -> bool:
    """Verify processed status and prepared content before any reuse."""
    canonical_path = _canonical_source_path(source_path)
    doc_id = _source_doc_id(canonical_path)
    status_doc = await rag.doc_status.get_by_id(doc_id)
    if _doc_status_value(status_doc).strip().lower() != "processed":
        return False
    try:
        existing = await rag.full_docs.get_by_id(doc_id)
    except Exception:
        return False
    existing_content = _doc_status_field(existing, "content", "")
    if existing_content != _prepare_text_for_rag(text):
        return False
    metadata = _load_document_metadata(source_path, text)
    # Keep the canonical source in every metadata-index alias.  LightRAG may
    # persist only a basename in doc_status even when ingestion supplied a
    # full path; without this field the provenance resolver cannot safely map
    # that basename back to the registry row after a reuse-only load.
    metadata["canonical_path"] = canonical_path
    _sync_source_metadata_index(source_path, metadata)
    rag_file_path = _rag_file_path(canonical_path)
    _sync_source_metadata_index(rag_file_path, metadata)
    return True


async def load_texts(
    rag: LightRAG,
    texts_with_paths: list[tuple[str, str]],
    batch_size: int = 5,
    reuse_processed: bool = False,
) -> int:
    """
    Load normalized texts into LightRAG.

    Args:
        rag: Initialized LightRAG instance
        texts_with_paths: List of (filepath, text_content) tuples
        batch_size: How many texts to insert at once

    Returns:
        Number of successfully inserted texts
    """
    total = len(texts_with_paths)
    inserted = 0
    skipped: list[dict[str, Any]] = []
    skipped_doc_ids: set[str] = set()
    attempted_docs: dict[str, dict[str, Any]] = {}
    insert_timeout = max(1.0, config.RAG_INSERT_TIMEOUT_SECONDS)

    for i, (path, text) in enumerate(texts_with_paths, start=1):
        canonical_path = _canonical_source_path(path)
        doc_id = _source_doc_id(canonical_path)
        attempted_docs[doc_id] = {
            "source_path": canonical_path,
            "content_length": len(text),
        }
        try:
            if reuse_processed and await _existing_processed_matches(rag, path, text):
                inserted += 1
                logger.info("  Reused verified processed document: %s", canonical_path)
                continue
            await asyncio.wait_for(
                _upsert_text(rag, path, text),
                timeout=insert_timeout,
            )
            status_doc = await _wait_for_doc_terminal_status(
                rag,
                doc_id,
                timeout_seconds=insert_timeout,
            )
            status = _doc_status_value(status_doc).strip().lower()
            if status != "processed":
                if status == "failed":
                    reason = "LightRAG marked document as failed"
                elif not status:
                    reason = "LightRAG status missing after insert"
                else:
                    reason = f"LightRAG returned unexpected status: {status}"
                skipped.append(
                    {
                        "source_path": canonical_path,
                        "doc_id": doc_id,
                        "reason": reason,
                        "content_length": len(text),
                    }
                )
                skipped_doc_ids.add(doc_id)
                logger.error(f"  Skipped non-processed insert for {canonical_path}: {reason}")
                await _cleanup_skipped_doc(rag, doc_id)
                _remove_source_metadata_index(canonical_path)
                continue
            inserted += 1
        except TimeoutError:
            reason = f"insert timeout after {insert_timeout:.0f}s"
            skipped.append(
                {
                    "source_path": canonical_path,
                    "doc_id": doc_id,
                    "reason": reason,
                    "content_length": len(text),
                }
            )
            skipped_doc_ids.add(doc_id)
            logger.error(f"  Skipped timed-out insert for {canonical_path}: {reason}")
            await _cleanup_skipped_doc(rag, doc_id)
            _remove_source_metadata_index(canonical_path)
        except Exception as e:
            reason = safe_exception_token(
                "insert_error",
                e,
                component="loader.ingest",
            )
            skipped.append(
                {
                    "source_path": canonical_path,
                    "doc_id": doc_id,
                    "reason": reason,
                    "content_length": len(text),
                }
            )
            skipped_doc_ids.add(doc_id)
            logger.error(f"  Skipped failed insert for {path}: {reason}")
            await _cleanup_skipped_doc(rag, doc_id)
            _remove_source_metadata_index(canonical_path)

        if i % batch_size == 0 or i == total:
            logger.info(f"  Inserted progress: {inserted}/{total} ({len(skipped)} skipped)")

    for doc_id, doc_info in attempted_docs.items():
        if doc_id in skipped_doc_ids:
            continue
        status_doc = await rag.doc_status.get_by_id(doc_id)
        status = _doc_status_value(status_doc).strip().lower()
        if status != "processed":
            attempted_source_path = str(doc_info["source_path"])
            observed_source_path = str(_doc_status_field(status_doc, "file_path") or "")
            source_path = attempted_source_path
            content_length = _doc_status_field(status_doc, "content_length", doc_info["content_length"])
            if status == "failed":
                reason = "LightRAG marked document as failed after insert returned"
            elif not status:
                reason = "LightRAG status missing after insert returned"
            else:
                reason = f"LightRAG returned unexpected status after insert: {status}"
            skipped.append(
                {
                    "source_path": source_path,
                    "observed_status_source_path": observed_source_path,
                    "doc_id": doc_id,
                    "reason": reason,
                    "content_length": content_length,
                }
            )
            skipped_doc_ids.add(doc_id)
            inserted = max(0, inserted - 1)
            logger.error(f"  Late non-processed insert for {source_path}: {reason}")
            await _cleanup_skipped_doc(rag, doc_id)
            _remove_source_metadata_index(attempted_source_path)

    _write_skipped_insert_report(skipped)
    if skipped:
        logger.warning("Skipped %s RAG insert(s):", len(skipped))
        for item in skipped:
            logger.warning(
                "  %s (%s, len=%s)",
                item["source_path"],
                item["reason"],
                item["content_length"],
            )
        logger.warning("Skipped insert report: %s", _skipped_insert_report_path())

    logger.info(
        f"Loading complete: {inserted}/{total} texts inserted into LightRAG "
        f"({len(skipped)} skipped)."
    )
    return inserted


async def load_from_directory(rag: LightRAG, directory: Path | None = None) -> int:
    """
    Load all .txt files from the normalized output directory into LightRAG.

    Args:
        rag: Initialized LightRAG instance
        directory: Directory to scan (defaults to config.NORMALIZED_DIR)

    Returns:
        Number of inserted texts
    """
    directory = directory or config.NORMALIZED_DIR

    texts_with_paths = []
    for txt_file in sorted(directory.rglob("*.txt")):
        try:
            text = txt_file.read_text(encoding="utf-8")
            if text.strip():
                texts_with_paths.append((str(txt_file), text))
        except Exception as e:
            diagnostic = safe_exception_token(
                "normalized_text_read_failed",
                e,
                component="loader.ingest",
            )
            logger.warning("  Cannot read %s: %s", txt_file, diagnostic)

    if not texts_with_paths:
        logger.warning("No normalized texts found to load.")
        return 0

    logger.info(f"Found {len(texts_with_paths)} normalized texts to load.")
    return await load_texts(rag, texts_with_paths)


async def load_from_enriched(
    rag: LightRAG, enriched_dir: Path | None = None, *, reuse_processed: bool = True,
) -> int:
    """
    Load graph_text from enriched cards into LightRAG.

    Uses the structured graph_text field which is purpose-built for knowledge graph
    extraction — contains entities, events, theses, and quotes in a format optimized
    for LightRAG's entity/relationship extraction.

    Only cards with schema_version=enriched_v2 and non-empty graph_text are loaded.
    Missing graph_text is skipped; normalized source text is never substituted.

    Returns:
        Number of inserted texts
    """
    enriched_dir = enriched_dir or config.ENRICHED_DIR

    if not enriched_dir.exists():
        logger.warning("Enriched directory does not exist: %s", enriched_dir)
        return 0

    texts_with_paths: list[tuple[str, str]] = []
    skipped_non_v2 = 0
    skipped_no_graph_text = 0

    for card_path in sorted(enriched_dir.rglob("*.enriched.json")):
        try:
            card = strict_json_loads(card_path.read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeError) as exc:
            diagnostic = safe_exception_token(
                "enriched_card_read_failed",
                exc,
                component="loader.ingest",
            )
            logger.debug("Cannot read enriched card %s: %s", card_path, diagnostic)
            continue

        if card.get("schema_version") != "enriched_v2":
            skipped_non_v2 += 1
            continue

        quality_flags = card.get("quality_flags") or []
        if {"extraction_unstable", "partial_segment_failure"} & set(quality_flags):
            logger.info("Skipping unstable enriched card from LightRAG: %s", card_path)
            continue
        if card.get("duplicate_of"):
            logger.info("Skipping duplicate enriched card from LightRAG: %s", card_path)
            continue

        graph_text = (card.get("graph_text") or "").strip()
        if not graph_text:
            skipped_no_graph_text += 1
            continue

        provenance = card.get("provenance") if isinstance(card.get("provenance"), dict) else {}
        source_path = provenance.get("normalized_path") or ""
        if not source_path:
            logger.warning("Skipping v2 card without provenance.normalized_path: %s", card_path)
            continue
        texts_with_paths.append((source_path, graph_text))

    if not texts_with_paths:
        logger.warning("No enriched cards with graph_text found to load.")
        return 0

    if skipped_non_v2:
        logger.info("  Skipped %d non-v2 enriched card(s)", skipped_non_v2)
    if skipped_no_graph_text:
        logger.info("  Skipped %d enriched_v2 card(s) without graph_text", skipped_no_graph_text)

    logger.info(f"Found {len(texts_with_paths)} enriched texts to load into LightRAG.")
    return await load_texts(rag, texts_with_paths, reuse_processed=reuse_processed)


async def load_from_manifest(
    rag: LightRAG,
    manifest_path: Path | str,
    *,
    reuse_processed: bool = False,
    allow_legacy_manifest: bool = False,
) -> int:
    """Load accepted Enriched cards only after a complete fail-closed preflight.

    The manifest is deliberately stricter than the normal directory loader.  A
    caller must provide immutable paths, provenance, and hashes for every
    accepted source.  All entries are validated before ``load_texts`` is
    called, so a malformed manifest cannot partially mutate LightRAG.
    """
    path_construction_error: BaseException | None = None
    try:
        candidate_path = Path(manifest_path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        path_construction_error = exc
        candidate_path = None
    if path_construction_error is not None:
        failure = _accepted_load_failure(
            _safe_report_error(
                "accepted manifest path construction failed",
                path_construction_error,
            )
        )
        raise failure from None
    if candidate_path is None:  # pragma: no cover - defensive type narrowing
        raise RuntimeError("accepted manifest path construction failed")
    path = safe_resolve_release_path(
        candidate_path,
        label="accepted_manifest_path",
    )
    run_id: str | None = None
    expected = 0
    manifest_bytes: bytes | None = None
    manifest_read_error: BaseException | None = None
    try:
        manifest_validation = load_manifest_file(
            path,
            allow_legacy_manifest=allow_legacy_manifest,
        )
        payload = manifest_validation.payload
        manifest_bytes = manifest_validation.manifest_bytes
    except (OSError, UnicodeError, ValueError) as exc:
        manifest_read_error = exc

    if manifest_read_error is not None:
        message = _safe_report_error(
            "Cannot read accepted load manifest",
            manifest_read_error,
        )
        report_error = _attempt_accepted_manifest_report(
            run_id=None,
            manifest_path=path,
            status="failed",
            expected=0,
            inserted=0,
            skipped=0,
            failed=1,
            errors=[
                _safe_report_error(
                    "Cannot read accepted load manifest",
                    manifest_read_error,
                )
            ],
        )
        failure = _accepted_load_failure(message, report_error)
        raise failure from None

    # The shared pure validator is the only manifest authority.  Repeating
    # these checks in the loader used to let loader/cold/verifier drift apart.
    shared_validation = manifest_validation
    if shared_validation.errors:
        run_id = payload.get("run_id") if isinstance(payload, dict) else None
        message = "Accepted load manifest validation failed: " + "; ".join(
            shared_validation.errors
        )
        report_error = _attempt_accepted_manifest_report(
            run_id=run_id if isinstance(run_id, str) else None,
            manifest_path=path,
            status="failed",
            expected=len(payload.get("sources", [])) if isinstance(payload, dict) and isinstance(payload.get("sources"), list) else 0,
            inserted=0,
            skipped=0,
            failed=len(shared_validation.errors),
            errors=list(shared_validation.errors),
            manifest_bytes=manifest_bytes,
        )
        failure = _accepted_load_failure(message, report_error)
        raise failure from None

    errors: list[str] = []
    entries = list(shared_validation.entries)
    snapshots = shared_validation.snapshots or {}
    expected = len(entries)
    if isinstance(payload, dict) and isinstance(payload.get("run_id"), str):
        run_id = payload["run_id"]
    texts_with_paths: list[tuple[str, str]] = []
    loaded_sources: list[dict[str, str]] = []

    for index, entry in enumerate(entries):
        source_id = entry["source_id"]
        snapshot = snapshots.get(source_id)
        if snapshot is None:
            errors.append(f"sources[{index}] validated snapshot is missing")
            continue
        snapshot_errors = snapshot_mismatches(snapshot)
        if snapshot_errors:
            errors.extend(f"sources[{index}] {error}" for error in snapshot_errors)
            continue
        graph_text = snapshot.graph_text
        if not graph_text.strip():
            errors.append(f"sources[{index}] missing graph_text in validated snapshot")
            continue
        texts_with_paths.append((str(snapshot.normalized_path), graph_text))
        loaded_sources.append(
            {
                "source_id": snapshot.source_id,
                "graph_text_sha256": snapshot.graph_text_sha256,
            }
        )

    if errors:
        message = "Accepted load manifest validation failed: " + "; ".join(errors)
        report_error = _attempt_accepted_manifest_report(
            run_id=run_id,
            manifest_path=path,
            status="failed",
            expected=expected,
            inserted=0,
            skipped=0,
            failed=len(errors),
            errors=errors,
            manifest_bytes=manifest_bytes,
            manifest_payload=payload if isinstance(payload, dict) else None,
            loaded_sources=loaded_sources,
        )
        failure = _accepted_load_failure(message, report_error)
        raise failure from None

    load_error: BaseException | None = None
    try:
        inserted = await load_texts(
            rag,
            texts_with_paths,
            reuse_processed=reuse_processed,
        )
    except Exception as exc:
        load_error = exc
        inserted = 0

    if load_error is not None:
        message = _safe_report_error(
            "accepted manifest load failed before completion",
            load_error,
        )
        report_error = _attempt_accepted_manifest_report(
            run_id=run_id,
            manifest_path=path,
            status="failed",
            expected=expected,
            inserted=0,
            skipped=expected,
            failed=1,
            errors=[
                _safe_report_error(
                    "accepted manifest load failed before completion",
                    load_error,
                )
            ],
            manifest_bytes=manifest_bytes,
            manifest_payload=payload if isinstance(payload, dict) else None,
            loaded_sources=loaded_sources,
        )
        failure = _accepted_load_failure(message, report_error)
        raise failure from None

    if inserted != expected:
        skipped = max(0, expected - inserted)
        message = f"accepted manifest load incomplete: inserted={inserted}, expected={expected}"
        report_error = _attempt_accepted_manifest_report(
            run_id=run_id,
            manifest_path=path,
            status="failed",
            expected=expected,
            inserted=inserted,
            skipped=skipped,
            failed=skipped,
            errors=[message],
            manifest_bytes=manifest_bytes,
            manifest_payload=payload if isinstance(payload, dict) else None,
            loaded_sources=loaded_sources,
        )
        failure = _accepted_load_failure(message, report_error)
        raise failure from None

    report_error: BaseException | None = None
    try:
        report = _write_accepted_manifest_report(
            run_id=run_id,
            manifest_path=path,
            status="accepted",
            expected=expected,
            inserted=inserted,
            skipped=0,
            failed=0,
            errors=[],
            manifest_bytes=manifest_bytes,
            manifest_payload=payload if isinstance(payload, dict) else None,
            loaded_sources=loaded_sources,
        )
    except Exception as exc:
        report_error = exc
        report = {}
    if report_error is not None:
        failure = _accepted_load_failure(
            "accepted manifest load completed but release report failed",
            report_error,
        )
        raise failure from None
    if report.get("status") != "accepted":
        failure = _accepted_load_failure(
            "accepted manifest load completed without an accepted report"
        )
        raise failure from None
    return inserted
