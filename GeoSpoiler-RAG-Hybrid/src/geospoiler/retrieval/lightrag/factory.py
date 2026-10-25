"""LightRAG factory wired to project LLM, embedding, extraction, and reranking settings."""

import asyncio

from lightrag import LightRAG
from lightrag.utils import EmbeddingFunc

from geospoiler import config
from geospoiler.providers import llm_backend
from geospoiler.providers.clients import _chat_completion_options, _chat_settings_for_role, _embed_texts, _openai_client
from geospoiler.providers.reranker import lightrag_rerank_func
from geospoiler.providers.runtime import LLM_ROLE as _LLM_ROLE
from geospoiler.providers.runtime import logger
from geospoiler.retrieval.lightrag.extraction import (
    _build_extraction_policy,
    _configure_lightrag_prompts,
    _is_extraction_prompt,
    _postprocess_extraction_response,
)


async def create_rag() -> LightRAG:
    """Initialize a LightRAG instance with configured LLM and Embedding."""
    _configure_lightrag_prompts()

    async def llm_func(prompt, system_prompt=None, history_messages=None, **kwargs):
        is_extraction = _is_extraction_prompt(prompt, system_prompt)
        role = _LLM_ROLE.get()
        chat_role = "build" if role == "build" else "query"
        backend_role = "rag_build" if chat_role == "build" else "query"
        messages = []
        if system_prompt:
            system_content = system_prompt
            if is_extraction:
                system_content = f"{system_prompt}\n\n---Project-Specific Rules---\n{_build_extraction_policy()}"
            messages.append({"role": "system", "content": system_content})
        for m in (history_messages or []):
            messages.append(m)
        messages.append({"role": "user", "content": prompt})

        delay_seconds = (
            config.RAG_BUILD_DELAY_SECONDS if chat_role == "build" else config.QUERY_DELAY_SECONDS
        )
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        if llm_backend.is_luna_role(backend_role):
            try:
                content = await llm_backend.complete_text_async(
                    messages,
                    role=backend_role,
                    timeout_seconds=config.CODEX_LLM_TIMEOUT_SECONDS,
                )
            except llm_backend.LLMBackendError:
                if not config.CODEX_FALLBACK_TO_API:
                    raise
                logger.warning("Codex %s call failed; explicit API fallback is enabled", backend_role)
                api_key, base_url, model = _chat_settings_for_role(chat_role)
                llm_client = _openai_client(
                    api_key,
                    base_url,
                    timeout=config.LLM_TIMEOUT_SECONDS,
                )
                response = await asyncio.wait_for(
                    llm_client.chat.completions.create(
                        model=model,
                        messages=messages,
                        **_chat_completion_options(
                            max_tokens=kwargs.get("max_tokens") or config.QUERY_MAX_TOKENS,
                            temperature=kwargs.get("temperature"),
                            top_p=kwargs.get("top_p"),
                        ),
                    ),
                    timeout=config.LLM_TIMEOUT_SECONDS,
                )
                content = response.choices[0].message.content or ""
        else:
            api_key, base_url, model = _chat_settings_for_role(chat_role)
            llm_client = _openai_client(
                api_key,
                base_url,
                timeout=config.LLM_TIMEOUT_SECONDS,
            )
            response = await asyncio.wait_for(
                llm_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **_chat_completion_options(
                        max_tokens=kwargs.get("max_tokens") or config.QUERY_MAX_TOKENS,
                        temperature=kwargs.get("temperature"),
                        top_p=kwargs.get("top_p"),
                    ),
                ),
                timeout=config.LLM_TIMEOUT_SECONDS,
            )
            content = response.choices[0].message.content or ""
        if is_extraction:
            content = _postprocess_extraction_response(content)
        return content

    # ── Embedding function ──
    # Using our custom _embed_texts which adds input_type for NIM asymmetric models
    embedding_func = EmbeddingFunc(
        embedding_dim=config.EMBEDDING_DIM,
        max_token_size=8192,
        func=_embed_texts,
    )

    # ── Create LightRAG ──
    # rerank_model_func: LightRAG calls this BEFORE sending chunks to LLM.
    # Signature: async (query, documents, top_n) -> [{"index": int, "relevance_score": float}]
    rag = LightRAG(
        working_dir=str(config.RAG_STORAGE_DIR),
        llm_model_func=llm_func,
        llm_model_max_async=config.LLM_MAX_ASYNC,
        embedding_func=embedding_func,
        # Keep LightRAG's outer embedding queue aligned with our provider
        # contract.  Its defaults are eight concurrent calls and a 60-second
        # worker deadline, which can overwhelm OpenRouter even though the
        # project client already serializes requests and retries explicitly.
        embedding_func_max_async=max(1, config.EMBEDDING_CONCURRENCY),
        default_embedding_timeout=max(1, int(config.EMBEDDING_TIMEOUT_SECONDS)),
        embedding_batch_num=max(1, config.EMBEDDING_BATCH_SIZE),
        rerank_model_func=lightrag_rerank_func if config.RERANKER_ENABLED else None,
        addon_params={
            "language": config.LIGHTRAG_LANGUAGE,
            "entity_types": config.LIGHTRAG_ENTITY_TYPES,
        },
    )

    await rag.initialize_storages()
    logger.info("LightRAG initialized.")
    return rag
