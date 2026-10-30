"""Episode/segment enrichment for normalized YouTube sources."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from geospoiler import config
from geospoiler.contracts.models import EnrichedCardV2, LLMPayload, Provenance, SourceChain, YouTubeSegmentCardV2
from geospoiler.ingestion.enricher.graph_text_builder import populate_graph_texts
from geospoiler.ingestion.enricher.llm_enricher import (
    EmptyLLMResponseError,
    _normalize_to_payload,
    extract_chunk_raw,
    extract_full_post_raw,
    merge_chunk_results_raw,
)
from geospoiler.ingestion.enricher.preprocessor import PreprocessedText
from geospoiler.ingestion.enricher.repair import RepairContext, repair_if_needed, repair_invalid_payload
from geospoiler.ingestion.enricher.validator import (
    drop_invalid_optional_items,
    has_required_language_violations,
    validate_payload,
)
from geospoiler.ingestion.enricher.youtube_segmenter import (
    SEGMENT_MAX_CHARS,
    SEGMENT_MIN_CHARS,
    SEGMENT_TARGET_CHARS,
    SEGMENT_THRESHOLD_CHARS,
    SEGMENT_THRESHOLD_SECONDS,
    SegmentSpec,
    build_segment_specs,
    needs_youtube_segments,
    youtube_timestamp_url,
)
from geospoiler.ingestion.normalizer.review_queue import REVIEW_TYPE_EXTERNAL_LINK
from geospoiler.ingestion.normalizer.review_queue import queue_item as queue_review_item
from geospoiler.ingestion.normalizer.youtube_handler import is_valid_youtube_url, redact_sensitive_url
from geospoiler.providers import llm_backend

logger = logging.getLogger("geospoiler.enricher.youtube")

# Keep merge requests comfortably below the Codex CLI timeout.  The public
# configuration remains part of the source fingerprint, while this safety cap
# prevents a large episode from putting several long segment payloads into one
# model request.
_SAFE_MERGE_BATCH_CHARS = 24_000
_COMPACT_MERGE_BATCH_CHARS = 18_000
_PIPELINE_OWNED_QUALITY_FLAGS = frozenset(
    {
        "extraction_unstable",
        "partial_segment_failure",
        "transcript_unavailable",
        "timestamps_unavailable",
    }
)
# One worker per video at a time, so a copy waits for the first generation.
_video_locks: dict[str, threading.Lock] = {}
_video_locks_guard = threading.Lock()


@dataclass
class YouTubeEnrichmentStats:
    sources_scanned: int = 0
    episodes_written: int = 0
    episodes_skipped: int = 0
    segments_written: int = 0
    partial_sources: int = 0
    failed_sources: int = 0
    duplicate_episodes: int = 0


@dataclass(frozen=True)
class YouTubeEnrichmentResult:
    written: bool
    skipped: bool
    segments_written: int = 0
    partial_failure: bool = False
    review_required: bool = False
    duplicate: bool = False


@dataclass(frozen=True)
class DedicatedYouTubeArtifact:
    metadata_path: Path
    text_path: Path
    cues_path: Path
    metadata: dict
    transcript_text: str
    cues: list
    video_id: str


@dataclass(frozen=True)
class GenerationValidation:
    valid: bool
    violations: tuple[str, ...] = ()
    segment_ids: tuple[str, ...] = ()


def enrich_youtube_all(
    *,
    channel_filter: str | None = None,
    force: bool = False,
    metadata_paths: list[Path] | None = None,
    legacy_meta_paths: list[Path] | None = None,
) -> YouTubeEnrichmentStats:
    """Enrich dedicated YouTube artifacts and legacy normalized YouTube posts."""
    discovery_errors: list[tuple[Path, str]] = []
    sources = _iter_youtube_sources(channel_filter=channel_filter, metadata_paths=metadata_paths,
                                    legacy_meta_paths=legacy_meta_paths, seen_keys=set(), discovery_errors=discovery_errors)
    return _enrich_youtube_sources(sources, force=force, discovery_errors=discovery_errors)


def _iter_youtube_sources(*, channel_filter, metadata_paths, legacy_meta_paths, seen_keys, discovery_errors):
    for source in _iter_dedicated_sources(channel_filter, discovery_errors, metadata_paths):
        key = _source_key(source)
        if key not in seen_keys:
            seen_keys.add(key)
            yield source
    # Keep dedicated-source preference and occurrence identity across the
    # entire stream, not a separate seen-set inside every worker invocation.
    for source in _iter_legacy_sources(channel_filter, discovery_errors, legacy_meta_paths):
        key = _source_key(source)
        if key not in seen_keys:
            seen_keys.add(key)
            yield source


def _enrich_youtube_sources(sources, *, force: bool, discovery_errors) -> YouTubeEnrichmentStats:
    stats = YouTubeEnrichmentStats()
    for source in sources:
        stats.sources_scanned += 1
        try:
            result = _enrich_source(source, force=force)
            if result.written:
                stats.episodes_written += 1
            if result.skipped:
                stats.episodes_skipped += 1
            stats.segments_written += result.segments_written
            if result.partial_failure:
                stats.partial_sources += 1
            if result.duplicate:
                stats.duplicate_episodes += 1
            if result.review_required:
                _queue_youtube_failure(source, "partial or unstable extraction")
        except Exception as exc:
            stats.failed_sources += 1
            logger.exception("YouTube enrichment failed for %s: %s", source.get("url"), exc)
            _queue_youtube_failure(source, str(exc))
    for metadata_path, reason in discovery_errors:
        stats.failed_sources += 1
        logger.error("Skipping invalid YouTube artifact %s: %s", metadata_path, reason)
        _queue_artifact_discovery_failure(metadata_path, reason)
    return stats


def _enrich_source(source: dict, *, force: bool) -> YouTubeEnrichmentResult:
    video_id = str(source.get("video_id") or "")
    with _video_locks_guard:
        lock = _video_locks.setdefault(video_id, threading.Lock())
    with lock:
        # A forced run re-extracts every video, so it never copies a generation.
        root = None if force else _video_root(source)
        if root is not None and _segmentation_inputs(source) != _segmentation_inputs(root[0]):
            source = _adopt_root_transcript(source, root[0])
        return _enrich_video_source(source, force=force, root=root)


def _enrich_video_source(
    source: dict,
    *,
    force: bool,
    root: tuple[dict, EnrichedCardV2] | None = None,
) -> YouTubeEnrichmentResult:
    transcript = str(source.get("transcript_text") or "").strip()
    if not transcript or source.get("transcript_source") in {"description", "unavailable"}:
        raise ValueError("YouTube transcript is unavailable")

    card_path = _episode_card_path(source)
    long_form, specs = _segment_plan(source, transcript)

    fingerprint = _source_fingerprint(source, transcript)
    checkpoint_fingerprint = _checkpoint_fingerprint(fingerprint)
    if not force and _is_current(card_path, source, fingerprint, specs) and (
        root is None or _published_duplicate_of(card_path) == root[1].provenance.source_id
    ):
        _cleanup_checkpoint_family(source, keep_fingerprint=None)
        return YouTubeEnrichmentResult(
            written=False,
            skipped=True,
            segments_written=0,
        )

    if root is not None:
        return _publish_duplicate(source, *root, card_path, specs, fingerprint, long_form)

    if specs:
        _initialize_checkpoint(source, checkpoint_fingerprint, specs)

    segment_payloads: list[dict] = []
    segment_cards: list[YouTubeSegmentCardV2] = []
    partial_failure = False
    if specs:
        for spec in specs:
            cached_segment = _load_reusable_segment(
                source,
                spec,
                fingerprint,
                checkpoint_fingerprint=checkpoint_fingerprint,
            )
            if cached_segment is not None:
                segment_cards.append(cached_segment)
                segment_payloads.append(_segment_card_payload(cached_segment, spec))
                continue
            try:
                payload, issues = _extract_segment_payload(spec, len(specs))
            except Exception as exc:
                payload = LLMPayload(quality_flags=["extraction_unstable"])
                issues = [f"segment_{spec.index}_extraction_failed: {exc}"]
            # A failed segment, including a persistent language breach, publishes
            # without semantic fields and is retried on the next run.
            if issues or "extraction_unstable" in payload.quality_flags:
                partial_failure = True
                logger.warning(
                    "YouTube segment %s of %s failed for %s: %s",
                    spec.index, len(specs), source.get("video_id") or source.get("url"), "; ".join(issues)[:500],
                )
            segment_cards.append(_build_segment_card(source, spec, payload, issues))
            if not issues and "extraction_unstable" not in payload.quality_flags:
                successful_card = segment_cards[-1]
                _checkpoint_segment(source, checkpoint_fingerprint, successful_card)
                segment_payloads.append({
                    **payload.model_dump(mode="json"),
                    "char_range": list(spec.char_range),
                })

        if segment_payloads:
            payload, issues = _merge_episode_payload(source, transcript, segment_payloads)
        else:
            payload = LLMPayload(quality_flags=["extraction_unstable"])
            issues = ["episode_has_no_successful_segment_extractions"]
    else:
        payload, issues = _extract_full_payload(transcript, source.get("title") or "YouTube")

    if transcript.strip() and not payload.summary.strip() and not payload.key_points:
        if segment_payloads:
            fallback = _deterministic_episode_payload(segment_payloads)
            if fallback.summary or fallback.key_points:
                logger.warning(
                    "YouTube episode extraction was empty; using deterministic segment fallback for %s",
                    source.get("video_id") or source.get("url"),
                )
                payload, issues = fallback, []
        if not payload.summary.strip() and not payload.key_points:
            raise ValueError(
                "YouTube extraction produced no usable summary or key_points for substantive transcript"
            )

    if has_required_language_violations(issues):
        raise ValueError(
            "YouTube extraction still violates the Russian-language contract after repair"
        )
    if issues and "extraction_unstable" in payload.quality_flags:
        partial_failure = True
    if partial_failure and "partial_segment_failure" not in payload.quality_flags:
        payload.quality_flags.append("partial_segment_failure")
    source["partial_segment_failure"] = partial_failure
    card = _build_episode_card(source, payload, issues)
    _publish_generation(
        source,
        card_path,
        card,
        segment_cards,
        fingerprint,
        long_form,
    )
    if specs:
        _remove_checkpoint(source, checkpoint_fingerprint)
        _cleanup_checkpoint_family(source, keep_fingerprint=None)
    return YouTubeEnrichmentResult(
        written=True,
        skipped=False,
        segments_written=len(segment_cards),
        partial_failure=partial_failure,
        review_required=partial_failure or "extraction_unstable" in card.quality_flags,
    )


def _segment_plan(source: dict, transcript: str) -> tuple[bool, list[SegmentSpec]]:
    long_form = needs_youtube_segments(transcript, _number(source.get("duration_seconds")))
    specs = build_segment_specs(
        transcript,
        cues=source.get("cues") or [],
        chapters=source.get("chapters") or [],
    ) if long_form else []
    return long_form, specs


def _segmentation_inputs(source: dict) -> tuple:
    return (
        str(source.get("transcript_text") or "").strip(),
        source.get("cues") or [],
        source.get("chapters") or [],
        source.get("duration_seconds"),
    )


def _video_root(source: dict) -> tuple[dict, EnrichedCardV2] | None:
    """The root generation of this video when another post holds it.

    Every post of one video is a copy of the earliest post whose current
    generation is not itself a copy. None when this post is that root, when no
    current generation exists yet, or for a legacy source without an artifact.
    """
    normalized_root = config.YOUTUBE_NORMALIZED_DIR
    if not source.get("metadata_path") or not normalized_root.exists():
        return None
    metadata_paths = sorted(normalized_root.rglob(f"{_safe_component(source.get('video_id'))}.youtube.meta.json"))
    roots = []
    for other in _iter_dedicated_sources(None, metadata_paths=metadata_paths):
        if other["video_id"] != source.get("video_id"):
            continue
        card = _current_episode_card(other)
        if card is not None and card.duplicate_of is None:
            roots.append((other, card))
    if not roots:
        return None
    root = min(roots, key=lambda item: (str(item[0].get("telegram_date") or ""), item[1].provenance.source_id))
    return None if _source_key(root[0]) == _source_key(source) else root


def _current_episode_card(source: dict) -> EnrichedCardV2 | None:
    transcript = str(source.get("transcript_text") or "").strip()
    _, specs = _segment_plan(source, transcript)
    card_path = _episode_card_path(source)
    if not _is_current(card_path, source, _source_fingerprint(source, transcript), specs):
        return None
    try:
        return EnrichedCardV2.model_validate_json(card_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError):
        return None


def _published_duplicate_of(card_path: Path) -> str | None:
    try:
        return EnrichedCardV2.model_validate_json(card_path.read_text(encoding="utf-8")).duplicate_of
    except (OSError, UnicodeError, ValidationError):
        return None


def _adopt_root_transcript(source: dict, root: dict) -> dict:
    """Save the root's transcript for this post, so the copy's segments match the root's.

    Two downloads of one video may differ slightly (subtitles, Whisper runs).
    The post's own transcript files stay on disk; its metadata stops naming them.
    """
    metadata_path = Path(source["metadata_path"])
    own = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata = json.loads(Path(root["metadata_path"]).read_text(encoding="utf-8"))
    for key in ("transcript_path", "cues_path"):
        if metadata.get(key):
            target = metadata_path.parent / Path(metadata[key]).name
            shutil.copyfile(config.resolve_project_path(metadata[key]), target)
            metadata[key] = str(target.relative_to(config.PROJECT_ROOT))
    metadata["telegram_source"] = own.get("telegram_source")
    temporary = metadata_path.with_name(f".{metadata_path.name}.tmp")
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(metadata_path)
    logger.info("YouTube %s now uses the transcript of %s", _episode_source_id(source), _episode_source_id(root))
    adopted = next(_iter_dedicated_sources(None, metadata_paths=[metadata_path]), None)
    if adopted is None:
        raise ValueError("YouTube artifact is invalid after adopting the root transcript")
    return adopted


def _publish_duplicate(
    source: dict,
    donor: dict,
    donor_card: EnrichedCardV2,
    card_path: Path,
    specs: list[SegmentSpec],
    fingerprint: str,
    long_form: bool,
) -> YouTubeEnrichmentResult:
    """Publish the donor's episode and segments under this post without LLM calls."""
    root_id = donor_card.duplicate_of or donor_card.provenance.source_id
    segment_cards: list[YouTubeSegmentCardV2] = []
    for spec in specs:
        segment_path = _segment_dir(donor) / f"{spec.index:04d}.youtube-segment.json"
        donor_segment = YouTubeSegmentCardV2.model_validate_json(segment_path.read_text(encoding="utf-8"))
        payload = _segment_card_payload(donor_segment, spec)
        payload.pop("char_range")
        segment_cards.append(_build_segment_card(
            source, spec, LLMPayload.model_validate(payload), list(donor_segment.extraction_issues),
            duplicate_of=root_id,
        ))
    dumped = donor_card.model_dump(mode="json")
    payload = LLMPayload.model_validate({
        **{name: dumped[name] for name in _payload_fields()},
        "quality_flags": _without_pipeline_owned_quality_flags(donor_card.quality_flags),
    })
    card = _build_episode_card(source, payload, list(donor_card.extraction_issues), duplicate_of=root_id)
    _publish_generation(source, card_path, card, segment_cards, fingerprint, long_form)
    _cleanup_checkpoint_family(source, keep_fingerprint=None)
    logger.info("YouTube %s is a copy of %s; reused its generation", _episode_source_id(source), root_id)
    return YouTubeEnrichmentResult(
        written=True,
        skipped=False,
        segments_written=len(segment_cards),
        duplicate=True,
    )


