"""Query profile prompt settings."""

from typing import Any

_QUERY_USER_PROMPT = (
    "Use only the context that directly answers the specific question asked. "
    "STRICTLY ignore tangential references, background information, or adjacent topics even if they share entities, countries, or people with the query. "
    "Do not broaden a narrow question into a general ideological or geopolitical essay, but if the context contains indirect or circumstantial evidence, answer with clear separation between direct evidence and broader context. "
    "For questions asking who funds or finances an actor, answer only if the context directly names a funder; do not infer financing from influence, sympathy, corruption, travel, leaks, or ideological alignment. "
    "If a funding question has no directly named funder, say in Russian: 'В базе отсутствует прямое указание; по имеющимся данным это нельзя определить.' "
    "Use careful attribution language: if the context says something is suspected, alleged, reported, or claimed, keep that qualification. "
    "Do not turn allegations, suspicions, or interpretations into established facts. "
    "If the answer is only indirectly supported, explicitly say so instead of claiming there is no information. "
    "If the provided context truly does not support an answer, clearly state that the base does not contain the answer. "
    "ВСЕГДА ОТВЕЧАЙ НА РУССКОМ ЯЗЫКЕ."
)
_SOURCE_QUERY_USER_PROMPT = (
    "The user is asking for provenance. Prioritize concrete source attribution over synthesis. "
    "Restate the claim using the user's wording before giving links; keep compounds such as 'ультралевые' and 'ультраправые' unhyphenated when the user writes them that way. "
    "Use only retrieved context and references. Name the specific post, file, or document that supports the claim. "
    "Prefer Telegram post URLs or source file references when available in the context. "
    "If the retrieved context does not contain a concrete source for the claim, say that the base contains the claim but the source link was not recovered. "
    "Do not add broad background or adjacent political analysis. "
    "ВСЕГДА ОТВЕЧАЙ НА РУССКОМ ЯЗЫКЕ."
)
_OVERVIEW_QUERY_USER_PROMPT = (
    "Answer as a broad overview, but still use only the provided context. "
    "Group repeated evidence by theme and avoid listing weakly related entities as if they were central. "
    "Clearly separate direct evidence from broader patterns inferred from multiple retrieved posts. "
    "If the context is thin or mixed, state that limitation. "
    "ВСЕГДА ОТВЕЧАЙ НА РУССКОМ ЯЗЫКЕ."
)
_QUERY_RESPONSE_TYPE = "Short factual answer in a few paragraphs"
_DEFAULT_QUERY_TOP_K = 15
_DEFAULT_QUERY_CHUNK_TOP_K = 10
_QUERY_PROFILES: dict[str, dict[str, Any]] = {
    "answer": {
        "top_k": 15,
        "chunk_top_k": _DEFAULT_QUERY_CHUNK_TOP_K,
        "user_prompt": _QUERY_USER_PROMPT,
    },
    "source": {
        "top_k": 15,
        "chunk_top_k": _DEFAULT_QUERY_CHUNK_TOP_K,
        "user_prompt": _SOURCE_QUERY_USER_PROMPT,
    },
    "overview": {
        "top_k": 30,
        "chunk_top_k": _DEFAULT_QUERY_CHUNK_TOP_K,
        "user_prompt": _OVERVIEW_QUERY_USER_PROMPT,
    },
}


def get_query_profile(profile: str | None = None) -> dict[str, Any]:
    """Return retrieval and prompt settings for a named query profile."""
    name = (profile or "answer").strip().lower()
    if name not in _QUERY_PROFILES:
        raise ValueError(f"unknown query profile: {profile}")
    return _QUERY_PROFILES[name].copy()
