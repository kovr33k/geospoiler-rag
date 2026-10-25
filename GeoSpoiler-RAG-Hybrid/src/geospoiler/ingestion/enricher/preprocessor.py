"""
Preprocessor — prepares clean text for LLM extraction.

Responsibilities:
  - Separate header from body
  - Load .meta.json
  - Find and remove native/legacy media placeholders
  - Return clean_text + ignored_blocks
  - Does NOT remove political rhetoric or "filler" — that's LLM's job
"""

import re
from dataclasses import dataclass, field

from geospoiler.contracts.models import IgnoredBlock

_HEADER_RE = re.compile(r"^\[Канал:.*\]\s*$")

_MEDIA_PATTERNS = [
    re.compile(r"^\[Изображение(?:\s+\d+)?(?::.*?)?\]\s*$", re.IGNORECASE),
    re.compile(r"^\[Видео:.*?\]\s*$", re.IGNORECASE),
    re.compile(r"^\[Аудио:.*?\]\s*$", re.IGNORECASE),
    re.compile(r"^\[Video transcript.*?\]\s*$", re.IGNORECASE),
    re.compile(r"^\[Photo\]\s*$", re.IGNORECASE),
    re.compile(r"^\[Media omitted\]\s*$", re.IGNORECASE),
    re.compile(r"^\[voice message not processed\]\s*$", re.IGNORECASE),
    re.compile(r"^\[AI-диалог:.*?\]\s*$", re.IGNORECASE),
    re.compile(r"^\[Отправлено в очередь на ручной просмотр:.*?\]\s*$", re.IGNORECASE),
    re.compile(r"^\[Instagram.*?\]\s*$", re.IGNORECASE),
    re.compile(r"^\[YouTube transcript stored separately:.*?\]\s*$", re.IGNORECASE),
]


@dataclass
class PreprocessedText:
    header: str
    clean_text: str
    ignored_blocks: list[IgnoredBlock] = field(default_factory=list)
    body_char_count: int = 0


def preprocess(normalized_text: str) -> PreprocessedText:
    """
    Preprocess normalized text: extract header, find and mask media placeholders.

    Returns PreprocessedText with clean body and list of ignored blocks.
    """
    lines = normalized_text.split("\n")
    header = ""
    body_lines: list[str] = []
    ignored: list[IgnoredBlock] = []

    for line in lines:
        stripped = line.strip()

        if not header and _HEADER_RE.match(stripped):
            header = stripped
            continue

        if _is_media_placeholder(stripped):
            block_type = _classify_placeholder(stripped)
            ignored.append(IgnoredBlock(type=block_type, text=stripped))
            continue

        body_lines.append(line)

    clean_text = "\n".join(body_lines).strip()

    return PreprocessedText(
        header=header,
        clean_text=clean_text,
        ignored_blocks=ignored,
        body_char_count=len(clean_text),
    )


def _is_media_placeholder(line: str) -> bool:
    if not line.startswith("["):
        return False
    return any(p.match(line) for p in _MEDIA_PATTERNS)


def _classify_placeholder(line: str) -> str:
    lower = line.lower()
    if "изображение" in lower or "photo" in lower:
        return "image"
    if "видео" in lower or "video" in lower:
        return "video"
    if "аудио" in lower or "voice" in lower:
        return "audio"
    if "instagram" in lower:
        return "instagram"
    if "youtube transcript stored separately" in lower:
        return "youtube"
    if "ai-диалог" in lower:
        return "ai_chat"
    if "media omitted" in lower:
        return "media_omitted"
    return "unknown"
