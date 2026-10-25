"""Controlled visual backfill for already captured native media."""

from __future__ import annotations

import hashlib
import json
import os  # noqa: F401 - compatibility patch point for atomic replace tests
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from geospoiler import config
from geospoiler.ingestion.normalizer.atomic_pair import atomic_write_pair, recover_atomic_pair
from geospoiler.ingestion.normalizer.media_paths import (
    MediaPathError,
    portable_media_path,
    portable_project_path,
    resolve_media_path,
)
from geospoiler.ingestion.normalizer.media_vision import (
    VisualExtractionResult,
    _visual_model,
    analyze_image,
    analyze_video,
)


@dataclass
class VisualBackfillItemResult:
    normalized_file: str
    media_type: str
    message_id: int
    status: str
    artifact_path: str = ""
    error: str = ""
    updated: bool = False


@dataclass
class VisualBackfillStats:
    candidates_seen: int = 0
    attempted: int = 0
    success: int = 0
    skipped: int = 0
    failed: int = 0
    normalized_updated: int = 0
    dry_run: bool = False
    items: list[VisualBackfillItemResult] = field(default_factory=list)


def backfill_visuals(
    normalized_dir: Path | None = None,
    limit: int | None = 3,
    channel: str | None = None,
    message_ids: set[int] | None = None,
    dry_run: bool = False,
) -> VisualBackfillStats:
    """Analyze downloaded images/videos and update their sidecars atomically."""
    normalized_dir = normalized_dir or config.NORMALIZED_DIR
    limit = 0 if limit is None else max(0, int(limit))
    wanted_channel = (channel or "").strip().casefold()
    wanted_ids = {parsed for value in (message_ids or set()) if (parsed := _safe_message_id(value))}
    stats = VisualBackfillStats(dry_run=dry_run)

    for candidate in _iter_candidates(normalized_dir):
        if wanted_channel and candidate["channel_name"].casefold() != wanted_channel:
            continue
        if wanted_ids and candidate["message_id"] not in wanted_ids:
            continue
        if limit and stats.attempted >= limit:
            break

        stats.candidates_seen += 1
        if _has_current_visual_identity(candidate):
            stats.skipped += 1
            cached = _cached_result(candidate)
            updated = False if dry_run else _update_candidate(candidate, cached)
            stats.normalized_updated += int(updated)
            stats.items.append(
                VisualBackfillItemResult(
                    normalized_file=str(candidate["normalized_path"]),
                    media_type=candidate["media_type"],
                    message_id=candidate["message_id"],
                    status="cached",
                    artifact_path=cached.artifact_path,
                    updated=updated,
                )
            )
            continue
        stats.attempted += 1
        result = (
            VisualExtractionResult(status="dry_run")
            if dry_run
            else _analyze(candidate)
        )
        updated = False
        if result.status == "success":
            stats.success += 1
        elif result.status in {"unavailable", "dry_run"}:
            stats.skipped += 1
        else:
            stats.failed += 1
        if not dry_run and result.status != "dry_run":
            updated = _update_candidate(candidate, result)
            stats.normalized_updated += int(updated)

        stats.items.append(
            VisualBackfillItemResult(
                normalized_file=str(candidate["normalized_path"]),
                media_type=candidate["media_type"],
                message_id=candidate["message_id"],
                status=result.status,
                artifact_path=result.artifact_path,
                error=result.error,
                updated=updated,
            )
        )

    return stats


