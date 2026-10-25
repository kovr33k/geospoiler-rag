"""Controlled backfill for native Telegram media transcripts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from geospoiler import config
from geospoiler.ingestion.fetcher.telegram_client import TelegramMedia
from geospoiler.ingestion.normalizer.atomic_pair import atomic_write_pair, recover_atomic_pair
from geospoiler.ingestion.normalizer.media_paths import portable_media_path, portable_project_path
from geospoiler.ingestion.normalizer.transcription_handler import TranscriptionResult, transcribe_media


@dataclass
class BackfillItemResult:
    normalized_file: str
    media_type: str
    message_id: int
    status: str
    artifact_path: str = ""
    error: str = ""
    updated: bool = False
    route: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float | None = None


@dataclass
class BackfillStats:
    candidates_seen: int = 0
    attempted: int = 0
    transcribed: int = 0
    no_speech: int = 0
    cached: int = 0
    disabled: int = 0
    skipped: int = 0
    failed: int = 0
    normalized_updated: int = 0
    dry_run: bool = False
    items: list[BackfillItemResult] = field(default_factory=list)


def backfill_transcripts(
    normalized_dir: Path | None = None,
    limit: int | None = 3,
    channel: str | None = None,
    media_type: str | None = None,
    dry_run: bool = False,
    message_ids: set[int] | None = None,
) -> BackfillStats:
    """Transcribe a bounded set of captured media and update text+sidecar atomically."""
    normalized_dir = normalized_dir or config.NORMALIZED_DIR
    limit = 0 if limit is None else max(0, int(limit))
    wanted_type = (media_type or "").strip().lower()
    wanted_channel = (channel or "").strip().casefold()
    wanted_ids = {parsed for value in (message_ids or set()) if (parsed := _safe_message_id(value))}
    stats = BackfillStats(dry_run=dry_run)

    for candidate in _iter_candidates(normalized_dir):
        if wanted_channel and candidate["channel_name"].casefold() != wanted_channel:
            continue
        if wanted_ids and candidate["message_id"] not in wanted_ids:
            continue
        item: TelegramMedia = candidate["media"]
        if wanted_type and item.media_type != wanted_type:
            continue
        if limit and stats.attempted >= limit:
            break

        stats.candidates_seen += 1
        stats.attempted += 1
        result = (
            TranscriptionResult(status="dry_run")
            if dry_run
            else transcribe_media(item, candidate["channel_name"], candidate["message_id"])
        )
        updated = False
        if result.status == "transcribed":
            stats.transcribed += 1
            if result.artifact_path and Path(result.artifact_path).exists():
                stats.cached += int(_transcript_already_present(candidate["normalized_path"], result))
        elif result.status == "no_speech":
            stats.no_speech += 1
        elif result.status == "disabled":
            stats.disabled += 1
        elif result.status in {"skipped", "unsupported_media_type", "dry_run"}:
            stats.skipped += 1
        else:
            stats.failed += 1
        if not dry_run and result.status != "dry_run":
            updated = _update_transcript_pair(candidate, result)
            stats.normalized_updated += int(updated)

        stats.items.append(
            BackfillItemResult(
                normalized_file=str(candidate["normalized_path"]),
                media_type=item.media_type,
                message_id=candidate["message_id"],
                status=result.status,
                artifact_path=result.artifact_path,
                error=result.error,
                updated=updated,
                route=result.route,
                attempts=result.attempts,
                latency_ms=result.latency_ms,
            )
        )
    return stats


def _iter_candidates(normalized_dir: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if not normalized_dir.exists():
        return candidates
    for meta_path in sorted(normalized_dir.rglob("*.meta.json")):
        normalized_path = meta_path.with_suffix("").with_suffix(".txt")
        try:
            recover_atomic_pair(normalized_path, meta_path, journal_suffix="transcript")
        except RuntimeError as exc:
            raise RuntimeError(f"Cannot recover transcription pair: {meta_path}: {exc}") from exc
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        message_id = _safe_message_id(meta.get("message_id"))
        if message_id <= 0:
            continue
        if not normalized_path.exists() or not isinstance(meta.get("media"), list):
            continue
        for payload in meta["media"]:
            if not isinstance(payload, dict):
                continue
            media = _media_from_payload(payload)
            if media.message_id <= 0:
                continue
            if media.media_type not in {"video", "audio", "voice"}:
                continue
            if media.download_status != "downloaded" or not media.file_path:
                continue
            candidates.append({
                "meta_path": meta_path,
                "normalized_path": normalized_path,
                "channel_name": str(meta.get("channel_name") or meta_path.parent.name),
                "message_id": message_id,
                "media": media,
            })
    return candidates


def _safe_message_id(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _media_from_payload(payload: dict[str, Any]) -> TelegramMedia:
    return TelegramMedia(
        media_type=str(payload.get("media_type") or ""),
        mime_type=str(payload.get("mime_type") or ""),
        message_id=_safe_message_id(payload.get("message_id")),
        file_path=str(payload.get("file_path") or ""),
        download_status=str(payload.get("download_status") or "not_attempted"),
        error=str(payload.get("error") or ""),
    )


def _update_transcript_pair(candidate: dict[str, Any], result: TranscriptionResult) -> bool:
    normalized_path: Path = candidate["normalized_path"]
    meta_path: Path = candidate["meta_path"]
    item: TelegramMedia = candidate["media"]
    text = normalized_path.read_text(encoding="utf-8")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    updated_meta = _apply_meta_update(meta, item, result)
    new_text = text
    if result.status == "transcribed" and result.text.strip() and not _transcript_already_present(normalized_path, result):
        section = _format_transcript_section(item, result)
        new_text = f"{text.rstrip()}\n\n{section}\n"
    _atomic_write_pair(
        normalized_path,
        new_text.encode("utf-8"),
        meta_path,
        json.dumps(updated_meta, ensure_ascii=False, indent=2).encode("utf-8"),
        original_text=normalized_path.read_bytes(),
        original_meta=meta_path.read_bytes(),
    )
    return new_text != text or updated_meta != meta


def _transcript_already_present(normalized_path: Path, result: TranscriptionResult) -> bool:
    text = normalized_path.read_text(encoding="utf-8")
    return bool((result.artifact_path and result.artifact_path in text) or (result.text and result.text.strip() in text))


def _format_transcript_section(item: TelegramMedia, result: TranscriptionResult) -> str:
    label = "Voice transcript" if item.media_type == "voice" else "Video transcript" if item.media_type == "video" else "Transcript"
    artifact_path = portable_project_path(result.artifact_path)
    artifact = f" | artifact={artifact_path}" if artifact_path else ""
    return f"[{label}{artifact}]\n{result.text.strip()}"


def _apply_meta_update(meta: dict[str, Any], item: TelegramMedia, result: TranscriptionResult) -> dict[str, Any]:
    source = portable_media_path(item.file_path)
    transcript = {
        "media_type": item.media_type,
        "message_id": item.message_id,
        "source_file_path": source,
        "status": result.status,
        "artifact_path": portable_project_path(result.artifact_path),
        "error": result.error,
        "route": result.route,
        "attempts": result.attempts,
        "latency_ms": result.latency_ms,
    }
    transcriptions = [
        existing for existing in (meta.get("transcriptions") or [])
        if not (existing.get("message_id") == item.message_id and portable_media_path(existing.get("source_file_path")) == source)
    ]
    transcriptions.append(transcript)
    meta["transcriptions"] = transcriptions
    for media in meta.get("media") or []:
        if isinstance(media, dict) and media.get("message_id") == item.message_id and portable_media_path(media.get("file_path")) == source:
            media["file_path"] = source
            media["transcription_status"] = result.status
            media["transcript_path"] = portable_project_path(result.artifact_path)
            media["transcription_error"] = result.error
            media["transcription_route"] = result.route
            media["transcription_attempts"] = result.attempts
            media["transcription_latency_ms"] = result.latency_ms
            break
    return meta


def _atomic_write_pair(normalized_path: Path, normalized_bytes: bytes, meta_path: Path, meta_bytes: bytes, *, original_text: bytes, original_meta: bytes) -> None:
    atomic_write_pair(
        normalized_path,
        normalized_bytes,
        meta_path,
        meta_bytes,
        original_first=original_text,
        original_second=original_meta,
        journal_suffix="transcript",
    )