def _extract_segment_payload(spec: SegmentSpec, total: int) -> tuple[LLMPayload, list[str]]:
    raw = extract_chunk_raw(spec.text, spec.index, total)
    return _finish_payload(raw, spec.text)


def _extract_full_payload(text: str, title: str) -> tuple[LLMPayload, list[str]]:
    preprocessed = PreprocessedText(
        header=f"[YouTube: {title}]",
        clean_text=text,
        body_char_count=len(text),
    )
    raw = extract_full_post_raw(preprocessed, "youtube_transcript")
    return _finish_payload(raw, text)


def _merge_episode_payload(
    source: dict,
    transcript: str,
    segment_payloads: list[dict],
) -> tuple[LLMPayload, list[str]]:
    if len(segment_payloads) >= 6:
        if _published_episode_needs_deterministic_fallback(source):
            fallback = _deterministic_episode_payload(segment_payloads)
            if fallback.summary or fallback.key_points:
                logger.warning(
                    "Rebuilding unstable YouTube episode from validated segments for %s",
                    source.get("video_id") or source.get("url"),
                )
                return fallback, []
        payload, issues = _compact_merge_episode_payload(source, transcript, segment_payloads)
        if issues or "extraction_unstable" in payload.quality_flags:
            fallback = _deterministic_episode_payload(segment_payloads)
            if fallback.summary or fallback.key_points:
                logger.warning(
                    "YouTube episode merge failed; publishing deterministic segment fallback for %s",
                    source.get("video_id") or source.get("url"),
                )
                return fallback, []
        return payload, issues
    if len(segment_payloads) == 1:
        payload = LLMPayload.model_validate(
            {key: segment_payloads[0].get(key) for key in _payload_fields()}
        )
        _normalize_quality_flags(payload)
        issues: list[str] = []
    else:
        current = segment_payloads
        title = str(source.get("title") or "YouTube episode")
        issues = []
        while len(current) > 1:
            next_level: list[dict] = []
            for batch in _merge_batches(
                current,
                min(config.YOUTUBE_MERGE_MAX_CHARS, _SAFE_MERGE_BATCH_CHARS),
            ):
                if len(batch) == 1:
                    merged = LLMPayload.model_validate(
                        {key: batch[0].get(key) for key in _payload_fields()}
                    )
                    _normalize_quality_flags(merged)
                    batch_issues: list[str] = []
                else:
                    raw = merge_chunk_results_raw(title, batch)
                    merged, batch_issues = _finish_payload(raw, transcript)
                if batch_issues or "extraction_unstable" in merged.quality_flags:
                    fallback = _deterministic_episode_payload(segment_payloads)
                    if fallback.summary or fallback.key_points:
                        logger.warning(
                            "YouTube episode merge failed; publishing deterministic segment fallback for %s",
                            source.get("video_id") or source.get("url"),
                        )
                        return fallback, []
                    return LLMPayload(quality_flags=["extraction_unstable"]), batch_issues or [
                        "youtube_merge_batch_unstable"
                    ]
                next_level.append(merged.model_dump(mode="json"))
            current = next_level
        payload = LLMPayload.model_validate(
            {key: current[0].get(key) for key in _payload_fields()}
        )
        _normalize_quality_flags(payload)
    if not payload.summary and not payload.key_points:
        good = next((item for item in segment_payloads if item.get("summary") or item.get("key_points")), None)
        if good:
            payload = LLMPayload.model_validate({key: good.get(key) for key in _payload_fields()})
            payload.quality_flags.append("extraction_unstable")
            issues.append("episode_merge_empty_used_segment_fallback")
    return payload, issues


