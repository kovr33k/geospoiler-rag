"""
Enricher Pipeline v2 — produces enriched_v2 cards from normalized posts.

Pipeline:
  normalized text
    -> content_type classifier (rule-based)
    -> triage
    -> preprocessor (clean text + ignored blocks)
    -> LLM extraction (semantic payload only)
    -> validator (contract checks)
    -> repair (optional, max 1 attempt)
    -> postprocessor (assemble final card)
    -> graph_text / search_text builders
    -> enriched_v2 card
"""

import concurrent.futures
import hashlib
import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from geospoiler import config
from geospoiler.contracts.models import EnrichedCardV2, LLMPayload, NormalizedMeta, Provenance, SourceChain
from geospoiler.ingestion.enricher.chunker import chunk_text, needs_chunking
from geospoiler.ingestion.enricher.content_classifier import classify_content
from geospoiler.ingestion.enricher.graph_text_builder import populate_graph_texts
from geospoiler.ingestion.enricher.llm_enricher import (
    EmptyLLMResponseError,
    _normalize_to_payload,
    extract_chunk_raw,
    extract_full_post_raw,
    extract_short_post_raw,
    merge_chunk_results_raw,
)
from geospoiler.ingestion.enricher.preprocessor import PreprocessedText, preprocess
from geospoiler.ingestion.enricher.repair import RepairContext, repair_if_needed, repair_invalid_payload
from geospoiler.ingestion.enricher.triage import TRIAGE_REVIEW, auto_triage
from geospoiler.ingestion.enricher.validator import (
    drop_invalid_optional_items,
    has_required_language_violations,
    validate_payload,
)
from geospoiler.ingestion.enricher.youtube_pipeline import (
    _enrich_youtube_sources,
    _iter_youtube_sources,
    _looks_like_legacy_youtube_document,
    enrich_youtube_all,
)
from geospoiler.ingestion.normalizer.pipeline import read_normalized_meta
from geospoiler.ingestion.normalizer.review_queue import REVIEW_TYPE_ENRICHMENT_FAILURE, REVIEW_TYPE_UNINFORMATIVE
from geospoiler.ingestion.normalizer.review_queue import queue_item as queue_review_item
from geospoiler.ingestion.normalizer.youtube_handler import redact_sensitive_url
from geospoiler.providers import llm_backend

logger = logging.getLogger("geospoiler.enricher")

_SHORT_POST_THRESHOLD = 500
_MEDIA_CONTRACT_VERSION = "media-contract-v2"
_ENRICHMENT_RETRY_ATTEMPTS = 2


class UnusableExtractionError(RuntimeError):
    """Raised when a substantive source has no usable extracted content."""


@dataclass
class EnrichmentStats:
    """Aggregated stats from an enrichment run."""

    scanned: int = 0
    enriched: int = 0
    partial: int = 0
    skipped_up_to_date: int = 0
    skipped_no_meta: int = 0
    skipped_review: int = 0
    skipped_youtube: int = 0
    repaired: int = 0
    youtube_sources: int = 0
    youtube_episodes: int = 0
    youtube_skipped: int = 0
    youtube_segments: int = 0
    youtube_partial: int = 0
    youtube_failed: int = 0
    failed: int = 0
    partial_posts: list = field(default_factory=list)
    by_content_type: dict = field(default_factory=dict)


def enrich_all(
    channel_filter: str | None = None,
    force: bool = False,
    *,
    ready_paths: queue.Queue | None = None,
) -> EnrichmentStats:
    """
    Scan normalized directory and create/update enriched memory cards.

    Args:
        channel_filter: If set, only process this channel subdirectory.
        force: If True, re-enrich all posts regardless of state.

    Returns:
        EnrichmentStats with counts of what happened.
    """
    stats = EnrichmentStats()
    progress = _load_progress()
    if ready_paths is None:
        jobs = _collect_enrichment_jobs(
            progress=progress, channel_filter=channel_filter, force=force, stats=stats,
        )
        _run_enrichment_jobs(jobs, progress, stats)
    else:
        _run_ready_enrichment(ready_paths, progress, stats, force=force)
    # Persist completed generic work before the potentially long YouTube pass.
    # Otherwise an interrupted YouTube run makes all generic LLM work repeat.
    _save_progress(progress)
    if ready_paths is None:
        _add_youtube_stats(stats, enrich_youtube_all(channel_filter=channel_filter, force=force))
    _save_progress(progress)

    logger.info(
        f"Enrichment complete: {stats.enriched} enriched, "
        f"{stats.repaired} repaired, "
        f"{stats.skipped_up_to_date} up-to-date, "
        f"{stats.skipped_review} review, "
        f"{stats.failed} failed out of {stats.scanned} scanned"
    )
    return stats


