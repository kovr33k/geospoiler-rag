"""
Message Router — classifies a TelegramMessage and routes it to appropriate handlers.

Classification priority:
1. AI chat links → review_queue (manual review)
2. YouTube links → youtube_handler
3. Instagram links → instagram_handler
4. Web links → web_handler (for remaining URLs)
5. Images → image_handler (via Vision API)
6. Plain text → text_handler

A single message can trigger MULTIPLE handlers (e.g. text + image + YouTube link).
"""

import logging
from dataclasses import dataclass, field

from geospoiler import config
from geospoiler.ingestion.fetcher.telegram_client import TelegramMedia, TelegramMessage
from geospoiler.ingestion.normalizer.instagram.downloader import canonicalize_instagram_url, is_valid_instagram_url
from geospoiler.ingestion.normalizer.youtube_handler import is_valid_youtube_url

logger = logging.getLogger("geospoiler.router")


@dataclass
class ClassifiedMessage:
    """Result of routing: what handlers should process this message."""

    original: TelegramMessage
    has_text: bool = False
    youtube_urls: list[str] = field(default_factory=list)
    instagram_urls: list[str] = field(default_factory=list)
    ai_chat_urls: list[str] = field(default_factory=list)
    web_urls: list[str] = field(default_factory=list)
    image_paths: list[str] = field(default_factory=list)
    has_video: bool = False  # Just a flag, no processing
    has_voice: bool = False
    has_document: bool = False
    media: list[TelegramMedia] = field(default_factory=list)


def classify(msg: TelegramMessage) -> ClassifiedMessage:
    """Classify a message into content types for handler routing."""

    result = ClassifiedMessage(original=msg)

    # Text
    if msg.text and msg.text.strip():
        result.has_text = True

    # Classify each URL
    for url in msg.urls:
        url_clean = url.rstrip(".,;:!?)")

        # AI chat links → manual review queue
        if any(p.search(url_clean) for p in config.AI_CHAT_PATTERNS):
            result.ai_chat_urls.append(url_clean)
            logger.debug(f"  AI chat URL: {url_clean}")
            continue

        # YouTube
        if is_valid_youtube_url(url_clean):
            result.youtube_urls.append(url_clean)
            logger.debug(f"  YouTube URL: {url_clean}")
            continue

        # Instagram. Canonicalize archived kkinstagram links before matching.
        instagram_url = canonicalize_instagram_url(url_clean)
        if is_valid_instagram_url(instagram_url):
            result.instagram_urls.append(instagram_url)
            logger.debug(f"  Instagram URL: {instagram_url}")
            continue

        # Any other web URL
        result.web_urls.append(url_clean)
        logger.debug(f"  Web URL: {url_clean}")

    # Images
    result.image_paths = msg.image_paths

    # Video flag
    result.has_video = msg.has_video
    result.has_voice = msg.has_voice
    result.has_document = msg.has_document
    result.media = list(msg.media)

    return result