def _compact_merge_episode_payload(
    source: dict,
    transcript: str,
    segment_payloads: list[dict],
) -> tuple[LLMPayload, list[str]]:
    """Merge long episodes with small semantic prompts and deterministic unions.

    Full segment payloads contain verbose entities, quotes, and search phrases.
    Sending all of them through every merge level is both slow and prone to the
    Codex CLI timeout. Summaries and semantic claims are merged by Luna; the
    retrieval-oriented fields are already validated per segment and can be
    unioned losslessly afterwards.
    """
    title = str(source.get("title") or "YouTube episode")
    compact_fields = ("summary", "key_points", "topics", "theses", "events", "quality_flags")
    current = [
        {key: segment.get(key) for key in compact_fields}
        for segment in segment_payloads
    ]
    issues: list[str] = []
    while len(current) > 1:
        next_level: list[dict] = []
        for batch in _merge_batches(current, _COMPACT_MERGE_BATCH_CHARS):
            if len(batch) == 1:
                # Compact items omit retrieval fields; those are restored below.
                merged = LLMPayload.model_validate(
                    {key: batch[0][key] for key in _payload_fields() if key in batch[0]}
                )
                _normalize_quality_flags(merged)
                batch_issues: list[str] = []
            else:
                raw = merge_chunk_results_raw(title, batch)
                merged, batch_issues = _finish_payload(raw, transcript)
            if batch_issues or "extraction_unstable" in merged.quality_flags:
                return LLMPayload(quality_flags=["extraction_unstable"]), batch_issues or [
                    "youtube_compact_merge_unstable"
                ]
            next_level.append(merged.model_dump(mode="json"))
        current = next_level

    payload = LLMPayload.model_validate(
        {key: current[0].get(key) for key in _payload_fields()}
    )
    _normalize_quality_flags(payload)
    return _restore_segment_metadata(payload, segment_payloads), issues


def _restore_segment_metadata(payload: LLMPayload, segment_payloads: list[dict]) -> LLMPayload:
    """Union validated retrieval fields omitted from compact merge prompts."""
    data = payload.model_dump(mode="json")
    entity_categories = (
        "people", "organizations", "countries", "locations", "military_units",
        "equipment", "weapons", "programs_projects", "media_sources", "other",
    )
    entities = {category: [] for category in entity_categories}
    for category in entity_categories:
        seen: set[str] = set()
        for segment in segment_payloads:
            for item in (segment.get("entities") or {}).get(category) or []:
                key = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    entities[category].append(item)
    data["entities"] = entities
    for field in ("quotes", "search_phrases"):
        values: list[dict] = []
        seen: set[str] = set()
        for segment in segment_payloads:
            for item in segment.get(field) or []:
                key = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    values.append(item)
        data[field] = values
    return LLMPayload.model_validate(data)


def _deterministic_episode_payload(segment_payloads: list[dict]) -> LLMPayload:
    """Build an episode payload from validated segment payloads without new claims.

    This is the last-resort path for a merge timeout/invalid response. Every
    value comes from a segment that already passed the normal extraction and
    contract validators; the fallback only concatenates summaries and unions
    structured retrieval fields.
    """
    summaries: list[str] = []
    for segment in segment_payloads:
        summary = str(segment.get("summary") or "").strip()
        if summary and summary not in summaries:
            summaries.append(summary)
    data: dict[str, object] = {"summary": "\n\n".join(summaries)}
    for field in ("key_points", "topics", "theses", "events"):
        values: list[dict] = []
        seen: set[str] = set()
        for segment in segment_payloads:
            for item in segment.get(field) or []:
                key = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    values.append(item)
        data[field] = values

    flags: list[str] = []
    for segment in segment_payloads:
        for flag in _without_pipeline_owned_quality_flags(segment.get("quality_flags") or []):
            if flag not in flags:
                flags.append(flag)
    data["quality_flags"] = flags
    payload = LLMPayload.model_validate(data)
    return _restore_segment_metadata(payload, segment_payloads)


def _published_episode_needs_deterministic_fallback(source: dict) -> bool:
    """Identify an existing published episode that cannot be reused as-is."""
    try:
        card = EnrichedCardV2.model_validate_json(
            _episode_card_path(source).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError):
        return False
    return (
        not card.summary.strip()
        or not card.key_points
        or bool({"extraction_unstable", "partial_segment_failure"} & set(card.quality_flags))
    )


