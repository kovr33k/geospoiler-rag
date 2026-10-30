"""Exact duplicates: the same Telegram post published more than once.

A forward or repost keeps the original text, attachment ids and links, so these
three identify it. Generated sections of a normalized text (the header and media
analysis) start with "[" and are left out: they name the channel and may differ
between runs.
"""

import hashlib
import json
import threading

from geospoiler.contracts.models import NormalizedMeta
from geospoiler.ingestion.normalizer.media_selection import caption_has_letters_or_digits


def post_content_key(meta: NormalizedMeta, normalized_text: str) -> str | None:
    """Identity of a post's content, or None when there is nothing to compare."""
    caption = "\n\n".join(
        " ".join(paragraph.split())
        for paragraph in normalized_text.split("\n\n")
        if paragraph.strip() and not paragraph.lstrip().startswith("[")
    )
    attachments = [item.get("attachment_id") if isinstance(item, dict) else None for item in meta.media]
    if not all(attachments):
        # Media without an id cannot be told apart from different media.
        return None
    if not caption_has_letters_or_digits(caption) and not attachments:
        return None
    links = sorted({*meta.youtube_urls, *meta.instagram_urls, *meta.ai_chat_urls, *meta.web_urls})
    encoded = json.dumps([caption, sorted(map(str, attachments)), links], ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class DuplicateRoots:
    """Root card per content key, shared by the enrichment workers of one run.

    A worker holds the key's lock while it enriches, so a copy that arrives at
    the same time waits for the root instead of calling the LLM again.
    """

    def __init__(self, roots: dict[str, dict] | None = None):
        self._roots = dict(roots or {})
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, key: str) -> dict | None:
        with self._guard:
            return self._roots.get(key)

    def set(self, key: str, card: dict) -> None:
        with self._guard:
            self._roots[key] = card