@dataclass(frozen=True)
class _EnrichmentJob:
    txt_path: Path
    meta_path: Path
    channel_name: str
    msg_id: str
    progress_key: str
    source_fingerprint: str
    out_path: Path


def _collect_enrichment_jobs(
    *,
    progress: dict,
    channel_filter: str | None,
    force: bool,
    stats: EnrichmentStats,
    normalized_paths: list[Path] | None = None,
) -> list[_EnrichmentJob]:
    normalized_dir = config.NORMALIZED_DIR
    enriched_dir = config.ENRICHED_DIR

    if normalized_paths is not None:
        channel_dirs = sorted({path.parent for path in normalized_paths})
    elif channel_filter:
        channel_dirs = [normalized_dir / channel_filter]
        if not channel_dirs[0].exists():
            logger.error(f"Channel directory not found: {channel_dirs[0]}")
            return []
    else:
        channel_dirs = sorted(d for d in normalized_dir.iterdir() if d.is_dir())

    jobs: list[_EnrichmentJob] = []
    for channel_dir in channel_dirs:
        channel_name = channel_dir.name
        txt_files = sorted(path for path in normalized_paths if path.parent == channel_dir) if normalized_paths is not None else sorted(channel_dir.glob("*.txt"))

        for txt_path in txt_files:
            stats.scanned += 1
            msg_id = txt_path.stem
            meta_path = txt_path.with_suffix(".meta.json")
            progress_key = f"{channel_name}/{msg_id}"
            expected_source_id: str | None = None
            metadata_identity_valid = False

            if not meta_path.exists():
                logger.warning(f"No meta.json for {progress_key} — skipping")
                stats.skipped_no_meta += 1
                continue

            try:
                normalized_meta = read_normalized_meta(txt_path)
                expected_source_id = _build_source_id(normalized_meta, channel_name, msg_id)
                metadata_identity_valid = True
                if normalized_meta.youtube_urls and _is_youtube_only_normalized_document(
                    normalized_meta,
                    txt_path,
                ):
                    stats.skipped_youtube += 1
                    continue
            except (OSError, ValueError):
                logger.warning("Cannot inspect metadata for %s", progress_key)

            source_fingerprint = _source_fingerprint(txt_path, meta_path)
            out_path = enriched_dir / channel_name / f"{msg_id}.enriched.json"
            if (
                not force
                and metadata_identity_valid
                and not normalized_meta.media
                and _migrate_non_media_contract(
                    progress,
                    progress_key,
                    source_fingerprint,
                    out_path,
                    expected_source_id,
                )
            ):
                stats.skipped_up_to_date += 1
                continue
            if (
                not force
                and out_path.exists()
                and not _needs_enrichment(progress, progress_key, source_fingerprint)
                and metadata_identity_valid
                and _is_valid_enriched_output(out_path, expected_source_id)
            ):
                stats.skipped_up_to_date += 1
                continue

            jobs.append(
                _EnrichmentJob(
                    txt_path=txt_path,
                    meta_path=meta_path,
                    channel_name=channel_name,
                    msg_id=msg_id,
                    progress_key=progress_key,
                    source_fingerprint=source_fingerprint,
                    out_path=out_path,
                )
            )

    return jobs