def _merge_batches(payloads: list[dict], max_chars: int) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for payload in payloads:
        payload_chars = len(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        # Keep a singleton with its next item so every merge level makes
        # progress. A single oversized payload is still bounded by the pair,
        # while the safety cap prevents the original 4+ segment requests.
        if current and current_chars + payload_chars > max_chars and len(current) > 1:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(payload)
        current_chars += payload_chars
    if current:
        batches.append(current)
    return batches


def _finish_payload(raw: object, source_text: str) -> tuple[LLMPayload, list[str]]:
    repair_context = RepairContext()
    issues: list[str] = []
    try:
        payload = _normalize_to_payload(raw)
    except (ValidationError, EmptyLLMResponseError) as exc:
        try:
            payload = repair_invalid_payload(raw, exc, repair_context)
        except Exception as repair_exc:
            return LLMPayload(quality_flags=["extraction_unstable"]), [str(repair_exc)]

    _normalize_quality_flags(payload)
    validation = validate_payload(payload, source_text)
    if not validation.is_valid:
        try:
            payload, _ = repair_if_needed(
                payload,
                validation,
                source_text,
                context=repair_context,
            )
        except Exception as repair_exc:
            return LLMPayload(quality_flags=["extraction_unstable"]), [str(repair_exc)]
        _normalize_quality_flags(payload)
        final_validation = validate_payload(payload, source_text)
        if not final_validation.is_valid and drop_invalid_optional_items(payload, final_validation):
            final_validation = validate_payload(payload, source_text)
            if final_validation.is_valid:
                return payload, []
        if not final_validation.is_valid:
            issues = list(dict.fromkeys(final_validation.violations))
            return LLMPayload(quality_flags=["extraction_unstable"]), issues
        issues = []
    return payload, issues


def _build_segment_card(
    source: dict,
    spec: SegmentSpec,
    payload: LLMPayload,
    issues: list[str],
    *,
    duplicate_of: str | None = None,
) -> YouTubeSegmentCardV2:
    parent_id = _episode_source_id(source)
    start = spec.start_seconds
    end = spec.end_seconds
    flags = list(payload.quality_flags)
    if start is None and "timestamps_unavailable" not in flags:
        flags.append("timestamps_unavailable")
    return YouTubeSegmentCardV2(
        schema_version="youtube_segment_v2",
        enrichment_model=llm_backend.active_model_for("enrichment"),
        segment_id=f"{parent_id}:segment:{spec.index}",
        parent_source_id=parent_id,
        video_id=str(source["video_id"]),
        segment_index=spec.index,
        title=str(source.get("title") or ""),
        start_seconds=start,
        end_seconds=end,
        start_url=youtube_timestamp_url(str(source.get("url") or ""), start),
        chapter_titles=list(spec.chapter_titles),
        char_range=list(spec.char_range),
        transcript_text=spec.text,
        summary=payload.summary,
        key_points=payload.key_points,
        entities=payload.entities,
        topics=payload.topics,
        theses=payload.theses,
        quotes=payload.quotes,
        events=payload.events,
        search_phrases=payload.search_phrases,
        search_text=_build_segment_search_text(payload, spec.text),
        quality_flags=list(dict.fromkeys(flags)),
        extraction_issues=issues,
        duplicate_of=duplicate_of,
    )


def _build_episode_card(
    source: dict,
    payload: LLMPayload,
    issues: list[str],
    *,
    duplicate_of: str | None = None,
) -> EnrichedCardV2:
    flags = list(payload.quality_flags)
    if not source.get("cues") and "timestamps_unavailable" not in flags:
        flags.append("timestamps_unavailable")
    source_id = _episode_source_id(source)
    provenance = Provenance(
        source_id=source_id,
        source_type="youtube",
        channel=str(source.get("channel") or ""),
        date=source.get("telegram_date"),
        post_url=redact_sensitive_url(str(source.get("url") or "")),
        message_id=source.get("message_id"),
        normalized_path=str(Path(source["text_path"]).relative_to(config.PROJECT_ROOT)),
        source_title=str(source.get("title") or ""),
        parent_source_id=_telegram_source_id(source),
    )
    source_chain = SourceChain(
        original_source=str(source.get("channel") or "YouTube"),
        external_links=[
            {"label": "YouTube video", "url": redact_sensitive_url(str(source.get("url") or ""))},
            *([{"label": "Telegram post", "url": redact_sensitive_url(str(source.get("telegram_post_url")))}]
              if source.get("telegram_post_url") else []),
        ],
    )
    card_data = EnrichedCardV2(
        schema_version=config.ENRICHMENT_SCHEMA_VERSION,
        prompt_version=config.YOUTUBE_ENRICHMENT_PROMPT_VERSION,
        enrichment_model=llm_backend.active_model_for("enrichment"),
        enriched_at=datetime.now(UTC).isoformat(),
        provenance=provenance,
        content_type="youtube_transcript",
        language="ru",
        summary=payload.summary,
        key_points=payload.key_points,
        entities=payload.entities,
        topics=payload.topics,
        theses=payload.theses,
        quotes=payload.quotes,
        events=payload.events,
        search_phrases=payload.search_phrases,
        source_chain=source_chain,
        ignored_blocks=[],
        quality_flags=list(dict.fromkeys(flags)),
        extraction_issues=issues,
        duplicate_of=duplicate_of,
    ).model_dump(mode="json")
    populate_graph_texts(card_data)
    return EnrichedCardV2.model_validate(card_data)


def _build_segment_search_text(payload: LLMPayload, transcript_text: str) -> str:
    card = payload.model_dump(mode="json")
    parts = [transcript_text.strip(), payload.summary.strip()]
    parts.extend(str(item.get("text") or "") for item in card["key_points"] if isinstance(item, dict))
    parts.extend(str(item.get("label") or "") for item in card["topics"] if isinstance(item, dict))
    parts.extend(str(item.get("text") or "") for item in card["search_phrases"] if isinstance(item, dict))
    for group in card["entities"].values():
        parts.extend(str(item.get("text") or "") for item in group if isinstance(item, dict))
    return "\n".join(part for part in parts if part)


def _load_reusable_segment(
    source: dict,
    spec: SegmentSpec,
    fingerprint: str,
    *,
    checkpoint_fingerprint: str | None = None,
) -> YouTubeSegmentCardV2 | None:
    manifest_path = _manifest_path(_episode_card_path(source))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("published YouTube manifest must be an object")
        segment_hashes = manifest.get("segment_sha256")
        if not isinstance(segment_hashes, dict):
            raise ValueError("published YouTube segment hashes must be an object")
        active_model = llm_backend.active_model_for("enrichment")
        if (
            manifest.get("fingerprint") == fingerprint
            and manifest.get("status") in {"partial", "processing"}
            and manifest.get("enrichment_model") == active_model
        ):
            path = _segment_dir(source) / f"{spec.index:04d}.youtube-segment.json"
            card = YouTubeSegmentCardV2.model_validate_json(path.read_text(encoding="utf-8"))
            if (
                card.enrichment_model == active_model
                and not _segment_spec_violations(card, source, spec)
                and "extraction_unstable" not in card.quality_flags
            ):
                expected_hash = segment_hashes.get(path.name)
                if isinstance(expected_hash, str) and expected_hash == _file_sha256(path):
                    return card.model_copy(
                        update={
                            "quality_flags": _without_pipeline_owned_quality_flags(
                                card.quality_flags
                            )
                        }
                    )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, ValidationError):
        pass
    return _load_checkpoint_segment(
        source,
        spec,
        checkpoint_fingerprint or fingerprint,
    )


def _segment_card_payload(card: YouTubeSegmentCardV2, spec: SegmentSpec) -> dict:
    dumped = card.model_dump(mode="json")
    payload = {
        key: dumped[key]
        for key in _payload_fields()
        if key in dumped
    }
    payload["quality_flags"] = _without_pipeline_owned_quality_flags(
        payload.get("quality_flags", [])
    )
    payload["char_range"] = list(spec.char_range)
    return payload


def _iter_dedicated_sources(
    channel_filter: str | None,
    discovery_errors: list[tuple[Path, str]] | None = None,
    metadata_paths: list[Path] | None = None,
):
    root = config.YOUTUBE_NORMALIZED_DIR
    if not root.exists():
        return
    for metadata_path in sorted(root.rglob("*.youtube.meta.json") if metadata_paths is None else metadata_paths):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            default_stem = metadata_path.name.removesuffix(".meta.json")
            text_path = _resolve_path(
                metadata.get("transcript_path") if isinstance(metadata, dict) else None,
                metadata_path.with_name(f"{default_stem}.txt"),
            )
            cues_path = _resolve_path(
                metadata.get("cues_path") if isinstance(metadata, dict) else None,
                metadata_path.with_name(f"{default_stem}.cues.json"),
            )
            artifact = load_dedicated_youtube_artifact(
                metadata_path,
                text_path,
                cues_path,
            )
            source = _source_from_metadata(artifact=artifact)
            if channel_filter and source["telegram_channel"] != channel_filter:
                continue
            yield source
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Skipping YouTube artifact %s: %s", metadata_path, exc)
            if discovery_errors is not None:
                discovery_errors.append((metadata_path, str(exc)))


