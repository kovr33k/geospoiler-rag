"""Reranker integration for LightRAG and the standalone query helper.

The production route uses OpenRouter's ``POST /api/v1/rerank`` contract.

LightRAG API (rerank_model_func):
  async def func(query: str, documents: list[str], top_n: int)
    -> list[{"index": int, "relevance_score": float}]

Если RERANKER_ENABLED=false — функция возвращает None, LightRAG rerank не будет подключён.
"""

import asyncio
import logging

import httpx

from geospoiler import config

logger = logging.getLogger("geospoiler.reranker")

_RERANKER_STATS: dict[str, object] = {
    "attempted": 0,
    "succeeded": 0,
    "failed": 0,
    "last_error_type": None,
}


def reset_reranker_stats() -> None:
    """Reset process-local counters before an observable query/run."""
    _RERANKER_STATS.update(attempted=0, succeeded=0, failed=0, last_error_type=None)


def get_reranker_stats() -> dict[str, object]:
    """Return a copy safe to persist in local evaluation artifacts."""
    return {
        **_RERANKER_STATS,
        "provider": config.RERANKER_PROVIDER,
        "model": config.RERANKER_MODEL,
    }


# ──────────────────────────────────────────────────────────────────
# LightRAG-совместимая async rerank функция
# ──────────────────────────────────────────────────────────────────

async def lightrag_rerank_func(
    query: str,
    documents: list[str],
    top_n: int | None = None,
) -> list[dict]:
    """
    Async reranker совместимый с LightRAG 1.4.15 rerank_model_func API.

    Args:
        query:     Поисковый запрос пользователя.
        documents: Список текстов для ранжирования (chunk content).
        top_n:     Сколько вернуть (если None — используется config.RERANKER_TOP_N).

    Returns:
        list of {"index": int, "relevance_score": float} — отсортированный по score desc.
    """
    if not config.RERANKER_ENABLED or not documents:
        # Вернуть все документы с убывающим score (без реального rerank)
        return [{"index": i, "relevance_score": 1.0 - i * 0.01} for i in range(len(documents))]

    top_n = top_n or config.RERANKER_TOP_N
    candidates = documents[: config.RERANKER_CANDIDATE_POOL]

    try:
        provider = config.RERANKER_PROVIDER.lower()

        _RERANKER_STATS["attempted"] = int(_RERANKER_STATS["attempted"]) + 1

        if provider == "openrouter":
            results = await _rerank_openrouter_async(query, candidates, top_n)
        elif provider == "jina":
            results = await _rerank_jina_async(query, candidates, top_n)
        else:
            raise ValueError(f"Unknown reranker provider: {provider}")

        if not results:
            raise ValueError("Reranker returned no results")

        _RERANKER_STATS["succeeded"] = int(_RERANKER_STATS["succeeded"]) + 1

        logger.info(
            f"Reranker ({provider}): {len(documents)} docs -> "
            f"{len(candidates)} candidates -> {len(results)} returned"
        )
        return results

    except Exception as e:
        _RERANKER_STATS["failed"] = int(_RERANKER_STATS["failed"]) + 1
        _RERANKER_STATS["last_error_type"] = type(e).__name__
        logger.warning(f"Reranker failed ({e}), using original order.")
        return [{"index": i, "relevance_score": 1.0} for i in range(min(top_n or 10, len(documents)))]


# ──────────────────────────────────────────────────────────────────
# OpenRouter async
# ──────────────────────────────────────────────────────────────────

async def _rerank_openrouter_async(
    query: str,
    documents: list[str],
    top_n: int,
) -> list[dict]:
    """Call OpenRouter's provider-routed rerank endpoint."""
    url = config.RERANKER_BASE_URL.rstrip("/") + "/rerank"

    payload = {
        "model": config.RERANKER_MODEL,
        "query": query,
        "documents": documents,
        "top_n": top_n,
    }

    headers = {
        "Authorization": f"Bearer {config.RERANKER_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    return _validated_results(data.get("results"), document_count=len(documents), top_n=top_n)


def _validated_results(results: object, *, document_count: int, top_n: int) -> list[dict]:
    if not isinstance(results, list):
        raise ValueError("Reranker response is missing results")
    validated: list[dict] = []
    seen: set[int] = set()
    for item in results[:top_n]:
        if not isinstance(item, dict):
            raise ValueError("Reranker result must be an object")
        index = item.get("index")
        score = item.get("relevance_score")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < document_count:
            raise ValueError("Reranker result index is invalid")
        if index in seen:
            raise ValueError("Reranker result index is duplicated")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise ValueError("Reranker relevance score is invalid")
        seen.add(index)
        validated.append({"index": index, "relevance_score": float(score)})
    return validated


# ──────────────────────────────────────────────────────────────────
# Jina AI async
# ──────────────────────────────────────────────────────────────────

async def _rerank_jina_async(
    query: str,
    passages: list[str],
    top_n: int,
) -> list[dict]:
    """
    Jina AI Reranker API (async).
    Endpoint: POST https://api.jina.ai/v1/rerank
    """
    url = "https://api.jina.ai/v1/rerank"

    payload = {
        "model": config.RERANKER_MODEL,
        "query": query,
        "documents": passages,
        "top_n": top_n,
    }

    headers = {
        "Authorization": f"Bearer {config.RERANKER_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    # Jina returns: {"results": [{"index": 0, "relevance_score": 0.95, ...}, ...]}
    return _validated_results(data.get("results"), document_count=len(passages), top_n=top_n)


# ──────────────────────────────────────────────────────────────────
# Синхронная обёртка (для обратной совместимости / тестов)
# ──────────────────────────────────────────────────────────────────

def rerank(query: str, passages: list[str]) -> list[str]:
    """
    Синхронный вызов reranker (совместимость со старым кодом).
    Возвращает список текстов (а не индексов), обрезанный до RERANKER_TOP_N.
    """
    if not config.RERANKER_ENABLED or not passages:
        return passages

    try:
        results = asyncio.run(
            lightrag_rerank_func(query, passages, config.RERANKER_TOP_N)
        )
        return [passages[r["index"]] for r in results if r["index"] < len(passages)]
    except Exception as e:
        logger.warning(f"Sync rerank wrapper failed ({e}), using original order.")
        return passages[: config.RERANKER_TOP_N]