def _run_ready_enrichment(ready_paths: queue.Queue, progress: dict, stats: EnrichmentStats, *, force: bool) -> None:
    """One coordinator publishes results while a bounded producer saves inputs."""
    concurrency = _enrichment_concurrency()
    finished = False
    seen_youtube_keys = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        pending = {}

        def submit_youtube(metadata_paths, legacy_meta_paths):
            errors = []
            sources = list(_iter_youtube_sources(channel_filter=None, metadata_paths=metadata_paths,
                                                 legacy_meta_paths=legacy_meta_paths,
                                                 seen_keys=seen_youtube_keys, discovery_errors=errors))
            if sources or errors:
                pending[executor.submit(_enrich_youtube_sources, sources, force=force, discovery_errors=errors)] = None

        while not finished or pending:
            for future in list(pending):
                if not future.done():
                    continue
                job = pending.pop(future)
                try:
                    if job is None:
                        _add_youtube_stats(stats, future.result())
                    else:
                        _handle_enrichment_result(job, future.result(), progress, stats)
                except Exception as exc:
                    if job is None:
                        stats.youtube_failed += 1
                        logger.exception("YouTube stream processing failed")
                    else:
                        _record_enrichment_failure(job, exc, stats, progress)
            if pending and (finished or len(pending) >= concurrency):
                concurrent.futures.wait(pending, timeout=0.05, return_when=concurrent.futures.FIRST_COMPLETED)
                continue
            if finished:
                continue
            try:
                path = ready_paths.get(timeout=0.02)
            except queue.Empty:
                continue
            if path is None:
                finished = True
                continue
            if path.name.endswith(".youtube.meta.json"):
                submit_youtube([path], [])
                continue
            skipped_youtube = stats.skipped_youtube
            jobs = _collect_enrichment_jobs(progress=progress, channel_filter=None, force=force,
                                           stats=stats, normalized_paths=[path])
            for job in jobs:
                pending[executor.submit(_run_enrichment_job_with_retry, job, stats)] = job
            if stats.skipped_youtube > skipped_youtube:
                meta = read_normalized_meta(path)
                if not (meta.model_extra or {}).get("youtube_sources"):
                    submit_youtube([], [path.with_suffix(".meta.json")])


def _add_youtube_stats(stats: EnrichmentStats, youtube_stats) -> None:
    stats.youtube_sources += youtube_stats.sources_scanned
    stats.youtube_episodes += youtube_stats.episodes_written
    stats.youtube_skipped += youtube_stats.episodes_skipped
    stats.youtube_segments += youtube_stats.segments_written
    stats.youtube_partial += youtube_stats.partial_sources
    stats.youtube_failed += youtube_stats.failed_sources


def _is_youtube_only_normalized_document(meta: NormalizedMeta, text_path: Path) -> bool:
    """Keep a standalone YouTube transcript out of the generic Telegram stream."""
    if not meta.youtube_urls:
        return False
    if meta.has_body_text is False:
        return True
    if meta.has_body_text is True:
        return False
    try:
        return _looks_like_legacy_youtube_document(text_path.read_text(encoding="utf-8"))
    except OSError:
        return False


def _run_enrichment_jobs(
    jobs: list[_EnrichmentJob],
    progress: dict,
    stats: EnrichmentStats,
) -> None:
    if not jobs:
        return

    concurrency = min(_enrichment_concurrency(), len(jobs))
    if concurrency <= 1:
        for job in jobs:
            try:
                result = _run_enrichment_job_with_retry(job, stats)
                _handle_enrichment_result(job, result, progress, stats)
            except Exception as exc:
                _record_enrichment_failure(job, exc, stats, progress)
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_job = {
            executor.submit(_run_enrichment_job_with_retry, job, stats): job
            for job in jobs
        }
        for future in concurrent.futures.as_completed(future_to_job):
            job = future_to_job[future]
            try:
                result = future.result()
                _handle_enrichment_result(job, result, progress, stats)
            except Exception as exc:
                _record_enrichment_failure(job, exc, stats, progress)


def _run_enrichment_job_with_retry(job: _EnrichmentJob, stats: EnrichmentStats) -> dict | None:
    """Retry a semantic contract failure once; backend calls retry internally."""
    retryable = (UnusableExtractionError,)
    for attempt in range(1, _ENRICHMENT_RETRY_ATTEMPTS + 1):
        try:
            return _run_enrichment_job(job, stats)
        except retryable as exc:
            if attempt >= _ENRICHMENT_RETRY_ATTEMPTS:
                exc.enrichment_attempts = attempt
                raise
            logger.warning(
                "Retrying enrichment for %s after %s (attempt %s/%s): %s",
                job.progress_key,
                type(exc).__name__,
                attempt + 1,
                _ENRICHMENT_RETRY_ATTEMPTS,
                exc,
            )
    raise AssertionError("unreachable")