def _iter_legacy_sources(
    channel_filter: str | None,
    discovery_errors: list[tuple[Path, str]] | None = None,
    metadata_paths: list[Path] | None = None,
):
    root = config.NORMALIZED_DIR
    if not root.exists():
        return
    for meta_path in sorted(root.rglob("*.meta.json") if metadata_paths is None else metadata_paths):
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("legacy metadata must be a JSON object")
            urls = metadata.get("youtube_urls") or []
            if not isinstance(urls, list):
                raise ValueError("legacy youtube_urls must be a list")
            if not urls:
                continue
            channel = str(metadata.get("channel_name") or meta_path.parent.name)
            if channel_filter and channel != channel_filter:
                continue
            text_path = meta_path.with_suffix("").with_suffix(".txt")
            text = text_path.read_text(encoding="utf-8")
            # Legacy fallback is allowed only for documents that are themselves
            # YouTube normalizations. A mixed Telegram post may contain the
            # same URL but its body is a different source and must not become
            # a fabricated transcript.
            if not _looks_like_legacy_youtube_document(text):
                continue
            for url in urls:
                yield _legacy_source(metadata, text_path, text, str(url))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Skipping legacy YouTube source %s: %s", meta_path, exc)
            if discovery_errors is not None:
                discovery_errors.append((meta_path, str(exc)))


def _source_from_metadata(
    metadata: dict | None = None,
    text_path: Path | None = None,
    cues_path: Path | None = None,
    metadata_path: Path | None = None,
    *,
    artifact: DedicatedYouTubeArtifact | None = None,
) -> dict:
    if artifact is None:
        if metadata_path is None or text_path is None or cues_path is None:
            raise ValueError("YouTube artifact paths are required")
        artifact = load_dedicated_youtube_artifact(metadata_path, text_path, cues_path)
    metadata = artifact.metadata
    text_path = artifact.text_path
    cues = artifact.cues
    text = artifact.transcript_text
    telegram = metadata.get("telegram_source") or {}
    return {
        "url": str(metadata.get("url") or ""),
        "video_id": artifact.video_id,
        "title": str(metadata.get("title") or "YouTube"),
        "channel": str(metadata.get("channel") or "YouTube"),
        "duration_seconds": metadata.get("duration_seconds"),
        "language": str(metadata.get("language") or "unknown"),
        "transcript_source": str(metadata.get("transcript_source") or "unknown"),
        "transcript_text": _extract_transcript_body(text),
        "cues": cues,
        "chapters": metadata.get("chapters") or [],
        "text_path": str(text_path),
        "metadata_path": str(artifact.metadata_path),
        "message_id": telegram.get("message_id") or metadata.get("video_id"),
        "telegram_channel": str(telegram.get("channel_name") or ""),
        "telegram_channel_id": telegram.get("channel_id"),
        "telegram_date": telegram.get("date"),
        "telegram_post_url": telegram.get("post_url"),
    }


def _validate_youtube_metadata(metadata: object) -> None:
    if not isinstance(metadata, dict):
        raise ValueError("YouTube metadata must be a JSON object")
    telegram = metadata.get("telegram_source")
    if telegram is not None and not isinstance(telegram, dict):
        raise ValueError("YouTube telegram_source must be an object")
    chapters = metadata.get("chapters")
    if chapters is not None and not isinstance(chapters, list):
        raise ValueError("YouTube chapters must be a list")
    if isinstance(chapters, list):
        _validate_chapters(chapters)


def _validate_chapters(chapters: list[object]) -> None:
    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            raise ValueError(f"YouTube chapter {index} must be an object")
        title = chapter.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"YouTube chapter {index} must have a non-empty title")
        start = _finite_seconds(chapter.get("start_seconds"))
        end = _finite_seconds(chapter.get("end_seconds"))
        if start is None:
            raise ValueError(f"YouTube chapter {index} must have a valid start_seconds")
        if chapter.get("end_seconds") is not None and end is None:
            raise ValueError(f"YouTube chapter {index} has an invalid end_seconds")
        if end is not None and end < start:
            raise ValueError(f"YouTube chapter {index} ends before it starts")


def _validate_cues(cues: list[object]) -> None:
    for index, cue in enumerate(cues):
        if not isinstance(cue, dict):
            raise ValueError(f"YouTube cue {index} must be an object")
        text = cue.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"YouTube cue {index} must have non-empty text")
        start = _finite_seconds(cue.get("start_seconds"))
        end = _finite_seconds(cue.get("end_seconds"))
        if start is None or end is None:
            raise ValueError(f"YouTube cue {index} must have valid timestamps")
        if end < start:
            raise ValueError(f"YouTube cue {index} ends before it starts")


