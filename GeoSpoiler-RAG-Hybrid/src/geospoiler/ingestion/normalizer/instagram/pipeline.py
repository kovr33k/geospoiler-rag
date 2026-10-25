"""Instagram extraction pipeline orchestration."""

import logging
import shutil
from datetime import datetime

from geospoiler import config
from geospoiler.ingestion.normalizer.instagram.audio import _extract_audio, _transcribe_audio
from geospoiler.ingestion.normalizer.instagram.builder import (
    _caption_only,
    _queue_long_reel_for_review,
    build_carousel_text,
    build_reel_text,
)
from geospoiler.ingestion.normalizer.instagram.cache import (
    _cache_signature,
    _is_review_queue_placeholder,
    _read_cache,
    _write_cache,
)
from geospoiler.ingestion.normalizer.instagram.downloader import (
    _download_carousel_images,
    _download_video,
    _extract_post_id,
    _get_info_ytdlp,
    _get_subtitles_from_info,
    _srt_to_text,
    canonicalize_instagram_url,
)
from geospoiler.ingestion.normalizer.instagram.frames import (
    _compute_edge_density,
    _compute_phash,
    _dedup_frames,
    _extract_frames,
    _filter_empty_frames,
    _hamming_distance,
)
from geospoiler.ingestion.normalizer.instagram.vision import (
    _describe_single_image,
    _ocr_frames_batched,
    _ocr_single_batch,
    _summarize_reel,
)
from geospoiler.ingestion.normalizer.review_queue import REVIEW_TYPE_EXTERNAL_LINK
from geospoiler.ingestion.normalizer.review_queue import queue_item as queue_review_item

logger = logging.getLogger("geospoiler.normalizer.instagram")

# Keep these reasons stable: review tooling and operators use them to
# distinguish a media failure from a normal caption-only extraction.
REEL_MEDIA_FAILURE_REASON = (
    "Instagram Reel deep extraction failed: audio=missing; visual=missing"
)
CAROUSEL_MEDIA_FAILURE_REASON = (
    "Instagram carousel extraction failed: visual=missing"
)

def extract_instagram_text(
    url: str,
    *,
    channel_name: str = "instagram",
    message_id: int = 0,
    message_text: str = "",
    message_date: datetime | None = None,
) -> str:
    """
    Extract text content from an Instagram post/Reel.
    Returns formatted text with caption, transcription, OCR, and summary.
    """
    canonical_url = canonicalize_instagram_url(url)
    post_id = _extract_post_id(canonical_url)

    # Check cache first
    if post_id:
        cached = _read_cache(post_id)
        if cached and (
            not config.INSTAGRAM_DEEP_EXTRACT_ENABLED
            or _has_deep_media_content(cached, is_reel="/reel/" in canonical_url.lower())
        ):
            logger.info(f"  Instagram cache hit: {post_id}")
            return cached
        if cached:
            logger.warning("Ignoring caption-only Instagram cache entry for %s", canonical_url)

    # Get metadata from yt-dlp
    info = _get_info_ytdlp(canonical_url)
    if not info:
        logger.warning(f"Could not extract Instagram content from {canonical_url}")
        return _queue_instagram_extraction_failure(
            canonical_url,
            channel_name=channel_name,
            message_id=message_id,
            message_text=message_text,
            message_date=message_date,
            reason=(
                "Instagram media metadata unavailable to yt-dlp; the page may still be "
                "visible in a browser (empty media response or extractor access block)"
            ),
        )

    caption = (info.get("description") or info.get("title") or "").strip()
    uploader = info.get("uploader") or info.get("channel") or ""
    duration = info.get("duration") or 0
    is_reel = "/reel/" in canonical_url.lower()

    # Deep extract path
    if config.INSTAGRAM_DEEP_EXTRACT_ENABLED:
        if is_reel:
            result = _deep_extract_reel(
                canonical_url,
                info,
                caption,
                uploader,
                duration,
                channel_name=channel_name,
                message_id=message_id,
                message_text=message_text,
                message_date=message_date,
            )
            if result and (
                _is_review_queue_placeholder(result)
                or _has_deep_media_content(result, is_reel=True)
            ):
                if post_id and not _is_review_queue_placeholder(result):
                    _write_cache(post_id, result)
                return result
            return _queue_instagram_extraction_failure(
                canonical_url,
                channel_name=channel_name,
                message_id=message_id,
                message_text=message_text,
                message_date=message_date,
                reason=REEL_MEDIA_FAILURE_REASON,
            )
        else:
            result = _deep_extract_carousel(canonical_url, info, caption, uploader)
            if result and (
                _is_review_queue_placeholder(result)
                or _has_deep_media_content(result, is_reel=False)
            ):
                if post_id:
                    _write_cache(post_id, result)
                return result
            return _queue_instagram_extraction_failure(
                canonical_url,
                channel_name=channel_name,
                message_id=message_id,
                message_text=message_text,
                message_date=message_date,
                reason=CAROUSEL_MEDIA_FAILURE_REASON,
                post_type="Post",
            )

    # Fallback: caption-only (original behavior)
    result = _caption_only(canonical_url, info, caption, uploader, is_reel)
    if post_id and not config.INSTAGRAM_DEEP_EXTRACT_ENABLED:
        _write_cache(post_id, result)
    return result