def _safe_message_id(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _iter_candidates(normalized_dir: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if not normalized_dir.exists():
        return candidates
    for meta_path in sorted(normalized_dir.rglob("*.meta.json")):
        normalized_path = meta_path.with_suffix("").with_suffix(".txt")
        try:
            recover_atomic_pair(normalized_path, meta_path, journal_suffix="visual")
        except RuntimeError as exc:
            raise RuntimeError(f"Cannot recover visual pair: {meta_path}: {exc}") from exc
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        channel_name = str(meta.get("channel_name") or meta_path.parent.name)
        message_id = _safe_message_id(meta.get("message_id"))
        if message_id <= 0:
            continue
        if not normalized_path.exists():
            continue
        media_entries = meta.get("media")
        if not isinstance(media_entries, list):
            continue
        for item in media_entries:
            if not isinstance(item, dict):
                continue
            media_type = str(item.get("media_type") or "").strip().lower()
            if media_type not in {"image", "video"}:
                continue
            if str(item.get("download_status") or "").strip().lower() != "downloaded":
                continue
            file_path = str(item.get("file_path") or "").strip()
            if not file_path:
                continue
            media_message_id = _safe_message_id(item.get("message_id"))
            if media_message_id <= 0:
                continue
            item = dict(item)
            item["message_id"] = media_message_id
            candidates.append(
                {
                    "meta_path": meta_path,
                    "normalized_path": normalized_path,
                    "channel_name": channel_name,
                    "message_id": message_id,
                    "media": item,
                    "media_type": media_type,
                }
            )
    return candidates


def _has_current_visual_identity(candidate: dict[str, Any]) -> bool:
    """Return whether a successful visual artifact still matches current identity."""
    item = candidate["media"]
    if str(item.get("visual_status") or "").strip().lower() != "success":
        return False
    artifact_value = str(item.get("visual_artifact_path") or "").strip()
    if not artifact_value:
        return False
    artifact = config.resolve_project_path(artifact_value)
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if str(payload.get("status") or "").strip().lower() != "success":
        return False
    if not str(payload.get("text") or "").strip():
        return False
    raw_source = str(item.get("file_path") or "")
    try:
        source = resolve_media_path(raw_source)
    except MediaPathError:
        source = Path(raw_source).resolve()
        try:
            source.relative_to(config.PROJECT_ROOT.resolve())
        except ValueError:
            return False
    try:
        digest = _sha256_file(source)
    except OSError:
        return False
    return (
        isinstance(payload.get("frame_count"), int)
        and 1 <= payload["frame_count"] <= config.MEDIA_VISION_MAX_FRAMES
        and
        payload.get("input_sha256") == digest
        and payload.get("model") == _visual_model()
        and payload.get("prompt_version") == config.MEDIA_VISION_PROMPT_VERSION
        and payload.get("frame_interval_seconds") == config.MEDIA_VISION_FRAME_INTERVAL_SEC
        and payload.get("max_frames") == config.MEDIA_VISION_MAX_FRAMES
        and payload.get("batch_size") == config.MEDIA_VISION_FRAME_BATCH_SIZE
    )


def _cached_result(candidate: dict[str, Any]) -> VisualExtractionResult:
    artifact_path = config.resolve_project_path(str(candidate["media"].get("visual_artifact_path") or ""))
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    return VisualExtractionResult(
        status="success",
        text=str(payload.get("text") or ""),
        model=str(payload.get("model") or ""),
        artifact_path=str(artifact_path),
        frame_count=int(payload.get("frame_count") or 0),
        input_sha256=str(payload.get("input_sha256") or ""),
        prompt_version=str(payload.get("prompt_version") or ""),
    )


def _analyze(candidate: dict[str, Any]) -> VisualExtractionResult:
    raw_path = str(candidate["media"].get("file_path") or "")
    try:
        path = resolve_media_path(raw_path)
    except MediaPathError as exc:
        return VisualExtractionResult(
            status="failed",
            model=_visual_model(),
            error=str(exc),
            prompt_version=config.MEDIA_VISION_PROMPT_VERSION,
        )
    if candidate["media_type"] == "image":
        return analyze_image(path)
    return analyze_video(
        path,
        source_key=(
            f"{candidate['channel_name']}:{candidate['message_id']}"
            f":{candidate['media'].get('message_id') or candidate['message_id']}"
        ),
    )


def _update_candidate(candidate: dict[str, Any], result: VisualExtractionResult) -> bool:
    meta_path: Path = candidate["meta_path"]
    normalized_path: Path = candidate["normalized_path"]
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    item = candidate["media"]
    media_id = item.get("message_id")
    file_path = portable_media_path(item.get("file_path"))
    matched = False
    for saved_item in meta.get("media") or []:
        if not isinstance(saved_item, dict):
            continue
        if saved_item.get("message_id") == media_id and portable_media_path(saved_item.get("file_path")) == file_path:
            matched = True
            saved_item["file_path"] = file_path
            saved_item["visual_status"] = result.status
            saved_item["visual_model"] = result.model
            saved_item["visual_artifact_path"] = portable_project_path(result.artifact_path)
            saved_item["visual_error"] = result.error
            saved_item["visual_frame_count"] = result.frame_count
            saved_item["visual_input_sha256"] = result.input_sha256
            saved_item["visual_prompt_version"] = result.prompt_version
            break
    if not matched:
        raise ValueError(
            f"visual backfill media item not found: message_id={media_id}, file_path={file_path}"
        )

    extra = {
        "media_type": candidate["media_type"],
        "message_id": media_id,
        "source_file_path": file_path,
        "status": result.status,
        "model": result.model,
        "artifact_path": portable_project_path(result.artifact_path),
        "error": result.error,
        "frame_count": result.frame_count,
        "input_sha256": result.input_sha256,
        "prompt_version": result.prompt_version,
    }
    visual_extractions = [
        existing
        for existing in (meta.get("visual_extractions") or [])
        if not (
            existing.get("message_id") == media_id
            and portable_media_path(existing.get("source_file_path")) == file_path
        )
    ]
    visual_extractions.append(extra)
    meta["visual_extractions"] = visual_extractions

    original_text = normalized_path.read_bytes()
    original_meta = meta_path.read_bytes()
    text = original_text.decode("utf-8")
    if result.text.strip() and not _visual_text_present(text, result):
        label = "Изображение" if candidate["media_type"] == "image" else "Текст с экрана (OCR)"
        text = f"{text.rstrip()}\n\n[{label}]\n{result.text.strip()}\n"
        updated = True
    else:
        updated = False

    _atomic_write_pair(
        normalized_path,
        text.encode("utf-8"),
        meta_path,
        json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
        original_text=original_text,
        original_meta=original_meta,
    )
    return updated


def _atomic_write_pair(
    normalized_path: Path,
    normalized_bytes: bytes,
    meta_path: Path,
    meta_bytes: bytes,
    *,
    original_text: bytes,
    original_meta: bytes,
) -> None:
    atomic_write_pair(
        normalized_path,
        normalized_bytes,
        meta_path,
        meta_bytes,
        original_first=original_text,
        original_second=original_meta,
        journal_suffix="visual",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _visual_text_present(text: str, result: VisualExtractionResult) -> bool:
    return bool(
        result.artifact_path
        and result.artifact_path in text
    ) or bool(result.text.strip() and result.text.strip() in text)