def _finite_seconds(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def load_dedicated_youtube_artifact(
    metadata_path: Path,
    text_path: Path,
    cues_path: Path,
    *,
    entry_video_id: str | None = None,
    expected_video_ids: set[str] | None = None,
) -> DedicatedYouTubeArtifact:
    """Load the artifact contract shared by pilot selection and production discovery."""
    if not metadata_path.name.endswith(".youtube.meta.json"):
        raise ValueError("YouTube metadata filename must end with .youtube.meta.json")
    if not metadata_path.is_file() or not text_path.is_file():
        raise ValueError("YouTube metadata and transcript files must exist")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    _validate_youtube_metadata(metadata)
    transcript_text = text_path.read_text(encoding="utf-8")
    if not transcript_text.strip():
        raise ValueError("YouTube transcript must not be empty")

    transcript_source = str(metadata.get("transcript_source") or "").strip().casefold()
    if transcript_source in {"description", "unavailable"}:
        raise ValueError("YouTube artifact does not contain a usable transcript")

    video_id = str(metadata.get("video_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,}", video_id):
        raise ValueError("YouTube metadata must contain a valid video_id")
    if entry_video_id is not None and str(entry_video_id).strip() != video_id:
        raise ValueError("YouTube entry and metadata video_id do not match")
    metadata_url = str(metadata.get("url") or "").strip()
    if not is_valid_youtube_url(metadata_url):
        raise ValueError("YouTube metadata URL is not a valid YouTube URL")
    metadata_url_id = _video_id(metadata_url)
    if metadata_url_id != video_id:
        raise ValueError("YouTube metadata URL and video_id do not match")
    if expected_video_ids is not None and video_id not in expected_video_ids:
        raise ValueError("YouTube artifact is not linked to a URL in the source post")

    cues: list = []
    if cues_path.is_file():
        cues = json.loads(cues_path.read_text(encoding="utf-8"))
        if not isinstance(cues, list):
            raise ValueError("YouTube cues must be a list")
        _validate_cues(cues)
    elif metadata.get("cues_path"):
        raise ValueError("YouTube metadata points to a missing cues file")

    return DedicatedYouTubeArtifact(
        metadata_path=metadata_path,
        text_path=text_path,
        cues_path=cues_path,
        metadata=metadata,
        transcript_text=transcript_text,
        cues=cues,
        video_id=video_id,
    )


def _legacy_source(metadata: dict, text_path: Path, text: str, url: str) -> dict:
    title = _first_header_value(text, "Название") or "YouTube"
    channel = _first_header_value(text, "Автор") or str(metadata.get("channel_name") or "YouTube")
    body = _extract_transcript_body(text)
    return {
        "url": url,
        "video_id": _video_id(url),
        "title": title,
        "channel": channel,
        "duration_seconds": None,
        "language": "unknown",
        "transcript_source": "legacy_normalized",
        "transcript_text": body,
        "cues": [],
        "chapters": [],
        "text_path": str(text_path),
        "message_id": metadata.get("message_id") or text_path.stem,
        "telegram_channel": str(metadata.get("channel_name") or text_path.parent.name),
        "telegram_channel_id": metadata.get("channel_id"),
        "telegram_date": metadata.get("date"),
        "telegram_post_url": metadata.get("post_url"),
    }


def _episode_card_path(source: dict) -> Path:
    channel = _source_channel_component(source)
    message_id = _safe_component(source.get("message_id") or "unknown")
    video_id = _safe_component(source.get("video_id") or "unknown")
    return config.ENRICHED_DIR / channel / f"{message_id}.youtube.{video_id}.enriched.json"


def _segment_dir(source: dict) -> Path:
    channel = _source_channel_component(source)
    message_id = _safe_component(source.get("message_id") or "unknown")
    video_id = _safe_component(source.get("video_id") or "unknown")
    return config.YOUTUBE_SEGMENTS_DIR / channel / message_id / video_id


def _count_segment_files(source: dict) -> int:
    return len(list(_segment_dir(source).glob("*.youtube-segment.json")))


def _source_channel_component(source: dict) -> str:
    return _safe_component(
        source.get("telegram_channel_id")
        if source.get("telegram_channel_id") is not None
        else source.get("telegram_channel") or source.get("channel") or "youtube"
    )


def _legacy_episode_card_path(source: dict) -> Path:
    channel = _safe_component(source.get("telegram_channel") or source.get("channel") or "youtube")
    message_id = _safe_component(source.get("message_id") or "unknown")
    video_id = _safe_component(source.get("video_id") or "unknown")
    return config.ENRICHED_DIR / channel / f"{message_id}.youtube.{video_id}.enriched.json"


def _legacy_segment_dir(source: dict) -> Path:
    channel = _safe_component(source.get("telegram_channel") or source.get("channel") or "youtube")
    message_id = _safe_component(source.get("message_id") or "unknown")
    video_id = _safe_component(source.get("video_id") or "unknown")
    return config.YOUTUBE_SEGMENTS_DIR / channel / message_id / video_id


def _manifest_path(card_path: Path) -> Path:
    return card_path.with_name(f"{card_path.name}.manifest.json")


def _checkpoint_dir(source: dict, fingerprint: str) -> Path:
    source_digest = hashlib.sha256(_episode_source_id(source).encode("utf-8")).hexdigest()[:24]
    return config.YOUTUBE_CHECKPOINT_DIR / source_digest / fingerprint


def _checkpoint_manifest_path(source: dict, fingerprint: str) -> Path:
    return _checkpoint_dir(source, fingerprint) / "manifest.json"


def _initialize_checkpoint(source: dict, fingerprint: str, specs: list[SegmentSpec]) -> None:
    """Create or reuse a processing checkpoint for one YouTube generation."""
    manifest_path = _checkpoint_manifest_path(source, fingerprint)
    manifest = _read_checkpoint_manifest(manifest_path, fingerprint)
    if (
        manifest is not None
        and manifest.get("segment_count") == len(specs)
        and manifest.get("status") in {"processing", "complete"}
    ):
        return

    checkpoint_dir = _checkpoint_dir(source, fingerprint)
    if checkpoint_dir.exists():
        try:
            shutil.rmtree(checkpoint_dir)
        except OSError as exc:
            logger.warning("Could not reset invalid YouTube checkpoint %s: %s", checkpoint_dir, exc)

    _checkpoint_write_json(
        manifest_path,
        {
            "status": "processing",
            "fingerprint": fingerprint,
            "enrichment_model": llm_backend.active_model_for("enrichment"),
            "segment_count": len(specs),
            "segments": {},
        },
    )


def _load_checkpoint_segment(
    source: dict,
    spec: SegmentSpec,
    fingerprint: str,
) -> YouTubeSegmentCardV2 | None:
    manifest_path = _checkpoint_manifest_path(source, fingerprint)
    path = _checkpoint_dir(source, fingerprint) / f"{spec.index:04d}.youtube-segment.json"
    manifest = _read_checkpoint_manifest(manifest_path, fingerprint)
    if manifest is None or manifest.get("status") not in {"processing", "complete"}:
        return None
    entry = manifest["segments"].get(str(spec.index))
    if not isinstance(entry, dict) or entry.get("file") != path.name:
        return None
    try:
        if entry.get("sha256") != _file_sha256(path):
            return None
        card = YouTubeSegmentCardV2.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, ValueError):
        return None
    if _segment_spec_violations(card, source, spec) or "extraction_unstable" in card.quality_flags:
        return None
    if card.enrichment_model != llm_backend.active_model_for("enrichment"):
        return None
    return card.model_copy(
        update={
            "quality_flags": _without_pipeline_owned_quality_flags(card.quality_flags)
        }
    )


def _read_checkpoint_manifest(path: Path, fingerprint: str) -> dict | None:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    if manifest.get("fingerprint") != fingerprint:
        return None
    if manifest.get("enrichment_model") != llm_backend.active_model_for("enrichment"):
        return None
    if manifest.get("status") not in {"processing", "complete"}:
        return None
    if not isinstance(manifest.get("segment_count"), int) or manifest["segment_count"] < 0:
        return None
    segments = manifest.get("segments")
    if not isinstance(segments, dict):
        return None
    for index, entry in segments.items():
        if not isinstance(index, str) or not isinstance(entry, dict):
            return None
        if not isinstance(entry.get("file"), str) or not isinstance(entry.get("sha256"), str):
            return None
    return manifest


def _checkpoint_segment(
    source: dict,
    fingerprint: str,
    card: YouTubeSegmentCardV2,
) -> None:
    """Persist one successful segment before continuing the expensive loop."""
    active_model = llm_backend.active_model_for("enrichment")
    if card.enrichment_model != active_model:
        raise ValueError(
            "YouTube checkpoint segment model does not match the active enrichment model"
        )
    checkpoint_dir = _checkpoint_dir(source, fingerprint)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    segment_path = checkpoint_dir / f"{card.segment_index:04d}.youtube-segment.json"
    _checkpoint_write_json(segment_path, card.model_dump(mode="json"))

    manifest_path = checkpoint_dir / "manifest.json"
    manifest = _read_checkpoint_manifest(manifest_path, fingerprint)
    if manifest is None:
        raise RuntimeError(f"YouTube checkpoint manifest is unreadable: {manifest_path}")
    segments = manifest["segments"]
    segments[str(card.segment_index)] = {
        "file": segment_path.name,
        "sha256": _file_sha256(segment_path),
    }
    manifest["status"] = "processing"
    _checkpoint_write_json(manifest_path, manifest)


def _checkpoint_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_checkpoint(source: dict, fingerprint: str) -> None:
    try:
        shutil.rmtree(_checkpoint_dir(source, fingerprint), ignore_errors=False)
    except OSError as exc:
        logger.warning("Could not remove YouTube checkpoint for %s: %s", _episode_source_id(source), exc)


def _cleanup_checkpoint_family(source: dict, keep_fingerprint: str | None) -> None:
    """Remove completed/stale checkpoints for one source in single-process runs."""
    source_digest = hashlib.sha256(_episode_source_id(source).encode("utf-8")).hexdigest()[:24]
    family_dir = config.YOUTUBE_CHECKPOINT_DIR / source_digest
    if not family_dir.is_dir():
        return
    for checkpoint in family_dir.iterdir():
        if not checkpoint.is_dir() or checkpoint.name == keep_fingerprint:
            continue
        try:
            shutil.rmtree(checkpoint)
        except OSError as exc:
            logger.warning("Could not remove stale YouTube checkpoint %s: %s", checkpoint, exc)


def _source_fingerprint(source: dict, transcript: str) -> str:
    data = {
        "transcript": transcript,
        "url": source.get("url"),
        "video_id": source.get("video_id"),
        "title": source.get("title"),
        "duration_seconds": source.get("duration_seconds"),
        "transcript_source": source.get("transcript_source"),
        "channel": source.get("channel"),
        "telegram_channel": source.get("telegram_channel"),
        "telegram_channel_id": source.get("telegram_channel_id"),
        "message_id": source.get("message_id"),
        "telegram_date": source.get("telegram_date"),
        "telegram_post_url": source.get("telegram_post_url"),
        "text_path": config.persisted_identity_path(source.get("text_path") or ""),
        "cues": source.get("cues") or [],
        "chapters": source.get("chapters") or [],
        "schema_version": config.ENRICHMENT_SCHEMA_VERSION,
        "enrichment_prompt_version": config.ENRICHMENT_PROMPT_VERSION,
        "prompt_version": config.YOUTUBE_ENRICHMENT_PROMPT_VERSION,
        "model": "profile-independent",
        "merge_max_chars": config.YOUTUBE_MERGE_MAX_CHARS,
        "segmentation_version": config.YOUTUBE_SEGMENTATION_VERSION,
        "segment_threshold_chars": SEGMENT_THRESHOLD_CHARS,
        "segment_threshold_seconds": SEGMENT_THRESHOLD_SECONDS,
        "segment_target_chars": SEGMENT_TARGET_CHARS,
        "segment_min_chars": SEGMENT_MIN_CHARS,
        "segment_max_chars": SEGMENT_MAX_CHARS,
    }
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checkpoint_fingerprint(source_fingerprint: str) -> str:
    """Identify resumable work by source generation and the active model."""
    data = {
        "source_fingerprint": source_fingerprint,
        "enrichment_model": llm_backend.active_model_for("enrichment"),
    }
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_current(
    card_path: Path,
    source: dict,
    fingerprint: str,
    specs: list[SegmentSpec],
) -> bool:
    return _validate_generation(card_path, source, fingerprint, specs).valid


def _validate_generation(
    card_path: Path,
    source: dict,
    fingerprint: str,
    specs: list[SegmentSpec],
) -> GenerationValidation:
    """Validate the complete published episode generation and its segments."""
    violations: list[str] = []
    manifest_path = _manifest_path(card_path)
    if not card_path.is_file():
        violations.append("episode_card_missing")
    if not manifest_path.is_file():
        violations.append("manifest_missing")

    manifest: dict = {}
    if manifest_path.is_file():
        try:
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw_manifest, dict):
                violations.append("manifest_not_object")
            else:
                manifest = raw_manifest
        except (OSError, UnicodeError, json.JSONDecodeError):
            violations.append("manifest_invalid")

    card: EnrichedCardV2 | None = None
    if card_path.is_file():
        try:
            card = EnrichedCardV2.model_validate_json(card_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError):
            violations.append("episode_card_invalid")

    expected_source_id = _episode_source_id(source)
    if card is not None:
        if card.provenance.source_id != expected_source_id:
            violations.append("episode_source_id_mismatch")
        if card.prompt_version != config.YOUTUBE_ENRICHMENT_PROMPT_VERSION:
            violations.append("episode_prompt_version_mismatch")
        if llm_backend.model_change_requires_regeneration(card.enrichment_model, "enrichment"):
            violations.append("episode_model_mismatch")
        if manifest.get("enrichment_model") != card.enrichment_model:
            violations.append("manifest_model_mismatch")
        if {"extraction_unstable", "partial_segment_failure"} & set(card.quality_flags):
            violations.append("episode_quality_flags_unstable")

    if manifest.get("status") != "complete":
        violations.append("manifest_not_complete")
    if manifest.get("fingerprint") != fingerprint:
        violations.append("fingerprint_mismatch")
    if manifest.get("segment_count") != len(specs):
        violations.append("segment_count_mismatch")
    if card_path.is_file():
        try:
            card_hash = _file_sha256(card_path)
        except OSError:
            violations.append("episode_card_hash_unreadable")
        else:
            if manifest.get("card_sha256") != card_hash:
                violations.append("episode_card_hash_mismatch")

    segment_dir = _segment_dir(source)
    actual_paths = sorted(segment_dir.glob("*.youtube-segment.json")) if segment_dir.is_dir() else []
    expected_names = [f"{spec.index:04d}.youtube-segment.json" for spec in specs]
    actual_names = [path.name for path in actual_paths]
    manifest_names = sorted(str(name) for name in (manifest.get("segment_files") or []))
    if manifest_names != expected_names:
        violations.append("manifest_segment_files_mismatch")
    if actual_names != expected_names:
        violations.append("segment_files_mismatch")

    segment_hashes = manifest.get("segment_sha256")
    if not isinstance(segment_hashes, dict):
        segment_hashes = {}
        violations.append("segment_hashes_missing")
    segment_ids: list[str] = []
    specs_by_name = {
        f"{spec.index:04d}.youtube-segment.json": spec
        for spec in specs
    }
    for segment_path in actual_paths:
        try:
            segment = YouTubeSegmentCardV2.model_validate_json(
                segment_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError):
            violations.append(f"segment_invalid:{segment_path.name}")
            continue
        segment_ids.append(segment.segment_id)
        spec = specs_by_name.get(segment_path.name)
        if spec is None:
            violations.append(f"segment_unexpected:{segment_path.name}")
        else:
            violations.extend(
                f"{violation}:{segment_path.name}"
                for violation in _segment_spec_violations(segment, source, spec)
            )
            if card is not None and segment.enrichment_model != card.enrichment_model:
                violations.append(f"segment_model_mismatch:{segment_path.name}")
        try:
            segment_hash = _file_sha256(segment_path)
        except OSError:
            violations.append(f"segment_hash_unreadable:{segment_path.name}")
        else:
            if segment_hashes.get(segment_path.name) != segment_hash:
                violations.append(f"segment_hash_mismatch:{segment_path.name}")

    if len(segment_ids) != len(set(segment_ids)):
        violations.append("segment_ids_duplicate")

    return GenerationValidation(
        valid=not violations,
        violations=tuple(dict.fromkeys(violations)),
        segment_ids=tuple(sorted(segment_ids)),
    )