def _run_enrichment_job(job: _EnrichmentJob, stats: EnrichmentStats) -> dict | None:
    """Run the full enrichment pipeline for a single post."""
    normalized_text = job.txt_path.read_text(encoding="utf-8")
    meta = read_normalized_meta(job.txt_path)
    meta_data = meta.model_dump(mode="json")

    # 1. Content type classification (rule-based)
    content_type = classify_content(meta_data, normalized_text)

    # 2. Triage check
    triage_status, triage_reason = auto_triage(content_type, meta_data, normalized_text)
    if triage_status == TRIAGE_REVIEW:
        _queue_uninformative_review(
            triage_reason=triage_reason,
            txt_path=job.txt_path,
            channel_name=job.channel_name,
            msg_id=job.msg_id,
            meta=meta_data,
        )
        return None

    # 3. Preprocess
    preprocessed = preprocess(normalized_text)

    repair_context = RepairContext()

    # 4. LLM extraction and strict structural validation
    payload = _extract_payload(preprocessed, content_type, repair_context)
    _normalize_pipeline_quality_flags(payload)

    # 5. Validate
    ignored_texts = [b.text for b in preprocessed.ignored_blocks]
    validation_text = _validation_text(preprocessed)
    validation = validate_payload(payload, validation_text, ignored_texts)

    # 6. Repair if needed (max 1 attempt)
    if not validation.is_valid:
        payload, _ = repair_if_needed(
            payload,
            validation,
            validation_text,
            ignored_texts,
            context=repair_context,
        )
        _normalize_pipeline_quality_flags(payload)

    final_validation = validate_payload(payload, validation_text, ignored_texts)
    extraction_issues = list(final_validation.violations)
    optional_items_dropped = False
    if not final_validation.is_valid:
        dropped = drop_invalid_optional_items(payload, final_validation)
        if dropped:
            optional_items_dropped = True
            final_validation = validate_payload(payload, validation_text, ignored_texts)
            extraction_issues = dropped
    if not final_validation.is_valid and "extraction_unstable" not in payload.quality_flags:
        payload.quality_flags.append("extraction_unstable")
    if has_required_language_violations(final_validation):
        raise UnusableExtractionError(
            "Semantic extraction still violates the Russian-language contract after repair"
        )
    if (
        not final_validation.is_valid
        and preprocessed.body_char_count >= 30
        and not payload.summary.strip()
        and not payload.key_points
    ):
        raise UnusableExtractionError(
            "Semantic extraction is invalid and contains neither summary nor key_points"
        )

    # Media state is runtime-owned and must be derived from normalized
    # metadata, never inferred or removed by the LLM.  This keeps a stale or
    # caption-only card auditable even when its textual payload is valid.
    extraction_issues.extend(_media_quality_issues(meta))
    for flag in _media_quality_flags(meta):
        if flag not in payload.quality_flags:
            payload.quality_flags.append(flag)

    # 7. Assemble final card
    card = _assemble_card(
        payload=payload,
        meta=meta,
        content_type=content_type,
        channel_name=job.channel_name,
        msg_id=job.msg_id,
        txt_path=job.txt_path,
        preprocessed=preprocessed,
        extraction_issues=extraction_issues,
    )

    card_data = card.model_dump(mode="json")
    repair_succeeded = (
        repair_context.succeeded
        and final_validation.is_valid
        and not optional_items_dropped
    )
    card_data["_repair_succeeded"] = repair_succeeded
    if repair_succeeded and threading.current_thread() is threading.main_thread():
        stats.repaired += 1
        card_data["_repair_counted"] = True
    return card_data


def _extract_payload(
    preprocessed: PreprocessedText,
    content_type: str,
    repair_context: RepairContext | None = None,
) -> LLMPayload:
    """Run LLM extraction and apply the job's structural repair budget."""
    context = repair_context or RepairContext()
    if preprocessed.body_char_count < 20:
        return LLMPayload()

    if preprocessed.body_char_count < _SHORT_POST_THRESHOLD:
        raw = extract_short_post_raw(preprocessed, content_type)
        return _payload_from_raw(raw, context)

    if needs_chunking(preprocessed.clean_text):
        text_chunks = chunk_text(preprocessed.clean_text)
        chunk_results = []
        for chunk in text_chunks:
            cr = extract_chunk_raw(chunk["text"], chunk["index"], len(text_chunks))
            if not isinstance(cr, dict):
                cr = {}
            cr["char_range"] = chunk["char_range"]
            chunk_results.append(cr)
        raw = merge_chunk_results_raw(preprocessed.header, chunk_results)
        return _payload_from_raw(raw, context)

    raw = extract_full_post_raw(preprocessed, content_type)
    return _payload_from_raw(raw, context)


def _payload_from_raw(raw: object, repair_context: RepairContext) -> LLMPayload:
    """Validate raw LLM output, using at most one structural repair attempt."""
    try:
        return _normalize_to_payload(raw)
    except (ValidationError, EmptyLLMResponseError) as error:
        return repair_invalid_payload(raw, error, repair_context)


