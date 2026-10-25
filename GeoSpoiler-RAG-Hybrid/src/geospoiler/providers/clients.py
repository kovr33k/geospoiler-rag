"""OpenAI-compatible client helpers for LightRAG queries and embeddings."""

import asyncio
import logging
from typing import Any

import numpy as np
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

from geospoiler import config
from geospoiler.providers.llm_auth import get_openai_api_key
from geospoiler.providers.runtime import LLM_ROLE as _LLM_ROLE

logger = logging.getLogger("geospoiler.loader")


def _client_api_key(api_key: str, base_url: str) -> str:
    return get_openai_api_key(api_key, base_url) or "geospoiler-missing-api-key"


def _chat_settings_for_role(role: str) -> tuple[str, str, str]:
    if role == "build":
        return config.RAG_BUILD_API_KEY, config.RAG_BUILD_BASE_URL, config.RAG_BUILD_MODEL
    if role == "fallback":
        return config.FALLBACK_SYNTH_API_KEY, config.FALLBACK_SYNTH_BASE_URL, config.FALLBACK_SYNTH_MODEL
    return config.QUERY_API_KEY, config.QUERY_BASE_URL, config.QUERY_MODEL


def _openai_client(api_key: str, base_url: str, **kwargs) -> AsyncOpenAI:
    # LightRAG already owns the retry/timeout boundary for build and query
    # calls.  Letting the OpenAI SDK retry internally can keep a cancelled
    # LightRAG task alive for hours when the provider connection is unhealthy.
    # The caller-level retry policy remains explicit and bounded.
    kwargs.setdefault("max_retries", 0)
    return AsyncOpenAI(
        api_key=_client_api_key(api_key, base_url),
        base_url=base_url,
        **kwargs,
    )


def _uses_deepseek_v4() -> bool:
    values = (
        config.LLM_BASE_URL,
        config.LLM_MODEL,
        config.RAG_BUILD_BASE_URL,
        config.RAG_BUILD_MODEL,
        config.QUERY_BASE_URL,
        config.QUERY_MODEL,
        config.FALLBACK_SYNTH_BASE_URL,
        config.FALLBACK_SYNTH_MODEL,
    )
    text = " ".join(str(value).casefold() for value in values)
    return "api.deepseek.com" in text or "deepseek-v4" in text


def _chat_completion_options(max_tokens: int | None = None, **kwargs) -> dict[str, Any]:
    """Build OpenAI-compatible chat options from explicit args plus local config."""
    options = {key: value for key, value in kwargs.items() if value is not None}
    if max_tokens is not None and max_tokens > 0:
        options["max_tokens"] = max_tokens
    if config.LLM_REASONING_EFFORT:
        options["reasoning_effort"] = config.LLM_REASONING_EFFORT
    if _uses_deepseek_v4() and not config.LLM_REASONING_EFFORT:
        extra_body = dict(options.get("extra_body") or {})
        extra_body.setdefault("thinking", {"type": "disabled"})
        options["extra_body"] = extra_body
    return options


_embed_client_cache: tuple[tuple[str, str, int], AsyncOpenAI] | None = None
_embed_semaphore_cache: tuple[int, asyncio.Semaphore] | None = None

_NIM_ASYMMETRIC_MODELS = {
    "nvidia/llama-nemotron-embed-vl-1b-v2",
    "nvidia/nv-embedqa-e5-v5",
    "nvidia/nv-embedqa-mistral-7b-v2",
    "nvidia/llama-3.2-nv-embedqa-1b-v2",
}


def _needs_embedding_input_type() -> bool:
    model = config.EMBEDDING_MODEL.casefold()
    base_url = config.EMBEDDING_BASE_URL.casefold()
    if model in _NIM_ASYMMETRIC_MODELS:
        return True
    return "api.nvidia.com" in base_url and "embed" in model


def _default_embedding_input_type() -> str:
    return "passage" if _LLM_ROLE.get() == "build" else "query"


def _embedding_dimensions_override() -> int | None:
    if "api.nvidia.com" in config.EMBEDDING_BASE_URL.casefold() and config.EMBEDDING_DIM > 0:
        return config.EMBEDDING_DIM
    return None


def _get_embed_client() -> AsyncOpenAI:
    global _embed_client_cache
    cache_key = (
        config.EMBEDDING_API_KEY,
        config.EMBEDDING_BASE_URL,
        config.EMBEDDING_TIMEOUT_SECONDS,
    )
    if _embed_client_cache is None or _embed_client_cache[0] != cache_key:
        _embed_client_cache = (
            cache_key,
            AsyncOpenAI(
                api_key=_client_api_key(config.EMBEDDING_API_KEY, config.EMBEDDING_BASE_URL),
                base_url=config.EMBEDDING_BASE_URL,
                timeout=config.EMBEDDING_TIMEOUT_SECONDS,
                max_retries=0,
            ),
        )
    return _embed_client_cache[1]


def _get_embed_semaphore() -> asyncio.Semaphore:
    global _embed_semaphore_cache
    concurrency = max(1, config.EMBEDDING_CONCURRENCY)
    if _embed_semaphore_cache is None or _embed_semaphore_cache[0] != concurrency:
        _embed_semaphore_cache = (concurrency, asyncio.Semaphore(concurrency))
    return _embed_semaphore_cache[1]


async def _embed_texts(texts: list[str], input_type: str | None = None) -> np.ndarray:
    """Call NIM embedding API. Returns numpy array as required by LightRAG."""
    if not texts:
        return np.empty((0, config.EMBEDDING_DIM), dtype=np.float32)

    batch_size = max(1, config.EMBEDDING_BATCH_SIZE)

    async def embed_batch(start: int, batch: list[str]) -> list[list[float]]:
        kwargs: dict = {
            "model": config.EMBEDDING_MODEL,
            "input": batch,
        }
        if _needs_embedding_input_type():
            embedding_input_type = input_type or _default_embedding_input_type()
            kwargs["extra_body"] = {"input_type": embedding_input_type, "truncate": "END"}
        dimensions = _embedding_dimensions_override()
        if dimensions is not None:
            kwargs["dimensions"] = dimensions

        for attempt in range(1, max(1, config.EMBEDDING_MAX_ATTEMPTS) + 1):
            try:
                async with _get_embed_semaphore():
                    response = await asyncio.wait_for(
                        _get_embed_client().embeddings.create(**kwargs),
                        timeout=config.EMBEDDING_TIMEOUT_SECONDS,
                    )
                return [item.embedding for item in response.data]
            except (APIConnectionError, APITimeoutError, TimeoutError) as exc:
                if attempt >= config.EMBEDDING_MAX_ATTEMPTS:
                    raise
                delay = min(12, 1.5 * attempt)
                logger.warning(
                    "Embedding request failed for batch %s-%s/%s on attempt %s/%s: %s. Retrying in %.1fs.",
                    start + 1,
                    start + len(batch),
                    len(texts),
                    attempt,
                    config.EMBEDDING_MAX_ATTEMPTS,
                    exc.__class__.__name__,
                    delay,
                )
                await asyncio.sleep(delay)

        raise RuntimeError(f"Embedding batch {start} did not complete")

    batches = [
        (start, texts[start : start + batch_size])
        for start in range(0, len(texts), batch_size)
    ]
    results = await asyncio.gather(
        *(embed_batch(start, batch) for start, batch in batches)
    )
    all_vectors = [vector for batch_vectors in results for vector in batch_vectors]
    return np.array(all_vectors, dtype=np.float32)
