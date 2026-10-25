"""Helpers for selecting clean China-related Telegram posts for bakeoff suites."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

_URL_RE = re.compile(r"https?://|(?:www\.)|t\.me/|youtube\.com|youtu\.be|instagram\.com", re.IGNORECASE)
_MEDIA_MARKER_RE = re.compile(
    r"\[(?:видео|аудио|изображение|фото|video|audio|image|photo|youtube|instagram)[^\]]*\]",
    re.IGNORECASE,
)

_TOPIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Taiwan", ("тайван", "taiwan", "roc", "кнр", "пекин", "beijing")),
    ("CCP", ("кпк", "ccp", "компарти", "партии", "си цзиньпин", "xi jinping")),
    ("Xinjiang", ("синьцзян", "уйгур", "xinjiang", "uyghur", "forced labor", "лагер")),
    ("Hong Kong", ("гонконг", "hong kong", "national security law", "протест")),
    ("Tibet", ("тибет", "tibet", "далай")),
    ("Censorship", ("цензур", "great firewall", "фаервол", "surveillance")),
    ("Ukraine/Russia", ("украин", "росси", "крым", "вторж", "war crimes", "bucha")),
    ("US-China", ("сша", "usa", "america", "trade war", "санкц")),
    ("South China Sea", ("южно-китай", "south china sea", "филиппин", "тайваньский пролив")),
)


@dataclass(frozen=True)
class CandidatePost:
    """A selected Telegram post candidate for real-corpus bakeoff cases."""

    text: str
    post_url: str
    score: int
    sensitive_topics: tuple[str, ...]
    channel_name: str = ""
    message_id: int | None = None
    date: str = ""
    reason: str = ""


def candidate_from_text(
    text: str,
    *,
    post_url: str,
    channel_name: str = "",
    message_id: int | None = None,
    date: str = "",
    min_chars: int = 80,
    max_chars: int = 2500,
) -> CandidatePost | None:
    """Return a candidate for clean, short, sensitive China posts."""
    clean = _normalize_whitespace(text)
    if len(clean) < min_chars or len(clean) > max_chars:
        return None
    if _URL_RE.search(clean) or _MEDIA_MARKER_RE.search(clean):
        return None

    topics = _detect_topics(clean)
    if not topics:
        return None

    score = _score_text(clean, topics)
    return CandidatePost(
        text=clean,
        post_url=post_url,
        score=score,
        sensitive_topics=tuple(topics),
        channel_name=channel_name,
        message_id=message_id,
        date=date,
        reason=_reason_for(topics),
    )


def write_candidate_artifacts(
    candidates: list[CandidatePost],
    output_dir: Path,
    *,
    prefix: str | None = None,
) -> tuple[Path, Path]:
    """Write selected post candidates as JSONL and Markdown."""
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = prefix or f"china_candidate_posts_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    jsonl_path = output_dir / f"{prefix}.jsonl"
    md_path = output_dir / f"{prefix}.md"

    with jsonl_path.open("w", encoding="utf-8") as f:
        for candidate in candidates:
            f.write(json.dumps(asdict(candidate), ensure_ascii=False, sort_keys=True) + "\n")
    md_path.write_text(_format_markdown(candidates), encoding="utf-8")
    return jsonl_path, md_path


def _format_markdown(candidates: list[CandidatePost]) -> str:
    lines = [
        "# China Candidate Posts",
        "",
        f"- candidates: {len(candidates)}",
        "",
    ]
    for index, candidate in enumerate(candidates, start=1):
        topics = ", ".join(candidate.sensitive_topics)
        preview = candidate.text[:500].strip()
        if len(candidate.text) > 500:
            preview += "..."
        lines.extend(
            [
                f"## {index}. {candidate.channel_name or 'unknown'} / {candidate.message_id or '?'}",
                "",
                f"- url: {candidate.post_url}",
                f"- date: {candidate.date or '?'}",
                f"- score: {candidate.score}",
                f"- topics: {topics}",
                f"- reason: {candidate.reason}",
                "",
                preview,
                "",
            ]
        )
    return "\n".join(lines)


def _detect_topics(text: str) -> list[str]:
    lowered = text.casefold()
    topics: list[str] = []
    for topic, keywords in _TOPIC_KEYWORDS:
        if any(keyword.casefold() in lowered for keyword in keywords):
            topics.append(topic)
    return topics


def _score_text(text: str, topics: list[str]) -> int:
    lowered = text.casefold()
    score = len(topics) * 2
    if any(marker in lowered for marker in ("заяв", "утверж", "сообщ", "claims", "alleges", "denies")):
        score += 2
    if any(marker in lowered for marker in ("давлен", "принужд", "цензур", "санкц", "вторж", "лагер")):
        score += 2
    if 400 <= len(text) <= 1800:
        score += 1
    return score


def _reason_for(topics: list[str]) -> str:
    return "Clean text post with sensitive/source-preservation topics: " + ", ".join(topics)


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()