_PIPELINE_OWNED_QUALITY_FLAGS = frozenset(
    {
        # These describe media/extraction state and require runtime evidence.
        "contains_legacy_media_placeholders",
        "transcript_unavailable",
        "timestamps_unavailable",
        "partial_segment_failure",
        "extraction_unstable",
        # Generic enrichment has no duplicate detector.  Do not let an LLM
        # turn repeated OCR frames into a duplicate-card assertion.
        "possible_duplicate",
    }
)


def _normalize_pipeline_quality_flags(payload: LLMPayload) -> None:
    """Keep runtime-owned technical status separate from LLM source flags."""
    substantive = any(
        (
            payload.summary.strip(),
            payload.key_points,
            payload.entities.model_dump(exclude_defaults=True),
            payload.topics,
            payload.theses,
            payload.quotes,
            payload.events,
            payload.search_phrases,
        )
    )
    flags = list(dict.fromkeys(payload.quality_flags))
    if substantive:
        flags = [flag for flag in flags if flag != "no_substantive_content"]
    # Technical statuses are assigned only by the responsible runtime/media
    # pipeline.  Generic LLM enrichment has no evidence to assign them.
    payload.quality_flags = [
        flag for flag in flags if flag not in _PIPELINE_OWNED_QUALITY_FLAGS
    ]


def _media_quality_flags(meta: NormalizedMeta) -> list[str]:
    """Return deterministic quality flags from native media metadata."""
    flags: list[str] = []
    for item in _media_entries(meta):
        operations = item.get("analysis_operations")
        if operations is not None and "transcribe" not in operations:
            continue
        media_type = str(item.get("media_type") or "").strip().lower()
        if media_type not in {"video", "audio", "voice"}:
            continue
        if str(item.get("download_status") or "").strip().lower() != "downloaded":
            continue
        status = str(item.get("transcription_status") or "").strip().lower()
        if status not in {"transcribed", "no_speech"}:
            flags.append("transcript_unavailable")
            break
    return flags


def _media_quality_issues(meta: NormalizedMeta) -> list[str]:
    """Return compact, non-secret media diagnostics for card provenance."""
    issues: list[str] = []
    for item in _media_entries(meta):
        operations = item.get("analysis_operations")
        if operations == []:
            continue
        media_type = str(item.get("media_type") or "").strip().lower()
        message_id = str(item.get("message_id") or "unknown")
        if media_type not in {"video", "audio", "voice", "image", "document"}:
            continue
        download_status = str(item.get("download_status") or "").strip().lower()
        if download_status and download_status != "downloaded":
            issues.append(f"media:{media_type}:{message_id}:download_{download_status}")
        if media_type in {"video", "audio", "voice"} and download_status == "downloaded" and (operations is None or "transcribe" in operations):
            status = str(item.get("transcription_status") or "").strip().lower()
            if status not in {"transcribed", "no_speech"}:
                issues.append(
                    f"media:{media_type}:{message_id}:transcription_{status or 'missing'}"
                )
        if media_type in {"image", "video"} and download_status == "downloaded" and (operations is None or "vision" in operations):
            visual_status = str(item.get("visual_status") or "").strip().lower()
            if visual_status not in {"success", "unavailable"}:
                issues.append(
                    f"media:{media_type}:{message_id}:visual_{visual_status or 'missing'}"
                )
        if media_type == "document" and item.get("text_extraction_status") != "success":
            issues.append(f"media:document:{message_id}:text_{item.get('text_extraction_status') or 'missing'}")
    return issues


def _media_entries(meta: NormalizedMeta) -> list[dict]:
    """Normalize permissive metadata media entries to dictionaries."""
    return [item for item in (meta.media or []) if isinstance(item, dict)]


def _validation_text(preprocessed: PreprocessedText) -> str:
    """Validate against source text plus the source header used by chunk merge."""
    return "\n\n".join(
        part.strip()
        for part in (preprocessed.header, preprocessed.clean_text)
        if part and part.strip()
    )


