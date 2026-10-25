"""Deterministic segmentation for long YouTube transcripts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SEGMENT_THRESHOLD_CHARS = 12_000
SEGMENT_THRESHOLD_SECONDS = 15 * 60
SEGMENT_TARGET_CHARS = 6_000
SEGMENT_MIN_CHARS = 3_500
SEGMENT_MAX_CHARS = 9_000


@dataclass(frozen=True)
class SegmentSpec:
    index: int
    text: str
    char_range: tuple[int, int]
    start_seconds: float | None = None
    end_seconds: float | None = None
    chapter_titles: tuple[str, ...] = field(default_factory=tuple)


def needs_youtube_segments(text: str, duration_seconds: float | None) -> bool:
    """Return whether a source should use the long-form YouTube profile."""
    return (
        len(text.strip()) >= SEGMENT_THRESHOLD_CHARS
        or (duration_seconds is not None and duration_seconds >= SEGMENT_THRESHOLD_SECONDS)
    )


def build_segment_specs(
    transcript_text: str,
    *,
    cues: list[dict] | list[object] | None = None,
    chapters: list[dict] | None = None,
) -> list[SegmentSpec]:
    """Build non-overlapping core segments from timed cues or plain text."""
    text = transcript_text.strip()
    if not text:
        return []

    normalized_cues = _normalize_cues(cues or [])
    if normalized_cues:
        specs = _from_cues(normalized_cues, chapters or [])
    else:
        specs = _from_plain_text(text)

    return [
        SegmentSpec(
            index=index,
            text=spec.text,
            char_range=spec.char_range,
            start_seconds=spec.start_seconds,
            end_seconds=spec.end_seconds,
            chapter_titles=spec.chapter_titles,
        )
        for index, spec in enumerate(specs)
        if spec.text.strip()
    ]


def youtube_timestamp_url(url: str, seconds: float | None) -> str:
    """Add a timestamp only when a real cue start is available."""
    if seconds is None:
        return ""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["t"] = str(max(0, int(seconds))) + "s"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@dataclass(frozen=True)
class _RawSegment:
    text: str
    char_range: tuple[int, int]
    start_seconds: float | None
    end_seconds: float | None
    chapter_titles: tuple[str, ...]


def _from_cues(cues: list[dict], chapters: list[dict]) -> list[_RawSegment]:
    segments: list[_RawSegment] = []
    parts: list[str] = []
    part_start = 0
    char_pos = 0
    start_seconds: float | None = None
    end_seconds: float | None = None
    chapter_names: list[str] = []

    for cue in cues:
        cue_text = cue["text"].strip()
        if not cue_text:
            continue
        pieces = _split_text_piece(cue_text, SEGMENT_MAX_CHARS)
        for piece_index, piece in enumerate(pieces):
            if not parts:
                part_start = char_pos
                start_seconds = cue.get("start_seconds")
            candidate_length = sum(len(part) for part in parts) + len(parts) + len(piece)
            if parts and candidate_length > SEGMENT_TARGET_CHARS:
                segments.append(
                    _RawSegment(
                        text=" ".join(parts).strip(),
                        char_range=(part_start, char_pos),
                        start_seconds=start_seconds,
                        end_seconds=end_seconds,
                        chapter_titles=tuple(dict.fromkeys(chapter_names)),
                    )
                )
                parts = []
                chapter_names = []
                start_seconds = cue.get("start_seconds")
                end_seconds = None
                part_start = char_pos

            has_previous = bool(parts)
            parts.append(piece)
            if piece_index == len(pieces) - 1:
                end_seconds = cue.get("end_seconds")
            chapter = _chapter_for_time(chapters, cue.get("start_seconds"))
            if chapter:
                chapter_names.append(chapter)
            char_pos += len(piece) + (1 if has_previous else 0)

    if parts:
        segments.append(
            _RawSegment(
                text=" ".join(parts).strip(),
                char_range=(part_start, char_pos),
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                chapter_titles=tuple(dict.fromkeys(chapter_names)),
            )
        )
    return _merge_short_segments(segments)


def _from_plain_text(text: str) -> list[_RawSegment]:
    sentences = _split_sentences(text)
    segments: list[_RawSegment] = []
    current: list[str] = []
    start = 0
    position = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        for piece in _split_text_piece(sentence, SEGMENT_MAX_CHARS):
            if current and len(" ".join(current)) + len(piece) + 1 > SEGMENT_TARGET_CHARS:
                content = " ".join(current).strip()
                segments.append(_RawSegment(content, (start, position), None, None, ()))
                current = []
                start = position
            has_previous = bool(current)
            current.append(piece)
            position += len(piece) + (1 if has_previous else 0)
    if current:
        content = " ".join(current).strip()
        segments.append(_RawSegment(content, (start, min(len(text), position)), None, None, ()))
    if not segments:
        segments = [_RawSegment(text, (0, len(text)), None, None, ())]
    return _merge_short_segments(segments)


def _merge_short_segments(segments: list[_RawSegment]) -> list[_RawSegment]:
    if len(segments) <= 1:
        return segments
    merged: list[_RawSegment] = []
    for segment in segments:
        combined_length = len(merged[-1].text) + 1 + len(segment.text) if merged else 0
        if merged and len(segment.text) < SEGMENT_MIN_CHARS and combined_length <= SEGMENT_MAX_CHARS:
            previous = merged.pop()
            merged.append(
                _RawSegment(
                    text=f"{previous.text} {segment.text}".strip(),
                    char_range=(previous.char_range[0], segment.char_range[1]),
                    start_seconds=previous.start_seconds,
                    end_seconds=segment.end_seconds,
                    chapter_titles=tuple(dict.fromkeys(previous.chapter_titles + segment.chapter_titles)),
                )
            )
        else:
            merged.append(segment)
    return merged


def _split_text_piece(text: str, max_chars: int) -> list[str]:
    """Split long unpunctuated text without ever exceeding the hard limit."""
    remaining = text.strip()
    pieces: list[str] = []
    while len(remaining) > max_chars:
        cut = remaining.rfind(" ", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def _normalize_cues(cues: list[dict] | list[object]) -> list[dict]:
    normalized = []
    for cue in cues:
        if isinstance(cue, dict):
            text = str(cue.get("text") or "").strip()
            start = _number(cue.get("start_seconds"))
            end = _number(cue.get("end_seconds"))
        else:
            text = str(getattr(cue, "text", "") or "").strip()
            start = _number(getattr(cue, "start_seconds", None))
            end = _number(getattr(cue, "end_seconds", None))
        if text:
            normalized.append({"text": text, "start_seconds": start, "end_seconds": end})
    return normalized


def _chapter_for_time(chapters: list[dict], seconds: float | None) -> str:
    if seconds is None:
        return ""
    current = ""
    for chapter in chapters:
        start = _number(chapter.get("start_seconds"))
        if start is not None and start <= seconds:
            current = str(chapter.get("title") or "").strip()
        else:
            break
    return current


def _split_sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?。！？])\s+|\n{2,}", text) if part.strip()]


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