def _segment_spec_violations(
    segment: YouTubeSegmentCardV2,
    source: dict,
    spec: SegmentSpec,
) -> list[str]:
    expected_source_id = _episode_source_id(source)
    expected_video_id = str(source.get("video_id") or "")
    expected_url = youtube_timestamp_url(str(source.get("url") or ""), spec.start_seconds)
    checks = (
        (segment.segment_id == f"{expected_source_id}:segment:{spec.index}", "segment_id_mismatch"),
        (segment.parent_source_id == expected_source_id, "segment_parent_mismatch"),
        (segment.video_id == expected_video_id, "segment_video_mismatch"),
        (segment.segment_index == spec.index, "segment_index_mismatch"),
        (segment.transcript_text == spec.text, "segment_transcript_mismatch"),
        (segment.char_range == list(spec.char_range), "segment_char_range_mismatch"),
        (segment.start_seconds == spec.start_seconds, "segment_start_mismatch"),
        (segment.end_seconds == spec.end_seconds, "segment_end_mismatch"),
        (segment.chapter_titles == list(spec.chapter_titles), "segment_chapters_mismatch"),
        (segment.start_url == expected_url, "segment_start_url_mismatch"),
    )
    return [name for matches, name in checks if not matches]


def _publish_generation(
    source: dict,
    card_path: Path,
    card: EnrichedCardV2,
    segment_cards: list[YouTubeSegmentCardV2],
    fingerprint: str,
    long_form: bool,
) -> None:
    card_path.parent.mkdir(parents=True, exist_ok=True)
    segment_dir = _segment_dir(source)
    segment_dir.parent.mkdir(parents=True, exist_ok=True)
    generation_id = uuid4().hex
    stage_dir: Path | None = None
    card_tmp = card_path.with_name(f".{card_path.name}.{generation_id}.tmp")
    manifest_path = _manifest_path(card_path)
    manifest_tmp = manifest_path.with_name(f".{manifest_path.name}.{generation_id}.tmp")
    card_backup = card_path.with_name(f".{card_path.name}.{generation_id}.old")
    manifest_backup = manifest_path.with_name(f".{manifest_path.name}.{generation_id}.old")
    segment_backup = segment_dir.with_name(f".{segment_dir.name}.{generation_id}.old")
    segment_swapped = False
    card_swapped = False
    manifest_swapped = False
    committed = False
    try:
        if segment_cards:
            stage_dir = Path(tempfile.mkdtemp(prefix=f".{segment_dir.name}.", dir=segment_dir.parent))
            for segment in segment_cards:
                segment_path = stage_dir / f"{segment.segment_index:04d}.youtube-segment.json"
                segment_path.write_text(
                    json.dumps(segment.model_dump(mode="json"), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        card_tmp.write_text(
            json.dumps(card.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        status = "partial" if any(
            flag in card.quality_flags
            for flag in ("extraction_unstable", "partial_segment_failure")
        ) else "complete"
        segment_hashes = {
            f"{segment.segment_index:04d}.youtube-segment.json": _file_sha256(
                stage_dir / f"{segment.segment_index:04d}.youtube-segment.json"
            )
            for segment in segment_cards
        } if stage_dir is not None else {}
        manifest_tmp.write_text(
            json.dumps(
                {
                    "generation_id": generation_id,
                    "fingerprint": fingerprint,
                    "enrichment_model": card.enrichment_model,
                    "status": status,
                    "segment_count": len(segment_cards) if long_form else 0,
                    "segment_files": [
                        f"{segment.segment_index:04d}.youtube-segment.json"
                        for segment in segment_cards
                    ],
                    "card_sha256": _file_sha256(card_tmp),
                    "segment_sha256": segment_hashes,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        if card_path.exists():
            card_path.replace(card_backup)
        if manifest_path.exists():
            manifest_path.replace(manifest_backup)
        if segment_dir.exists():
            segment_dir.replace(segment_backup)
        if stage_dir is not None:
            stage_dir.replace(segment_dir)
            stage_dir = None
            segment_swapped = True
        elif segment_dir.exists():
            shutil.rmtree(segment_dir)
            segment_swapped = True

        card_tmp.replace(card_path)
        card_swapped = True
        manifest_tmp.replace(manifest_path)
        manifest_swapped = True
        committed = True
    except BaseException as original_exc:
        if not committed:
            rollback_errors: list[BaseException] = []

            def rollback_step(label: str, action) -> None:
                try:
                    action()
                except BaseException as rollback_exc:
                    rollback_errors.append(rollback_exc)
                    logger.exception("YouTube rollback step failed (%s): %s", label, rollback_exc)

            if manifest_swapped:
                rollback_step("remove new manifest", lambda: manifest_path.unlink(missing_ok=True))
            if card_swapped:
                rollback_step("remove new episode card", lambda: card_path.unlink(missing_ok=True))
            if segment_swapped and segment_dir.exists():
                rollback_step("remove new segments", lambda: shutil.rmtree(segment_dir))
            _restore_backup(manifest_backup, manifest_path)
            _restore_backup(card_backup, card_path)
            _restore_backup(segment_backup, segment_dir)
            if rollback_errors:
                logger.error(
                    "YouTube rollback completed with %d secondary error(s); preserving original failure: %s",
                    len(rollback_errors),
                    original_exc,
                )
        raise
    finally:
        if stage_dir is not None and stage_dir.exists():
            try:
                shutil.rmtree(stage_dir)
            except BaseException as exc:
                logger.exception("Could not remove staged YouTube segments %s: %s", stage_dir, exc)
        for temporary in (card_tmp, manifest_tmp):
            try:
                temporary.unlink(missing_ok=True)
            except BaseException as exc:
                logger.exception("Could not remove temporary YouTube file %s: %s", temporary, exc)

    # Cleanup is deliberately outside the commit/rollback transaction. The
    # live generation is already valid and must survive cleanup failures.
    for backup in (card_backup, manifest_backup, segment_backup):
        try:
            if backup.is_dir():
                shutil.rmtree(backup)
            else:
                backup.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove YouTube generation backup %s: %s", backup, exc)
    try:
        _remove_legacy_generation(source, card_path, segment_dir)
    except OSError as exc:
        logger.warning("Could not remove legacy YouTube generation: %s", exc)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _restore_backup(backup: Path, target: Path) -> None:
    """Restore one backup without allowing rollback errors to hide the original error."""
    if not backup.exists():
        return
    try:
        os.replace(backup, target)
    except OSError as exc:
        logger.exception("Could not restore YouTube generation backup %s: %s", backup, exc)


def _remove_legacy_generation(source: dict, card_path: Path, segment_dir: Path) -> None:
    old_card = _legacy_episode_card_path(source)
    if old_card != card_path and old_card.exists():
        old_card.unlink()
        _manifest_path(old_card).unlink(missing_ok=True)
    old_segments = _legacy_segment_dir(source)
    if old_segments != segment_dir and old_segments.exists():
        shutil.rmtree(old_segments)


def _episode_source_id(source: dict) -> str:
    telegram_id = _telegram_source_id(source)
    return f"{telegram_id}:youtube:{source['video_id']}" if telegram_id else f"youtube:{source['video_id']}"


def _source_key(source: dict) -> tuple[str, str]:
    occurrence = _telegram_source_id(source)
    if not occurrence:
        occurrence = f"channel:{source.get('telegram_channel') or source.get('channel') or 'youtube'}"
    return occurrence, str(source.get("video_id") or "unknown")


def _telegram_source_id(source: dict) -> str:
    channel_id = source.get("telegram_channel_id")
    channel = source.get("telegram_channel") or "telegram"
    message_id = source.get("message_id")
    if message_id is None:
        return ""
    return f"telegram:{channel_id or channel}:{message_id}"


def _extract_transcript_body(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower().startswith("url:"):
            return " ".join(part.strip() for part in lines[index + 1:] if part.strip())
    return text.strip()


def _looks_like_legacy_youtube_document(text: str) -> bool:
    return bool(
        re.search(r"^\[YouTube\]\s*$", text, re.IGNORECASE | re.MULTILINE)
        and re.search(r"^URL:\s*\S+", text, re.IGNORECASE | re.MULTILINE)
    )


def _first_header_value(text: str, label: str) -> str:
    match = re.search(rf"^{re.escape(label)}:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else ""


def _video_id(url: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]+)", url)
    return match.group(1) if match else "unknown"


def _resolve_path(value: object, fallback: Path) -> Path:
    if value:
        return config.resolve_project_path(str(value))
    return fallback


def _safe_component(value: object) -> str:
    return re.sub(r"[^\w.-]+", "_", str(value or "unknown"), flags=re.UNICODE).strip("._") or "unknown"


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_quality_flags(payload: LLMPayload) -> None:
    flags = list(dict.fromkeys(payload.quality_flags))
    substantive = bool(payload.summary.strip() or payload.key_points or payload.events or payload.theses)
    if substantive:
        flags = [flag for flag in flags if flag != "no_substantive_content"]
    # These flags describe pipeline state and must never be accepted from the LLM.
    payload.quality_flags = _without_pipeline_owned_quality_flags(flags)


def _without_pipeline_owned_quality_flags(flags: Sequence[str]) -> list[str]:
    return [flag for flag in flags if flag not in _PIPELINE_OWNED_QUALITY_FLAGS]


def _payload_fields() -> tuple[str, ...]:
    return (
        "summary", "key_points", "entities", "topics", "theses", "quotes",
        "events", "search_phrases", "quality_flags",
    )


def _queue_youtube_failure(source: dict, reason: str) -> None:
    message_id = source.get("message_id")
    try:
        message_id = int(message_id)
    except (TypeError, ValueError):
        message_id = 0
    try:
        queue_review_item(
            review_type=REVIEW_TYPE_EXTERNAL_LINK,
            channel_name=str(source.get("telegram_channel") or source.get("channel") or "YouTube"),
            message_id=message_id,
            message_date=None,
            url=str(source.get("url") or ""),
            reason=f"YouTube enrichment failed: {reason}",
        )
    except Exception as exc:
        logger.warning("Could not queue failed YouTube source: %s", exc)


def _queue_artifact_discovery_failure(metadata_path: Path, reason: str) -> None:
    try:
        message_id = int(metadata_path.parent.name)
    except ValueError:
        message_id = 0
    try:
        queue_review_item(
            review_type=REVIEW_TYPE_EXTERNAL_LINK,
            channel_name=metadata_path.parent.parent.name,
            message_id=message_id,
            reason=f"YouTube artifact discovery failed: {reason}",
            normalized_filepath=str(metadata_path),
        )
    except Exception as exc:
        logger.warning("Could not queue invalid YouTube artifact: %s", exc)
