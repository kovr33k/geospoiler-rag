"""
Content Classifier — determines content_type from meta.json + normalized text.

Reads the sidecar .meta.json produced by the normalizer and the body text
to classify a post into one of the v2 content types.
"""

import logging
import re

logger = logging.getLogger("geospoiler.enricher.classifier")

# ── Content types (v2) ─────────────────────────────────────────────────────
CONTENT_TYPES = [
    "telegram_post",            # Original Telegram post (text, analysis, quote, image+text)
    "telegram_forward",         # Forwarded post
    "youtube_transcript",       # YouTube video with transcript
    "instagram_text",           # Instagram content
    "web_article_text",         # External web article
    "mixed_normalized_text",    # Native video, image-only, mixed media
    "unknown",                  # Fallback
]

# YouTube marker left by the normalizer's youtube_handler
_YOUTUBE_MARKER_RE = re.compile(r"\[YouTube:", re.IGNORECASE)
_STANDALONE_YOUTUBE_RE = re.compile(r"^\[YouTube\]\s*$", re.IGNORECASE | re.MULTILINE)
# Vision API description marker from image_handler
_IMAGE_DESC_RE = re.compile(r"\[Изображение(?:\s+\d+)?:", re.IGNORECASE)
# Native media placeholders from normalizer
_MEDIA_PLACEHOLDER_RE = re.compile(r"\[(?:Видео:|Аудио:).*не обработано.*\]", re.IGNORECASE)
# AI-chat placeholder from normalizer
_AI_CHAT_PLACEHOLDER_RE = re.compile(
    r"\[(?:AI-диалог:|Отправлено в очередь на ручной просмотр:)", re.IGNORECASE
)
# Instagram marker
_INSTAGRAM_MARKER_RE = re.compile(r"\[Instagram", re.IGNORECASE)
# Web article marker from web_handler
_WEB_MARKER_RE = re.compile(r"\[Веб-страница:", re.IGNORECASE)
# Header line
_HEADER_RE = re.compile(r"^\[Канал:.*\]\s*$")


def classify_content(meta: dict, normalized_text: str) -> str:
    """
    Determine the content_type of a post based on its metadata and normalized text.

    Args:
        meta: Parsed contents of the .meta.json sidecar file.
        normalized_text: Full text from the .txt normalized file.

    Returns:
        One of the CONTENT_TYPES strings.
    """
    lines = normalized_text.split("\n")
    body_lines = [ln for ln in lines if not _HEADER_RE.match(ln.strip())]
    body = "\n".join(body_lines).strip()

    has_youtube = bool(meta.get("youtube_urls"))
    has_instagram = bool(meta.get("instagram_urls"))
    has_ai_chat = bool(meta.get("ai_chat_urls"))
    has_web = bool(meta.get("web_urls"))
    has_images = meta.get("has_images", False)
    has_video = meta.get("has_video", False)
    has_text = meta.get("has_text", False)
    has_body_text = meta.get("has_body_text")
    is_forward = meta.get("is_forward", False)

    # AI chat → handled by triage as "review", but classify if it reaches here
    if has_ai_chat:
        return "mixed_normalized_text"

    # A link-only post is represented by the dedicated YouTube episode job.
    # Mixed posts keep their Telegram text as a normal Telegram card.
    standalone_youtube = has_youtube and (
        has_body_text is False
        or (has_body_text is None and _STANDALONE_YOUTUBE_RE.search(normalized_text))
    )
    if standalone_youtube:
        return "youtube_transcript"

    # Instagram
    if has_instagram and has_body_text is not True:
        return "instagram_text"

    # Web article
    if has_web and _WEB_MARKER_RE.search(body):
        return "web_article_text"

    # Native video / image-only / mixed media without substantive text
    if (has_video or has_images) and not has_text:
        return "mixed_normalized_text"
    if has_video and has_text and _body_text_length(body) < 50:
        return "mixed_normalized_text"

    # Forwarded text posts
    if is_forward:
        return "telegram_forward"

    # Original text posts (all lengths: short quotes, analysis, news — same type)
    if has_text:
        return "telegram_post"

    return "unknown"


def _body_text_length(body: str) -> int:
    """
    Length of body text excluding markers, placeholders, and image descriptions.
    Gives a rough measure of 'real' textual content.
    """
    clean_lines = []
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        # Skip normalized markers
        if any(pattern.match(stripped) for pattern in [
            _YOUTUBE_MARKER_RE, _IMAGE_DESC_RE, _MEDIA_PLACEHOLDER_RE,
            _AI_CHAT_PLACEHOLDER_RE, _INSTAGRAM_MARKER_RE, _WEB_MARKER_RE,
        ]):
            continue
        clean_lines.append(stripped)
    return sum(len(ln) for ln in clean_lines)


def _estimate_youtube_section_length(body: str) -> int:
    """
    Estimate the length of YouTube transcript/content in the body.
    Everything after the [YouTube: ...] marker until end or next marker.
    """
    match = _YOUTUBE_MARKER_RE.search(body)
    if not match:
        return 0
    # Find the end of the YouTube marker line
    start = body.find("\n", match.start())
    if start == -1:
        return 0
    # Text from after the marker to end of body
    yt_section = body[start:]
    return len(yt_section.strip())
