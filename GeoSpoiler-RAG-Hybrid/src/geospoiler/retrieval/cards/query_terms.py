"""Shared lexical query expansion for local retrieval."""

from __future__ import annotations

_SIMILARITY_STEMS = (
    "сход",
    "совпад",
    "одинак",
    "похож",
    "similar",
    "same",
    "coincid",
    "overlap",
)

_SIMILARITY_EXPANSIONS = (
    "сход",
    "сходств",
    "совпад",
    "одинак",
    "похож",
    "similar",
    "same",
    "coincid",
    "overlap",
)


def expand_query_terms(
    terms: list[str],
    *,
    max_terms: int = 64,
) -> list[str]:
    """Expand lexical intent deterministically."""
    expanded = list(dict.fromkeys(term for term in terms if term))
    if any(_has_stem(term, _SIMILARITY_STEMS) for term in expanded):
        expanded.extend(term for term in _SIMILARITY_EXPANSIONS if term not in expanded)
    if "afd" in expanded and "адг" not in expanded:
        expanded.append("адг")
    if "адг" in expanded and "afd" not in expanded:
        expanded.append("afd")
    return expanded[:max_terms]


def add_compound_terms(terms: list[str]) -> list[str]:
    """Add joined forms for common split compounds such as ultra-left/right."""
    expanded = list(terms)
    for first, second in zip(terms, terms[1:], strict=False):
        if first == "ультра" and second.startswith("лев"):
            expanded.append(f"ультра{second}")
        elif first == "ультра" and second.startswith("прав"):
            expanded.append(f"ультра{second}")
    return list(dict.fromkeys(expanded))


def matches_term(token: str, term: str) -> bool:
    """Match exact words plus conservative inflection variants by prefix."""
    if token == term:
        return True

    token_family = _ultra_family(token)
    term_family = _ultra_family(term)
    if token_family or term_family:
        if token_family != term_family:
            return False

    if len(token) < 4 or len(term) < 4:
        return False
    if min(len(token), len(term)) <= 4 and max(len(token), len(term)) > 5:
        return False
    prefix_len = min(len(token), len(term), 6)
    if token[:prefix_len] == term[:prefix_len]:
        return True
    if min(len(token), len(term)) <= 5:
        return token[:4] == term[:4]
    return False


def _has_stem(term: str, stems: tuple[str, ...]) -> bool:
    normalized = term.casefold().strip()
    return any(normalized.startswith(stem) for stem in stems)


def _ultra_family(value: str) -> str:
    if value.startswith("ультралев"):
        return "left"
    if value.startswith("ультраправ"):
        return "right"
    if value == "ультра":
        return "generic"
    return ""
