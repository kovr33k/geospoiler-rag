"""
Auto-Triage — decides keep / review without deleting useful material.

The Telegram folder is already curated by the user, so recall is more important
than aggressive noise filtering. Very short posts can carry important theses.

Rules:
- review: AI chats, placeholder-only native video, content that needs manual extraction
- keep: everything with any meaningful text, including short posts and video captions
"""

import logging
import re

logger = logging.getLogger("geospoiler.enricher.triage")

# Triage categories
TRIAGE_KEEP = "keep"
TRIAGE_REVIEW = "review"
TRIAGE_LOW_VALUE = "low-value"  # Legacy value: no new cards should use it.

# Normalized markers that indicate placeholder-only content
_PLACEHOLDER_RE = re.compile(
    r"^\[(?:Видео:|Аудио:|AI-диалог:|Отправлено в очередь|Уже обработано:|Веб-страница:.*ошибка).*\]$",
    re.IGNORECASE,
)
_HEADER_RE = re.compile(r"^\[Канал:.*\]\s*$")


def auto_triage(
    content_type: str,
    meta: dict,
    normalized_text: str,
) -> tuple[str, str]:
    """
    Determine triage status for a post.

    Args:
        content_type: Result from content_classifier.
        meta: Parsed .meta.json data.
        normalized_text: Full normalized text.

    Returns:
        Tuple of (triage_status, reason).
    """
    body = _extract_body(normalized_text)
    body_stripped = body.strip()

    # ── Rule 1: AI chat → review (existing review_queue mechanism handles these) ──
    if content_type == "ai_chat" or meta.get("ai_chat_urls"):
        return TRIAGE_REVIEW, "AI chat — requires manual review"

    # ── Rule 2: Native video without useful caption → review ──
    if content_type == "video_native":
        if _has_meaningful_text(body):
            return TRIAGE_KEEP, "Native video has useful caption text"
        return TRIAGE_REVIEW, "Native video without useful caption — needs Whisper"

    # ── Rule 3: Empty body ──
    if not body_stripped:
        has_images = meta.get("has_images", False)
        if has_images:
            # Image with very short/no text might still be useful
            return TRIAGE_KEEP, "Image post with minimal text"
        return TRIAGE_REVIEW, "Empty post body"

    # ── Rule 4: Body is only placeholder lines ──
    if not _has_meaningful_text(body):
        return TRIAGE_REVIEW, "Only placeholder markers, no extracted text"

    # ── Default: keep ──
    return TRIAGE_KEEP, "Content meets minimum quality threshold"


def _extract_body(normalized_text: str) -> str:
    """Strip the metadata header line, return only the body."""
    lines = normalized_text.split("\n")
    body_lines = []
    for line in lines:
        if _HEADER_RE.match(line.strip()):
            continue
        body_lines.append(line)
    return "\n".join(body_lines)


def _has_meaningful_text(body: str) -> bool:
    """Return True if the body contains anything beyond technical placeholders."""
    body_lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
    if not body_lines:
        return False

    meaningful_lines = [ln for ln in body_lines if not _PLACEHOLDER_RE.match(ln)]
    return bool("\n".join(meaningful_lines).strip())
