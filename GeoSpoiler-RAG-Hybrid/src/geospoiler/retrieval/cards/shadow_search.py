"""
Shadow Search — a fast keyword fallback search over memory cards.
Returns (file_path, score, snippet) for exact/partial term matches.
"""

import json
import logging
import re
from dataclasses import dataclass

from geospoiler import config
from geospoiler.retrieval.cards.card_text import card_search_text
from geospoiler.retrieval.cards.query_terms import add_compound_terms, expand_query_terms, matches_term

logger = logging.getLogger("geospoiler.retrieval.shadow")

_STOPWORDS = {
    "что",
    "как",
    "какой",
    "где",
    "кто",
    "это",
    "или",
    "для",
    "про",
    "при",
    "под",
    "над",
    "без",
    "базе",
    "говорится",
    "главный",
    "тезис",
    "тезисы",
    "автор",
    "продвигает",
}


@dataclass
class ShadowMatch:
    source_path: str
    card_path: str | None
    score: float
    snippet: str
    title: str


def _tokenize(text: str) -> list[str]:
    """Extract lowercase words >= 3 chars."""
    words = re.findall(r"\w{3,}", text.lower())
    return add_compound_terms([word for word in words if word not in _STOPWORDS])


def query_terms(text: str) -> list[str]:
    """Tokenize and expand query intent terms for local lexical retrieval."""
    return expand_query_terms(_tokenize(text))


def _matches_term(token: str, term: str) -> bool:
    """Match exact words plus simple Slavic inflection variants by prefix."""
    return matches_term(token, term)


def _extract_snippet(text: str, query_terms: list[str], context_chars: int = 100) -> str:
    """Find the best window of text containing query terms."""
    text_lower = text.casefold()

    for term in sorted(query_terms, key=lambda value: (-len(value.split()), -len(value), value)):
        pos = text_lower.find(term.casefold())
        if pos != -1:
            # We found a match. To be simple, just return the first good window.
            start = max(0, pos - context_chars)
            end = min(len(text), pos + len(term) + context_chars)
            return "..." + text[start:end].replace("\n", " ").strip() + "..."

    return text[:context_chars*2].replace("\n", " ").strip() + "..."


def _count_term_matches(text: str, text_tokens: list[str], term: str) -> int:
    phrase_tokens = re.findall(r"\w{2,}", term.casefold(), re.UNICODE)
    if not phrase_tokens:
        return 0
    if len(phrase_tokens) == 1:
        return sum(1 for token in text_tokens if _matches_term(token, phrase_tokens[0]))

    all_tokens = re.findall(r"\w{2,}", text.casefold(), re.UNICODE)
    width = len(phrase_tokens)
    matches = 0
    for start in range(len(all_tokens) - width + 1):
        window = all_tokens[start : start + width]
        if all(
            _matches_term(token, expected)
            for token, expected in zip(window, phrase_tokens, strict=True)
        ):
            matches += 1
    return matches * width


def search(query: str, top_k: int | None = 10) -> list[ShadowMatch]:
    """
    Perform a keyword search over enriched_v2 cards.
    """
    terms = query_terms(query)
    if not terms:
        return []

    enriched_dir = config.ENRICHED_DIR
    matches = []

    # 1. Search in enriched cards
    if enriched_dir.exists():
        for channel_dir in sorted(enriched_dir.iterdir(), key=lambda path: path.as_posix()):
            if not channel_dir.is_dir():
                continue
            for card_path in sorted(channel_dir.glob("*.enriched.json"), key=lambda path: path.as_posix()):
                try:
                    card = json.loads(card_path.read_text(encoding="utf-8"))
                    if not isinstance(card, dict) or card.get("schema_version") != "enriched_v2" or card.get("duplicate_of"):
                        continue

                    search_text = card_search_text(card, card_path)

                    text_tokens = _tokenize(search_text)

                    score = 0.0
                    for term in terms:
                        score += _count_term_matches(search_text, text_tokens, term)

                    if score > 0:
                        prov = card.get("provenance", {})
                        source_path = prov.get("normalized_path") or ""
                        title = f"{prov.get('channel') or '?'} - {prov.get('date', '?')[:10]}"

                        snippet = _extract_snippet(search_text, terms)

                        matches.append(
                            ShadowMatch(
                                source_path=source_path,
                                card_path=str(card_path),
                                score=score,
                                snippet=snippet,
                                title=title,
                            )
                        )
                except Exception as e:
                    logger.debug(f"Shadow search failed to read {card_path}: {e}")

    # 2. (Optional) Search in normalized if we don't have enough enriched yet?
    # For now, relying on enriched is better since we will backfill anyway.

    matches.sort(
        key=lambda match: (
            -match.score,
            match.source_path.casefold(),
            match.card_path.casefold(),
        )
    )
    return matches if top_k is None else matches[: max(1, top_k)]