def _queue_instagram_extraction_failure(
    url: str,
    *,
    channel_name: str,
    message_id: int,
    message_text: str,
    message_date: datetime | None,
    reason: str,
    post_type: str = "Reel",
) -> str:
    """Make source-access failures visible without pretending caption-only success."""
    review = queue_review_item(
        review_type=REVIEW_TYPE_EXTERNAL_LINK,
        channel_name=channel_name,
        message_id=message_id,
        message_text=message_text,
        message_date=message_date,
        url=url,
        reason=reason,
        reopen_processed=True,
    )
    return f"[Instagram {post_type}: {url}]\n{review.placeholder_text}"

def _deep_extract_reel(
    url: str,
    info: dict,
    caption: str,
    uploader: str,
    duration: float,
    *,
    channel_name: str,
    message_id: int,
    message_text: str,
    message_date: datetime | None,
) -> str | None:
    """Full pipeline: video → audio transcription + frame OCR → LLM summary."""

    # Check duration limits — queue for manual review if too long
    if duration > config.INSTAGRAM_MAX_VIDEO_DURATION_SEC:
        logger.info(
            f"Instagram Reel longer than {config.INSTAGRAM_MAX_VIDEO_DURATION_SEC}s "
            f"({duration}s), queuing for review: {url}"
        )
        return _queue_long_reel_for_review(
            url,
            caption,
            uploader,
            duration,
            channel_name=channel_name,
            message_id=message_id,
            message_text=message_text,
            message_date=message_date,
        )

    work_dir = config.MEDIA_CACHE_DIR / "instagram_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    post_id = _extract_post_id(url) or "unknown"

    video_path = None
    audio_path = None
    frames_dir = None

    audio_status = ""
    visual_status = ""
    try:
        # Step 1: Download video
        video_path = _download_video(url, work_dir, post_id)
        if not video_path:
            logger.warning(f"Could not download Instagram video: {url}")
            return None

        # Check file size
        size_mb = video_path.stat().st_size / (1024 * 1024)
        if size_mb > config.INSTAGRAM_MAX_VIDEO_SIZE_MB:
            logger.warning(f"Instagram video too large ({size_mb:.1f}MB), skipping deep extract")
            return None

        # Step 2: Extract audio and transcribe.  Audio and visual failures are
        # isolated so a successful branch remains usable as a partial result.
        transcript = ""
        try:
            audio_path = _extract_audio(video_path, work_dir, post_id)
            if audio_path:
                if not config.TRANSCRIPTION_API_KEY or config.TRANSCRIPTION_API_KEY == "your-api-key-here":
                    audio_status = "unavailable:missing_key"
                else:
                    transcript = _transcribe_audio(audio_path) or ""
                    audio_status = "success" if transcript.strip() else "empty"
            else:
                audio_status = "unavailable"
        except Exception as exc:
            audio_status = f"failed: {exc}"
            logger.warning("Instagram Reel audio branch failed for %s: %s", url, exc)

        # Step 3: Extract and process frames
        frames_dir = work_dir / f"{post_id}_frames"
        frames_dir.mkdir(exist_ok=True)
        ocr_texts = []
        try:
            raw_frames = _extract_frames(video_path, frames_dir, config.INSTAGRAM_FRAME_INTERVAL_SEC)
            if raw_frames:
                # Deduplicate
                unique_frames = _dedup_frames(raw_frames)
                logger.info(
                    f"  Frames: {len(raw_frames)} extracted → {len(unique_frames)} after dedup"
                )

                # Filter empty
                content_frames = _filter_empty_frames(unique_frames)
                logger.info(
                    f"  Frames: {len(unique_frames)} unique → {len(content_frames)} with content"
                )

                # Batched OCR
                if content_frames:
                    ocr_texts = _ocr_frames_batched(
                        content_frames, config.INSTAGRAM_FRAME_BATCH_SIZE
                    )
                visual_status = "success" if ocr_texts else "empty"
            else:
                visual_status = "unavailable"
        except Exception as exc:
            visual_status = f"failed: {exc}"
            logger.warning("Instagram Reel visual branch failed for %s: %s", url, exc)

        ocr_combined = "\n".join(t for t in ocr_texts if t)
        # A caption is not a media extraction result.  Do not return a
        # truthy caption-only document when both content-bearing branches
        # failed; the caller will route this source into review and will not
        # write it to the normal Instagram success cache.
        if not transcript.strip() and not ocr_combined.strip():
            logger.warning(
                "Instagram Reel media extraction produced no transcript or visual text: %s",
                url,
            )
            return None

        summary = None
        if transcript or ocr_combined:
            summary = _summarize_reel(caption, transcript, ocr_combined, uploader)

        return build_reel_text(
            url,
            caption,
            uploader,
            transcript,
            ocr_combined,
            summary,
            audio_status=audio_status,
            visual_status=visual_status,
        )

    except Exception as e:
        logger.error(f"Instagram deep extract error for {url}: {e}", exc_info=True)
        return None
    finally:
        # Cleanup
        if video_path and video_path.exists():
            video_path.unlink(missing_ok=True)
        if audio_path and audio_path.exists():
            audio_path.unlink(missing_ok=True)
        if frames_dir and frames_dir.exists():
            shutil.rmtree(frames_dir, ignore_errors=True)