def _assemble_card(
    *,
    payload: LLMPayload,
    meta: NormalizedMeta,
    content_type: str,
    channel_name: str,
    msg_id: str,
    txt_path: Path,
    preprocessed: PreprocessedText,
    extraction_issues: list[str] | None = None,
) -> EnrichedCardV2:
    """Assemble the final EnrichedCardV2 from LLM payload + code-built fields."""
    provenance = Provenance(
        source_id=_build_source_id(meta, channel_name, msg_id),
        source_type=_source_type_from_content_type(content_type),
        channel=meta.channel_name or channel_name,
        date=meta.date or None,
        post_url=redact_sensitive_url(meta.post_url),
        message_id=meta.message_id or msg_id,
        forwarded_from=meta.forward_from_name,
        normalized_path=str(txt_path.relative_to(config.PROJECT_ROOT)),
    )

    source_chain = SourceChain(
        original_source=_detect_original_source(meta),
        forwarded_from=meta.forward_from_name,
        mentioned_sources=[],
        external_links=_collect_external_links(meta),
    )

    card = EnrichedCardV2(
        schema_version=config.ENRICHMENT_SCHEMA_VERSION,
        prompt_version=config.ENRICHMENT_PROMPT_VERSION,
        enrichment_model=llm_backend.active_model_for("enrichment"),
        media_contract_version=_MEDIA_CONTRACT_VERSION,
        enriched_at=datetime.now(UTC).isoformat(),
        provenance=provenance,
        content_type=content_type,
        language="ru",
        # LLM payload
        summary=payload.summary,
        key_points=payload.key_points,
        entities=payload.entities,
        topics=payload.topics,
        theses=payload.theses,
        quotes=payload.quotes,
        events=payload.events,
        search_phrases=payload.search_phrases,
        # Code-built
        source_chain=source_chain,
        graph_text="",
        search_text="",
        ignored_blocks=preprocessed.ignored_blocks,
        quality_flags=payload.quality_flags,
        extraction_issues=list(extraction_issues or []),
    )

    # Build graph_text and search_text
    card_dict = card.model_dump(mode="json")
    populate_graph_texts(card_dict)
    card = EnrichedCardV2.model_validate(card_dict)

    return card


