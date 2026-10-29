"""Cache helpers for Instagram extraction."""

import json
import logging
from datetime import UTC, datetime

from geospoiler import config
from geospoiler.ingestion.normalizer.instagram.downloader import installed_ytdlp_version
from geospoiler.providers import llm_backend

logger = logging.getLogger("geospoiler.normalizer.instagram")
_INSTAGRAM_CACHE_VERSION = 6

def _is_review_queue_placeholder(text: str) -> bool:
    """Return True for review placeholders that should not be cached as extraction output."""
    return (
        "Отправлено в очередь на ручной просмотр:" in text
        or "Уже обработано:" in text
    )

def _cache_signature() -> dict:
    return {
        "deep_extract_enabled": config.INSTAGRAM_DEEP_EXTRACT_ENABLED,
        "transcription_enabled": config.TRANSCRIPTION_ENABLED,
        "transcription_base_url": config.TRANSCRIPTION_BASE_URL,
        "transcription_model": config.TRANSCRIPTION_MODEL,
        "transcription_language": config.TRANSCRIPTION_LANGUAGE,
        "vision_base_url": config.INSTAGRAM_VISION_BASE_URL,
        "vision_api_base_url": config.VISION_BASE_URL,
        "vision_api_model": config.VISION_MODEL,
        "vision_model": config.INSTAGRAM_VISION_MODEL,
        "media_vision_backend": config.MEDIA_VISION_BACKEND,
        "media_vision_prompt_version": config.MEDIA_VISION_PROMPT_VERSION,
        "media_vision_max_images": config.MEDIA_VISION_MAX_IMAGES,
        "media_vision_max_frames": config.MEDIA_VISION_MAX_FRAMES,
        "media_vision_frame_interval_sec": config.MEDIA_VISION_FRAME_INTERVAL_SEC,
        "media_vision_frame_batch_size": config.MEDIA_VISION_FRAME_BATCH_SIZE,
        "media_vision_fallback_enabled": config.MEDIA_VISION_FALLBACK_ENABLED,
        "instagram_frame_interval_sec": config.INSTAGRAM_FRAME_INTERVAL_SEC,
        "instagram_frame_batch_size": config.INSTAGRAM_FRAME_BATCH_SIZE,
        "ytdlp_min_version": config.YTDLP_MIN_VERSION,
        "ytdlp_require_current": config.YTDLP_REQUIRE_CURRENT,
        "ytdlp_installed_version": installed_ytdlp_version(),
        "llm_profile": llm_backend.active_profile(),
        "llm_model": llm_backend.active_model_for("default"),
        "llm_base_url": config.LLM_BASE_URL,
        "codex_fallback_to_api": config.CODEX_FALLBACK_TO_API,
    }

def _read_cache(post_id: str) -> str | None:
    """Read cached extraction result for a post."""
    cache_path = config.INSTAGRAM_CACHE_DIR / f"{post_id}.json"
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("cache_version") != _INSTAGRAM_CACHE_VERSION:
            return None
        if payload.get("signature") != _cache_signature():
            return None
        text = payload.get("text")
        return text if text else None
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return None

def _write_cache(post_id: str, text: str) -> None:
    """Write extraction result to cache."""
    cache_path = config.INSTAGRAM_CACHE_DIR / f"{post_id}.json"
    try:
        config.INSTAGRAM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "post_id": post_id,
            "text": text,
            "cached_at": datetime.now(UTC).isoformat(),
            "cache_version": _INSTAGRAM_CACHE_VERSION,
            "signature": _cache_signature(),
        }
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(f"Failed to write Instagram cache for {post_id}: {e}")


# ─────────────────────── Utilities ───────────────────────
