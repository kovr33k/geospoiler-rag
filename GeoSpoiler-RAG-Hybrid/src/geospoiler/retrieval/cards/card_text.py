"""Card text helpers shared by local retrieval backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from geospoiler import config

_TEXT_FIELDS = ("search_text", "graph_text", "summary")
_EVIDENCE_LIST_FIELDS = ("key_points", "quotes", "theses", "events")


def card_search_text(card: dict[str, Any], card_path: Path | str | None = None) -> str:
    """Return searchable card text, falling back to normalized text for thin cards."""
    parts: list[str] = []
    for key in _TEXT_FIELDS:
        _append_text(parts, card.get(key))

    if _is_thin_card(card):
        _append_text(parts, _read_normalized_text(card, card_path))

    seen: set[str] = set()
    deduped = []
    for part in parts:
        key = part.strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(key)
    return "\n\n".join(deduped)


def card_ranking_text(card: dict[str, Any], card_path: Path | str | None = None) -> str:
    """Return card text for relevance coverage without source metadata headers."""
    return strip_metadata_lines(card_search_text(card, card_path))


def strip_metadata_lines(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if stripped.startswith("[Канал:") or stripped.startswith("Источник:") or stripped.startswith("Тип контента:"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _is_thin_card(card: dict[str, Any]) -> bool:
    if str(card.get("summary") or "").strip():
        return False
    for field in _EVIDENCE_LIST_FIELDS:
        value = card.get(field)
        if isinstance(value, list) and value:
            return False
    return True


def _read_normalized_text(card: dict[str, Any], card_path: Path | str | None = None) -> str:
    provenance = card.get("provenance") if isinstance(card.get("provenance"), dict) else {}
    normalized_file = str(provenance.get("normalized_path") or "").strip()
    candidates = []
    if normalized_file:
        candidates.append(config.resolve_project_path(normalized_file))
    for path in candidates:
        try:
            if path.exists() and path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


def _append_text(parts: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text:
        parts.append(text)