def _handle_enrichment_result(
    job: _EnrichmentJob,
    result: dict | None,
    progress: dict,
    stats: EnrichmentStats,
) -> None:
    if result is None:
        stats.skipped_review += 1
        logger.warning("  Review result retained previous card: %s", job.progress_key)
        return

    card = result
    if card.pop("_repair_succeeded", False) and not card.pop("_repair_counted", False):
        stats.repaired += 1
    ct = card.get("content_type", "unknown")

    is_partial = (
        not card.get("summary")
        and not card.get("key_points")
        and job.txt_path.stat().st_size > 50
    )

    if is_partial:
        stats.partial += 1
        stats.partial_posts.append(job.progress_key)
        logger.warning(f"  Partial: {job.progress_key} -> {ct}")
    else:
        out_dir = job.out_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        temporary = job.out_path.with_name(f".{job.out_path.name}.tmp")
        temporary.write_text(
            json.dumps(card, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(job.out_path)

        failed = progress.get("failed")
        if not isinstance(failed, dict):
            failed = {}
            progress["failed"] = failed
        failed.pop(job.progress_key, None)
        progress["enriched"][job.progress_key] = {
            "enriched_at": datetime.now(UTC).isoformat(),
            "source_fingerprint": job.source_fingerprint,
            "schema_version": config.ENRICHMENT_SCHEMA_VERSION,
            "prompt_version": config.ENRICHMENT_PROMPT_VERSION,
            "enrichment_model": llm_backend.active_model_for("enrichment"),
            "media_contract_version": _MEDIA_CONTRACT_VERSION,
        }
        stats.enriched += 1
        logger.info(f"  Enriched: {job.progress_key} -> {ct}")

        # The caller owns progress mutation; write it here after the card is
        # safely published so an interrupted batch does not repeat the job.
        _save_progress(progress)

    if is_partial:
        logger.warning("  Partial result retained previous card: %s", job.progress_key)

    stats.by_content_type[ct] = stats.by_content_type.get(ct, 0) + 1


def _record_enrichment_failure(
    job: _EnrichmentJob,
    exc: Exception,
    stats: EnrichmentStats,
    progress: dict | None = None,
) -> None:
    stats.failed += 1
    failure = {
        "status": "failed",
        "failed_at": datetime.now(UTC).isoformat(),
        "source_fingerprint": job.source_fingerprint,
        "exception_type": type(exc).__name__,
        "error": _safe_failure_message(exc),
        "attempts": int(getattr(exc, "enrichment_attempts", 1)),
    }
    if progress is not None:
        failed = progress.get("failed")
        if not isinstance(failed, dict):
            failed = {}
            progress["failed"] = failed
        previous = failed.get(job.progress_key)
        if isinstance(previous, dict):
            try:
                failure["attempts"] = int(previous.get("attempts", 0)) + 1
            except (TypeError, ValueError):
                failure["attempts"] = 1
        review_path = _queue_enrichment_failure(job, exc)
        if review_path:
            failure["review_path"] = review_path
            failure["status"] = "review"
        failed[job.progress_key] = failure
        _save_progress(progress)
    logger.error(
        f"  Failed to enrich {job.progress_key}: {exc}",
        exc_info=True,
    )


def _safe_failure_message(exc: Exception) -> str:
    """Keep failure diagnostics useful without persisting sensitive URLs."""
    message = str(exc).strip() or type(exc).__name__
    return redact_sensitive_url(message).replace("\r", " ").replace("\n", " ")[:500]


def _queue_enrichment_failure(job: _EnrichmentJob, exc: Exception) -> str | None:
    """Persist a final enrichment failure only for real normalized inputs."""
    try:
        job.txt_path.resolve().relative_to(config.NORMALIZED_DIR.resolve())
    except (OSError, ValueError, AttributeError):
        # Unit-test fixtures and ad-hoc callers should not write into the
        # repository's review queue.
        return None

    try:
        meta = json.loads(job.meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        meta = {}
    try:
        message_text = job.txt_path.read_text(encoding="utf-8")[:500]
    except (OSError, UnicodeError):
        message_text = ""

    try:
        message_id = int(job.msg_id)
    except (TypeError, ValueError):
        message_id = 0
    message_date = None
    date_str = meta.get("date") if isinstance(meta, dict) else None
    if date_str:
        try:
            message_date = datetime.fromisoformat(str(date_str))
        except (TypeError, ValueError):
            pass

    post_url = ""
    if isinstance(meta, dict):
        post_url = redact_sensitive_url(str(meta.get("post_url") or ""))
    result = queue_review_item(
        review_type=REVIEW_TYPE_ENRICHMENT_FAILURE,
        channel_name=job.channel_name,
        message_id=message_id,
        message_text=message_text,
        message_date=message_date,
        url=post_url,
        reason=f"Enrichment failed after bounded retry: {_safe_failure_message(exc)}",
        normalized_filepath=str(job.txt_path),
    )
    return result.filepath


def _enrichment_concurrency() -> int:
    try:
        return max(1, int(config.ENRICHMENT_CONCURRENCY))
    except (TypeError, ValueError):
        return 1


# ── Postprocessor helpers ─────────────────────────────────────────────────────

def _build_source_id(meta: NormalizedMeta, channel_name: str, msg_id: str) -> str:
    existing = meta.source_id
    if existing is not None:
        return existing.value

    channel_id = meta.channel_id
    message_id = meta.message_id or msg_id
    if channel_id and message_id:
        return f"telegram:{channel_id}:{message_id}"
    if channel_name and message_id:
        return f"telegram:{channel_name}:{message_id}"
    raise ValueError("Could not build stable source_id from normalized metadata")


def _source_type_from_content_type(content_type: str) -> str:
    if content_type in ("telegram_post", "telegram_forward"):
        return content_type
    if content_type == "youtube_transcript":
        return "youtube_transcript"
    if content_type == "instagram_text":
        return "instagram_text"
    if content_type == "web_article_text":
        return "web_article_text"
    return "mixed_normalized_text"


def _detect_original_source(meta: NormalizedMeta) -> str | None:
    if meta.is_forward and meta.forward_from_name:
        return meta.forward_from_name
    return meta.channel_name or None


def _collect_external_links(meta: NormalizedMeta) -> list[dict[str, str]]:
    links = []
    for url in meta.youtube_urls:
        links.append({"url": redact_sensitive_url(url), "label": "YouTube video"})
    for url in meta.instagram_urls:
        links.append({"url": redact_sensitive_url(url), "label": "Instagram post"})
    for url in meta.web_urls:
        links.append({"url": redact_sensitive_url(url), "label": "External link"})
    return links


def _queue_uninformative_review(
    triage_reason: str,
    txt_path: Path,
    channel_name: str,
    msg_id: str,
    meta: dict,
) -> None:
    try:
        message_text = txt_path.read_text(encoding="utf-8")[:500]
    except OSError:
        message_text = ""

    try:
        mid = int(msg_id)
    except ValueError:
        mid = 0

    date_str = meta.get("date")
    message_date = None
    if date_str:
        try:
            message_date = datetime.fromisoformat(date_str)
        except (ValueError, TypeError):
            pass

    queue_review_item(
        review_type=REVIEW_TYPE_UNINFORMATIVE,
        channel_name=channel_name,
        message_id=mid,
        message_text=message_text,
        message_date=message_date,
        reason=triage_reason,
        normalized_filepath=str(txt_path),
    )


# ── Progress tracking ──────────────────────────────────────────────────────

_PROGRESS_FILE = config.STATE_DIR / "enrichment_progress.json"


def _load_progress() -> dict:
    if _PROGRESS_FILE.exists():
        try:
            progress = json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
            if isinstance(progress, dict):
                if not isinstance(progress.get("enriched"), dict):
                    progress["enriched"] = {}
                if not isinstance(progress.get("failed"), dict):
                    progress["failed"] = {}
                return progress
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load enrichment progress: {e}")
    return {"last_run": None, "enriched": {}, "failed": {}}


def _save_progress(progress: dict) -> None:
    progress["last_run"] = datetime.now(UTC).isoformat()
    temporary = _PROGRESS_FILE.with_name(f".{_PROGRESS_FILE.name}.tmp")
    temporary.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(_PROGRESS_FILE)


def _needs_enrichment(
    progress: dict,
    key: str,
    source_fingerprint: str,
) -> bool:
    if key in progress.get("failed", {}):
        return True
    prev = progress.get("enriched", {}).get(key)
    if prev is None:
        return True
    if prev.get("source_fingerprint") != source_fingerprint:
        return True
    if prev.get("schema_version") != config.ENRICHMENT_SCHEMA_VERSION:
        return True
    if prev.get("prompt_version") != config.ENRICHMENT_PROMPT_VERSION:
        return True
    if prev.get("media_contract_version") != _MEDIA_CONTRACT_VERSION:
        return True
    if llm_backend.model_change_requires_regeneration(
        str(prev.get("enrichment_model") or ""), "enrichment"
    ):
        return True
    return False


def _migrate_non_media_contract(
    progress: dict,
    key: str,
    source_fingerprint: str,
    path: Path,
    expected_source_id: str | None,
) -> bool:
    """Stamp media-contract-v2 without an LLM call when a source has no media.

    All semantic inputs and valid provenance must already match. A changed
    fingerprint or any structural mismatch stays on the normal enrichment path.
    """
    if key in progress.get("failed", {}):
        return False
    prev = progress.get("enriched", {}).get(key)
    if not isinstance(prev, dict) or prev.get("source_fingerprint") != source_fingerprint:
        return False
    if prev.get("schema_version") != config.ENRICHMENT_SCHEMA_VERSION:
        return False
    if prev.get("prompt_version") != config.ENRICHMENT_PROMPT_VERSION:
        return False
    if llm_backend.model_change_requires_regeneration(
        str(prev.get("enrichment_model") or ""), "enrichment"
    ):
        return False
    try:
        card = EnrichedCardV2.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError):
        return False
    if (
        card.schema_version != config.ENRICHMENT_SCHEMA_VERSION
        or card.prompt_version != config.ENRICHMENT_PROMPT_VERSION
        or llm_backend.model_change_requires_regeneration(card.enrichment_model, "enrichment")
        or not expected_source_id
        or card.provenance.source_id != expected_source_id
        or "extraction_unstable" in card.quality_flags
        or not (card.summary.strip() or card.key_points)
    ):
        return False
    if card.media_contract_version != _MEDIA_CONTRACT_VERSION:
        card.media_contract_version = _MEDIA_CONTRACT_VERSION
        temporary = path.with_name(f".{path.name}.media-contract.tmp")
        temporary.write_text(
            json.dumps(card.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    prev["media_contract_version"] = _MEDIA_CONTRACT_VERSION
    return True


def _is_valid_enriched_output(path: Path, expected_source_id: str | None) -> bool:
    """Return whether an output file is safe to treat as a completed card."""
    try:
        card = EnrichedCardV2.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError):
        return False
    if card.schema_version != config.ENRICHMENT_SCHEMA_VERSION:
        return False
    if card.prompt_version != config.ENRICHMENT_PROMPT_VERSION:
        return False
    if card.media_contract_version != _MEDIA_CONTRACT_VERSION:
        return False
    if llm_backend.model_change_requires_regeneration(card.enrichment_model, "enrichment"):
        return False
    if not expected_source_id:
        return False
    if card.provenance.source_id != expected_source_id:
        return False
    if "extraction_unstable" in card.quality_flags or not (card.summary.strip() or card.key_points):
        return False
    return True


def _source_fingerprint(txt_path: Path, meta_path: Path) -> str:
    """Hash both normalized inputs so metadata-only changes invalidate a card."""
    digest = hashlib.sha256()
    for label, path in ((b"text", txt_path), (b"meta", meta_path)):
        data = path.read_bytes()
        digest.update(label)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()
