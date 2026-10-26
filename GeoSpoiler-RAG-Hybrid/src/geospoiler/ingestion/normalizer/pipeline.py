"""
Normalizer Pipeline - orchestrates the conversion of TelegramMessage -> normalized .txt file.

Takes classified messages from the router and runs appropriate handlers,
then assembles everything into a single normalized text file per message.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from geospoiler import config
from geospoiler.contracts.models import NormalizedMeta
from geospoiler.ingestion.fetcher.telegram_client import TelegramMedia, TelegramMessage
from geospoiler.ingestion.normalizer.ai_chat_handler import queue_for_review
from geospoiler.ingestion.normalizer.atomic_pair import atomic_write_bytes, atomic_write_pair, recover_atomic_pair
from geospoiler.ingestion.normalizer.document_handler import extract_document_text
from geospoiler.ingestion.normalizer.image_handler import describe_image
from geospoiler.ingestion.normalizer.instagram_handler import extract_instagram_text
from geospoiler.ingestion.normalizer.media_paths import portable_media_path, portable_project_path
from geospoiler.ingestion.normalizer.media_selection import media_operations
from geospoiler.ingestion.normalizer.media_vision import VisualExtractionResult, analyze_image, analyze_video
from geospoiler.ingestion.normalizer.review_queue import REVIEW_TYPE_EXTERNAL_LINK
from geospoiler.ingestion.normalizer.review_queue import queue_item as queue_review_item
from geospoiler.ingestion.normalizer.router import ClassifiedMessage, classify
from geospoiler.ingestion.normalizer.text_handler import normalize_text
from geospoiler.ingestion.normalizer.transcription_handler import TranscriptionResult, transcribe_media
from geospoiler.ingestion.normalizer.translator import translate_to_russian_if_needed
from geospoiler.ingestion.normalizer.youtube_handler import (
    extract_youtube_artifact,
    save_youtube_artifact,
)
from geospoiler.providers import llm_backend

logger = logging.getLogger("geospoiler.normalizer")


@dataclass
class NormalizedMessageResult:
    """Normalization result for a single Telegram message."""

    status: str  # normalized | skipped
    filepath: str | None = None
    text: str | None = None
    ai_review_created: int = 0
    ai_review_already_reviewed: int = 0
    link_review_created: int = 0
    uninformative_review_created: int = 0


@dataclass
class NormalizationBatchResult:
    """Aggregated normalization output and content statistics for a batch."""

    texts_with_paths: list[tuple[str, str]] = field(default_factory=list)
    messages_total: int = 0
    messages_with_text: int = 0
    messages_with_images: int = 0
    images_total: int = 0
    messages_with_native_video: int = 0
    messages_with_youtube: int = 0
    youtube_links_total: int = 0
    messages_with_instagram_reels: int = 0
    instagram_reel_links_total: int = 0
    messages_with_instagram_posts: int = 0
    instagram_post_links_total: int = 0
    messages_with_ai_chat: int = 0
    ai_chat_links_total: int = 0
    messages_with_web: int = 0
    web_links_total: int = 0
    normalized_messages: int = 0
    skipped_messages: int = 0
    failed_messages: int = 0
    processed_messages: int = 0
    ai_review_created: int = 0
    link_review_created: int = 0
    uninformative_review_created: int = 0
    ai_review_already_reviewed: int = 0

    def merge(self, other: "NormalizationBatchResult") -> None:
        """Merge another batch result into this one."""
        self.texts_with_paths.extend(other.texts_with_paths)
        self.messages_total += other.messages_total
        self.messages_with_text += other.messages_with_text
        self.messages_with_images += other.messages_with_images
        self.images_total += other.images_total
        self.messages_with_native_video += other.messages_with_native_video
        self.messages_with_youtube += other.messages_with_youtube
        self.youtube_links_total += other.youtube_links_total
        self.messages_with_instagram_reels += other.messages_with_instagram_reels
        self.instagram_reel_links_total += other.instagram_reel_links_total
        self.messages_with_instagram_posts += other.messages_with_instagram_posts
        self.instagram_post_links_total += other.instagram_post_links_total
        self.messages_with_ai_chat += other.messages_with_ai_chat
        self.ai_chat_links_total += other.ai_chat_links_total
        self.messages_with_web += other.messages_with_web
        self.web_links_total += other.web_links_total
        self.normalized_messages += other.normalized_messages
        self.skipped_messages += other.skipped_messages
        self.failed_messages += other.failed_messages
        self.processed_messages += other.processed_messages
        self.ai_review_created += other.ai_review_created
        self.ai_review_already_reviewed += other.ai_review_already_reviewed
        self.link_review_created += other.link_review_created
        self.uninformative_review_created += other.uninformative_review_created


def normalize_message(
    msg: TelegramMessage,
    classified: ClassifiedMessage | None = None,
) -> NormalizedMessageResult:
    """
    Normalize a single Telegram message into a text document.

    Returns normalization metadata and saves the file to
    output/normalized/{channel}/{msg_id}.txt when meaningful content exists.
    """
    classified = classified or classify(msg)

    sections = []
    youtube_sources: list[dict] = []
    ai_review_created = 0
    ai_review_already_reviewed = 0
    link_review_created = 0
    uninformative_review_created = 0

    header = _build_header(msg)
    sections.append(header)

    # Check if post has meaningful text beyond just URLs
    has_body_text = False
    if classified.has_text:
        clean_text = normalize_text(msg.text)
        text_without_urls = _strip_urls_from_text(clean_text, msg.urls)
        if text_without_urls.strip():
            sections.append(text_without_urls)
            has_body_text = True

    image_sections, image_visual_metadata = _build_image_sections(classified, msg)
    sections.extend(image_sections)

    media_sections, transcription_metadata, visual_metadata = _build_media_sections(classified, msg)
    sections.extend(media_sections)
    visual_metadata = image_visual_metadata + visual_metadata

    # YouTube is extracted into a dedicated source artifact. A mixed Telegram
    # post keeps its own text in the normal archive and gets a separate episode.
    for url in classified.youtube_urls:
        try:
            artifact = extract_youtube_artifact(url)
            paths = save_youtube_artifact(
                artifact,
                channel_name=msg.channel_name,
                message_id=msg.message_id,
                source_meta={
                    "channel_name": msg.channel_name,
                    "channel_id": msg.channel_id,
                    "message_id": msg.message_id,
                    "date": msg.date.isoformat() if msg.date else None,
                    "post_url": msg.post_url,
                },
                channel_id=msg.channel_id,
            )
            youtube_sources.append(
                {
                    **artifact.metadata(),
                    **{
                        "text_path": paths["text_path"],
                        "metadata_path": paths["metadata_path"],
                        "cues_path": paths["cues_path"],
                    },
                }
            )
            if has_body_text:
                # The dedicated artifact is referenced by metadata. Do not put
                # a filesystem path into translatable/searchable body text.
                continue
            else:
                sections.append(artifact.normalized_text)
        except Exception as exc:
            logger.warning("YouTube extraction failed for %s: %s", url, exc)
            rv = queue_review_item(
                review_type=REVIEW_TYPE_EXTERNAL_LINK,
                channel_name=msg.channel_name,
                message_id=msg.message_id,
                message_text=msg.text,
                message_date=msg.date,
                url=url,
                reason=f"YouTube extraction failed: {exc}",
            )
            sections.append(rv.placeholder_text)
            if rv.action == "queued":
                link_review_created += 1

    # Instagram: same rule — auto-process only link-only posts.
    for url in classified.instagram_urls:
        ig_text = extract_instagram_text(
            url,
            channel_name=msg.channel_name,
            message_id=msg.message_id,
            message_text=msg.text,
            message_date=msg.date,
        )
        sections.append(ig_text)

    for url in classified.ai_chat_urls:
        review_result = queue_for_review(
            url=url,
            channel_name=msg.channel_name,
            message_id=msg.message_id,
            message_text=msg.text,
            message_date=msg.date,
        )
        sections.append(review_result.placeholder_text)
        if review_result.action == "queued":
            ai_review_created += 1
        elif review_result.action == "already_reviewed":
            ai_review_already_reviewed += 1

    # ALL external web URLs go to review queue.
    for url in classified.web_urls:
        rv = queue_review_item(
            review_type=REVIEW_TYPE_EXTERNAL_LINK,
            channel_name=msg.channel_name,
            message_id=msg.message_id,
            message_text=msg.text,
            message_date=msg.date,
            url=url,
            reason="External web link",
        )
        sections.append(rv.placeholder_text)
        if rv.action == "queued":
            link_review_created += 1

    if len(sections) <= 1:
        logger.debug(f"  Message {msg.message_id} from '{msg.channel_name}' has no content - skipping.")
        return NormalizedMessageResult(status="skipped")

    # The header is sections[0]. We should not translate the header to avoid breaking metadata parsers.
    header = sections[0]
    body_text = "\n\n".join(sections[1:])
    policy_only_media = not has_body_text and bool(msg.media) and all(item.analysis_operations == [] for item in msg.media)
    translated_body = body_text if policy_only_media else translate_to_russian_if_needed(body_text)

    full_text = f"{header}\n\n{translated_body}"
    metadata = _build_metadata(
        msg,
        classified,
        transcription_metadata,
        youtube_sources,
        visual_metadata=visual_metadata,
        has_body_text=has_body_text,
    )

    filepath = _save_normalized(msg, full_text, metadata)
    logger.info(f"  Normalized: {filepath.name} ({len(full_text)} chars)")

    return NormalizedMessageResult(
        status="normalized",
        filepath=str(filepath),
        text=full_text,
        ai_review_created=ai_review_created,
        ai_review_already_reviewed=ai_review_already_reviewed,
        link_review_created=link_review_created,
        uninformative_review_created=uninformative_review_created,
    )


def normalize_batch(messages: list[TelegramMessage]) -> NormalizationBatchResult:
    """
    Normalize a batch of messages.

    Returns normalized texts plus aggregate content/outcome statistics.
    """
    batch = NormalizationBatchResult()

    for msg in messages:
        classified = classify(msg)
        batch.messages_total += 1
        _accumulate_classification_stats(batch, classified)

        try:
            result = normalize_message(msg, classified=classified)
            batch.ai_review_created += result.ai_review_created
            batch.ai_review_already_reviewed += result.ai_review_already_reviewed
            batch.link_review_created += result.link_review_created
            batch.uninformative_review_created += result.uninformative_review_created

            if result.status == "normalized" and result.filepath and result.text is not None:
                batch.normalized_messages += 1
                batch.texts_with_paths.append((result.filepath, result.text))
            else:
                batch.skipped_messages += 1
        except Exception as e:
            batch.failed_messages += 1
            logger.error(
                f"  Error normalizing msg {msg.message_id} from '{msg.channel_name}': {e}",
                exc_info=True,
            )

    return batch


def _accumulate_classification_stats(
    batch: NormalizationBatchResult,
    classified: ClassifiedMessage,
) -> None:
    """Update aggregate counters from one classified message."""
    if classified.has_text:
        batch.messages_with_text += 1

    image_count = max(len(classified.image_paths), sum(item.media_type == "image" for item in classified.media))
    if image_count:
        batch.messages_with_images += 1
        batch.images_total += image_count

    if classified.has_video:
        batch.messages_with_native_video += 1

    if classified.youtube_urls:
        batch.messages_with_youtube += 1
        batch.youtube_links_total += len(classified.youtube_urls)

    if classified.ai_chat_urls:
        batch.messages_with_ai_chat += 1
        batch.ai_chat_links_total += len(classified.ai_chat_urls)

    if classified.web_urls:
        batch.messages_with_web += 1
        batch.web_links_total += len(classified.web_urls)

    reel_links = 0
    post_links = 0
    for url in classified.instagram_urls:
        if "/reel/" in url.lower():
            reel_links += 1
        else:
            post_links += 1

    if reel_links:
        batch.messages_with_instagram_reels += 1
        batch.instagram_reel_links_total += reel_links

    if post_links:
        batch.messages_with_instagram_posts += 1
        batch.instagram_post_links_total += post_links


def _build_header(msg: TelegramMessage) -> str:
    """Build the metadata header line for a normalized document."""
    parts = [f"Канал: {msg.channel_name}"]

    if msg.is_forward and msg.forward_from_name:
        parts.append(f"Источник: {msg.forward_from_name} (пересылка)")

    date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "?"
    parts.append(f"Дата: {date_str}")
    parts.append(f"Пост: {msg.post_url}")

    return "[" + " | ".join(parts) + "]"


def _build_metadata(
    msg: TelegramMessage,
    classified: ClassifiedMessage,
    transcriptions: list[dict] | None = None,
    youtube_sources: list[dict] | None = None,
    *,
    visual_metadata: list[dict] | None = None,
    has_body_text: bool | None = None,
) -> dict:
    """Build structured metadata saved alongside the normalized text."""
    transcriptions = transcriptions or []
    visual_metadata = visual_metadata or []
    media = [
        _media_to_metadata(item, transcriptions, visual_metadata)
        for item in classified.media
    ]
    return {
        "channel_name": msg.channel_name,
        "channel_id": msg.channel_id,
        "channel_username": msg.channel_username,
        "message_id": msg.message_id,
        "date": msg.date.isoformat() if msg.date else None,
        "post_url": msg.post_url,
        "is_forward": msg.is_forward,
        "forward_from_name": msg.forward_from_name,
        "forward_from_id": msg.forward_from_id,
        "forward_date": msg.forward_date.isoformat() if msg.forward_date else None,
        "has_text": classified.has_text,
        "has_body_text": has_body_text,
        "has_images": bool(classified.image_paths) or any(item.media_type == "image" for item in classified.media),
        "image_count": max(len(classified.image_paths), sum(item.media_type == "image" for item in classified.media)),
        "image_paths": [portable_media_path(path) for path in classified.image_paths],
        "has_video": classified.has_video,
        "has_voice": classified.has_voice,
        "has_document": classified.has_document,
        "media_count": len(media),
        "media": media,
        "transcriptions": transcriptions,
        "visual_extractions": visual_metadata,
        "youtube_sources": youtube_sources or [],
        "native_media_paths": [
            portable_media_path(item["file_path"])
            for item in media
            if item["media_type"] in {"video", "audio", "voice"} and item["file_path"]
        ],
        "youtube_urls": classified.youtube_urls,
        "instagram_urls": classified.instagram_urls,
        "ai_chat_urls": classified.ai_chat_urls,
        "web_urls": classified.web_urls,
    }


def _build_media_sections(
    classified: ClassifiedMessage,
    msg: TelegramMessage,
) -> tuple[list[str], list[dict], list[dict]]:
    """Create canonical audio and visual sections for captured native media."""
    sections = []
    transcriptions = []
    visual_metadata = []
    for item in classified.media:
        if item.media_type not in {"video", "audio", "voice", "document"}:
            continue
        operations = _select_operations(item, msg.text)
        if not operations:
            sections.append(_format_media_placeholder(item))
            continue
        if item.media_type == "document":
            try:
                if item.download_status != "downloaded":
                    raise ValueError(item.error or "document_not_downloaded")
                text = extract_document_text(item.file_path, item.mime_type)
                item.text_extraction_status = "success"
                sections.append(f"[Текст документа]\n{text}")
            except Exception as exc:
                item.text_extraction_status = "failed"
                item.text_extraction_error = str(exc)
                sections.append(_format_media_placeholder(item))
            continue
        processed = False
        if "vision" in operations and item.media_type == "video" and item.file_path and item.download_status == "downloaded":
            visual = analyze_video(
                item.file_path,
                source_key=f"{msg.channel_name}:{msg.message_id}:{item.message_id}",
            )
            visual_metadata.append(_visual_to_metadata(item, visual))
            if visual.status == "success" and visual.text.strip():
                sections.append(f"[Текст с экрана (OCR)]\n{visual.text.strip()}")
                processed = True
        if "transcribe" in operations:
            result = transcribe_media(item, msg.channel_name, msg.message_id)
            transcript_meta = _transcription_to_metadata(item, result)
            transcriptions.append(transcript_meta)
            if result.status == "transcribed" and result.text.strip():
                sections.append(_format_transcript_section(item, result))
                processed = True
        if not processed:
            sections.append(_format_media_placeholder(item))

    if not sections and classified.has_video:
        sections.append("[Видео: пост содержал видео - не обработано]")
    if not sections and classified.has_voice:
        sections.append("[Аудио: пост содержал аудио - не обработано]")
    return sections, transcriptions, visual_metadata


def _select_operations(item: TelegramMedia, caption: str) -> list[str]:
    operations = media_operations(caption, item.media_type)
    item.analysis_operations = operations
    item.selection_reason = "explicit_dependency" if operations else "no_explicit_dependency"
    return operations


def _build_image_sections(
    classified: ClassifiedMessage,
    msg: TelegramMessage,
) -> tuple[list[str], list[dict]]:
    """Analyze Telegram images once and retain the canonical visual result.

    Telegram's fetcher records downloaded images both in ``image_paths`` and in
    ``media``.  The latter is the source of truth for the normalized sidecar,
    so the image route analyzes those records directly and passes the exact
    ``VisualExtractionResult`` to both the text and metadata builders.  A small
    compatibility path remains for hand-constructed messages that only provide
    ``image_paths`` (older callers/tests).
    """
    sections: list[str] = []
    visual_metadata: list[dict] = []
    caption = msg.text or ""
    processed_paths: set[str] = set()

    for item in classified.media:
        if item.media_type != "image":
            continue
        processed_paths.add(item.file_path)
        operations = _select_operations(item, caption)
        if not operations:
            visual = VisualExtractionResult(status="not_analyzed")
        elif item.file_path and item.download_status == "downloaded":
            visual = analyze_image(item.file_path, caption=caption)
        else:
            visual = VisualExtractionResult(
                status="unavailable",
                model=llm_backend.active_model_for("default")
                if llm_backend.active_profile() == "luna"
                else "",
                error=item.error or "image_not_downloaded",
                prompt_version=config.MEDIA_VISION_PROMPT_VERSION,
            )
        visual_metadata.append(_visual_to_metadata(item, visual))
        sections.append(_format_image_result(visual, item.file_path))

    # Keep compatibility with manually-created TelegramMessage objects that
    # predate the structured ``media`` records.  Real fetched images take the
    # unified VisualExtractionResult path above and are not analyzed twice.
    for img_path in classified.image_paths:
        if img_path in processed_paths:
            continue
        if media_operations(caption, "image"):
            sections.append(describe_image(img_path, caption=caption))
        else:
            sections.append("[Изображение: вложение не анализировалось по выборочной политике]")

    return sections, visual_metadata


def _format_image_result(result: VisualExtractionResult, source_path: str) -> str:
    """Format image analysis without presenting failures as visual evidence."""
    if result.status == "not_analyzed":
        return "[Изображение: вложение не анализировалось по выборочной политике]"
    if result.status == "success" and result.text.strip():
        return f"[Изображение]\n{result.text.strip()}"
    parts = ["Изображение: описание недоступно", f"status={result.status}"]
    if source_path:
        parts.append(f"path={source_path}")
    if result.error:
        parts.append(f"error={result.error}")
    return "[" + " | ".join(parts) + "]"


def _format_media_placeholder(item: TelegramMedia) -> str:
    if item.media_type == "document":
        prefix = "Документ: вложение не обработано"
    elif item.media_type == "video":
        prefix = "Видео: пост содержал видео - не обработано"
    elif item.media_type == "voice":
        prefix = "Аудио: пост содержал голосовое сообщение - не обработано"
    else:
        prefix = "Аудио: пост содержал аудио - не обработано"

    if item.analysis_operations == []:
        prefix += "; вложение не анализировалось по выборочной политике"
    parts = [prefix, f"status={item.download_status}"]
    if item.file_path:
        parts.append(f"path={item.file_path}")
    if item.mime_type:
        parts.append(f"mime={item.mime_type}")
    if item.error:
        parts.append(f"error={item.error}")
    if item.text_extraction_error:
        parts.append(f"extraction_error={item.text_extraction_error}")
    return "[" + " | ".join(parts) + "]"


def _format_transcript_section(item: TelegramMedia, result: TranscriptionResult) -> str:
    label = "Transcript"
    if item.media_type == "voice":
        label = "Voice transcript"
    elif item.media_type == "video":
        label = "Video transcript"
    artifact_path = portable_project_path(result.artifact_path)
    artifact = f" | artifact={artifact_path}" if artifact_path else ""
    return f"[{label}{artifact}]\n{result.text.strip()}"


def _transcription_to_metadata(item: TelegramMedia, result: TranscriptionResult) -> dict:
    return {
        "media_type": item.media_type,
        "message_id": item.message_id,
        "source_file_path": portable_media_path(item.file_path),
        "status": result.status,
        "artifact_path": portable_project_path(result.artifact_path),
        "error": result.error,
        "route": result.route,
        "attempts": result.attempts,
        "latency_ms": result.latency_ms,
    }


def _media_to_metadata(
    item: TelegramMedia,
    transcriptions: list[dict] | None = None,
    visual_metadata: list[dict] | None = None,
) -> dict:
    payload = {
        "media_type": item.media_type,
        "mime_type": item.mime_type,
        "message_id": item.message_id,
        "file_path": portable_media_path(item.file_path),
        "download_status": item.download_status,
        "error": item.error,
        "attachment_id": item.attachment_id,
        "analysis_operations": item.analysis_operations,
        "selection_reason": item.selection_reason,
        "analysis_status": "not_analyzed" if item.analysis_operations == [] else "requested",
        "text_extraction_status": item.text_extraction_status,
        "text_extraction_error": item.text_extraction_error,
    }
    for transcript in transcriptions or []:
        if (
            transcript["message_id"] == item.message_id
            and portable_media_path(transcript["source_file_path"]) == portable_media_path(item.file_path)
        ):
            payload["transcription_status"] = transcript["status"]
            payload["transcript_path"] = portable_project_path(transcript["artifact_path"])
            payload["transcription_error"] = transcript["error"]
            break
    for visual in visual_metadata or []:
        if (
            visual["message_id"] == item.message_id
            and portable_media_path(visual["source_file_path"]) == portable_media_path(item.file_path)
        ):
            payload["visual_status"] = visual["status"]
            payload["visual_model"] = visual["model"]
            payload["visual_artifact_path"] = portable_project_path(visual["artifact_path"])
            payload["visual_error"] = visual["error"]
            payload["visual_frame_count"] = visual["frame_count"]
            break
    if item.analysis_operations:
        successful = {"vision": payload.get("visual_status") == "success",
                      "transcribe": payload.get("transcription_status") in {"transcribed", "no_speech"},
                      "document_text": item.text_extraction_status == "success"}
        payload["analysis_status"] = "success" if all(successful.get(op, False) for op in item.analysis_operations) else "failed"
    return payload


def _visual_to_metadata(item: TelegramMedia, result: VisualExtractionResult) -> dict:
    return {
        "media_type": item.media_type,
        "message_id": item.message_id,
        "source_file_path": portable_media_path(item.file_path),
        "status": result.status,
        "model": result.model,
        "artifact_path": portable_project_path(result.artifact_path),
        "error": result.error,
        "frame_count": result.frame_count,
        "input_sha256": result.input_sha256,
        "prompt_version": result.prompt_version,
    }


def _strip_urls_from_text(text: str, urls: list[str]) -> str:
    """Remove URLs from text body (since they are expanded into separate sections)."""
    result = text
    for url in urls:
        result = result.replace(url, "")
    # Telegram entities and message text can differ in punctuation, aliases,
    # or URL normalization. Remove any remaining URL token as a final guard so
    # a URL-only post cannot be misclassified as substantive body text.
    result = re.sub(r"(?:https?://|www\.)[^\s<>\"']+", " ", result, flags=re.IGNORECASE)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _get_filepath(msg: TelegramMessage) -> Path:
    """Get the output filepath for a normalized message."""
    channel_dir = config.NORMALIZED_DIR / _sanitize(msg.channel_name)
    channel_dir.mkdir(parents=True, exist_ok=True)
    return channel_dir / f"{msg.message_id}.txt"


def _save_normalized(msg: TelegramMessage, text: str, metadata: dict | None = None) -> Path:
    """Save normalized text plus sidecar metadata to files."""
    filepath = _get_filepath(msg)
    text_bytes = text.encode("utf-8")
    if metadata is not None:
        metadata = {**metadata, "normalized_text_sha256": hashlib.sha256(text_bytes).hexdigest()}
        metadata_bytes = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
        meta_path = filepath.with_suffix(".meta.json")
        recover_atomic_pair(filepath, meta_path, journal_suffix="normalized")
        if meta_path.is_file():
            # A legacy sidecar may survive without its text. Roll back to empty
            # input in that case so recovery cannot accept new text as old data.
            atomic_write_pair(filepath, text_bytes, meta_path, metadata_bytes,
                              original_first=filepath.read_bytes() if filepath.is_file() else b"",
                              original_second=meta_path.read_bytes(),
                              journal_suffix="normalized")
            return filepath
    atomic_write_bytes(filepath, text_bytes)
    # Publish metadata last; its hash detects an interrupted pair overwrite.
    if metadata is not None:
        atomic_write_bytes(filepath.with_suffix(".meta.json"), metadata_bytes)
    return filepath


def read_normalized_meta(path: Path) -> NormalizedMeta:
    """Validate saved input readiness, including interrupted pair publication."""
    recover_atomic_pair(path, path.with_suffix(".meta.json"), journal_suffix="normalized")
    text = path.read_bytes()
    if not text.decode("utf-8").strip():
        raise ValueError(f"Empty normalized text: {path}")
    metadata = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    meta = NormalizedMeta.model_validate(metadata)
    expected_hash = metadata.get("normalized_text_sha256")
    if expected_hash is not None and expected_hash != hashlib.sha256(text).hexdigest():
        raise ValueError(f"Incomplete normalized text/metadata pair: {path}")
    return meta


def _sanitize(name: str) -> str:
    """Sanitize string for use as directory name."""
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip()