# ─────────────────────── Deep Extract: Carousel ───────────────────────

def _deep_extract_carousel(
    url: str, info: dict, caption: str, uploader: str
) -> str | None:
    """Download carousel images and analyze each with Vision API."""

    work_dir = config.MEDIA_CACHE_DIR / "instagram_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    post_id = _extract_post_id(url) or "unknown"
    carousel_dir = work_dir / f"{post_id}_carousel"
    carousel_dir.mkdir(exist_ok=True)

    try:
        image_paths = _download_carousel_images(url, carousel_dir, post_id, info)
        if not image_paths:
            logger.info(f"No carousel images downloaded for {url}")
            return None

        image_descriptions = [
            _describe_single_image(img_path) for img_path in image_paths
        ]
        if not any(_is_successful_visual_description(description) for description in image_descriptions):
            logger.warning(
                "Instagram carousel visual extraction produced no usable image description: %s",
                url,
            )
            return None
        return build_carousel_text(url, caption, uploader, image_descriptions)

    except Exception as e:
        logger.error(f"Instagram carousel extract error for {url}: {e}", exc_info=True)
        return None
    finally:
        if carousel_dir.exists():
            shutil.rmtree(carousel_dir, ignore_errors=True)


def _is_successful_visual_description(description: str) -> bool:
    """Return whether a carousel description contains visual evidence.

    The legacy/current vision helpers return human-readable failure markers,
    so a non-empty string alone is not sufficient to treat an image as
    successfully analyzed.
    """
    value = str(description or "").strip().casefold()
    if not value:
        return False
    failure_markers = (
        "описание недоступно",
        "visual analysis failed",
        "vision api не настроен",
        "ошибка vision api",
        "таймаут vision api",
    )
    return not any(marker in value for marker in failure_markers)


def _has_deep_media_content(text: str, *, is_reel: bool) -> bool:
    """Validate a deep-extraction result before returning or caching it."""
    value = str(text or "")
    if is_reel:
        return "[Транскрипция аудио]" in value or "[Текст с экрана (OCR)]" in value
    return "[Изображение " in value


# Preserve helper exports through the instagram_handler module alias.
__all__ = [
    "_cache_signature",
    "_caption_only",
    "_compute_edge_density",
    "_compute_phash",
    "_dedup_frames",
    "_deep_extract_carousel",
    "_deep_extract_reel",
    "_has_deep_media_content",
    "_describe_single_image",
    "_download_carousel_images",
    "_download_video",
    "_extract_audio",
    "_extract_frames",
    "_extract_post_id",
    "_filter_empty_frames",
    "_get_info_ytdlp",
    "_get_subtitles_from_info",
    "_hamming_distance",
    "_is_review_queue_placeholder",
    "_ocr_frames_batched",
    "_ocr_single_batch",
    "_queue_long_reel_for_review",
    "_queue_instagram_extraction_failure",
    "_read_cache",
    "_srt_to_text",
    "_summarize_reel",
    "_transcribe_audio",
    "_write_cache",
    "canonicalize_instagram_url",
    "config",
    "extract_instagram_text",
]
