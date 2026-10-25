"""LightRAG query orchestration with card context and fallbacks."""

import asyncio
from typing import Any

from lightrag import LightRAG, QueryParam

from geospoiler import config
from geospoiler.answering.answer_postprocess import (
    _answer_looks_corrupt,
    _is_funding_question,
    _postprocess_answer_text,
    _question_requests_visuals,
    _response_has_no_context,
    _response_looks_corrupt,
)
from geospoiler.answering.card_context import (
    _CLAIM_TYPE_TAG_PROMPT,
    _USER_ENTITY_WORDING_PROMPT,
    _attach_card_context,
    _card_context_for_query,
    _card_references_should_be_first,
    _format_shadow_context,
    _shadow_fallback_result,
    _synthesize_shadow_fallback_result,
)
from geospoiler.answering.profiles import _QUERY_RESPONSE_TYPE, get_query_profile
from geospoiler.answering.reference_hints import _attach_reference_hints
from geospoiler.providers import llm_backend
from geospoiler.providers.clients import _chat_completion_options, _openai_client
from geospoiler.providers.runtime import LLM_ROLE as _LLM_ROLE
from geospoiler.providers.runtime import logger


async def _try_shadow_fallback_result(question: str, query_profile: str | None) -> dict[str, Any] | None:
    """Return synthesized shadow fallback unless the question must not use fallback inference."""
    if _is_funding_question(question):
        return None
    fallback = _shadow_fallback_result(question, query_profile)
    if not fallback:
        return None
    return await _synthesize_shadow_fallback_result(question, query_profile, fallback)


async def query_rag(
    rag: LightRAG,
    question: str,
    mode: str | None = None,
    query_profile: str | None = None,
) -> str:
    """
    Query the LightRAG knowledge graph.

    Reranking happens INSIDE LightRAG at the chunk retrieval stage (before LLM),
    via rerank_model_func passed to the LightRAG constructor.

    Args:
        rag:      Initialized LightRAG instance
        question: The question to ask
        mode:     Query mode — "local", "global", "hybrid", "naive", "mix".
                  Defaults to "mix" when reranker is enabled, otherwise "hybrid".
        query_profile: Answer behavior profile — "answer", "source", or "overview".

    Returns:
        The answer from LightRAG
    """
    result = await query_rag_result(
        rag,
        question,
        mode=mode,
        query_profile=query_profile,
    )
    return str(result.get("llm_response", {}).get("content") or result.get("response") or "")

async def _synthesize_hybrid_result(
    question: str,
    query_profile: str | None,
    result: dict[str, Any],
    card_context: dict[str, Any],
) -> dict[str, Any]:
    """Compose LightRAG answer with enriched-card facts into one user-facing answer."""
    prefer_card_references = _card_references_should_be_first(question, query_profile)
    if not config.HYBRID_SYNTH_ENABLED or (
        not config.FALLBACK_SYNTH_API_KEY and not llm_backend.is_luna_role("fallback_synth")
    ):
        return _attach_card_context(result, card_context, prefer_card_references=prefer_card_references)

    graph_answer = str(result.get("llm_response", {}).get("content") or result.get("response") or "").strip()
    context = _format_shadow_context(list(card_context.get("shadow_context") or []))
    if not graph_answer or not context.strip():
        return _attach_card_context(result, card_context, prefer_card_references=prefer_card_references)

    include_visual = _question_requests_visuals(question)
    profile = query_profile or "answer"
    system = (
        "Ты пишешь финальный ответ RAG-системы на русском языке. "
        "У тебя есть черновой ответ графа и дополнительные карточки источников. "
        "Собери один связный ответ, используя только эти данные. "
        "Не упоминай LightRAG, shadow search, fallback, карточки, технические детали системы или внутренние режимы поиска. "
        "Не копируй сырые поля как дамп. "
        f"{_USER_ENTITY_WORDING_PROMPT}"
        "Сохраняй осторожные формулировки: подозрения остаются подозрениями, заявления источника остаются заявлениями источника. "
        f"{_CLAIM_TYPE_TAG_PROMPT}"
        "Не называй утверждения фальшивыми, ложными, дезинформацией или имитацией, если это явно не сказано в данных. "
        "Если дополнительные данные только дублируют или слабо связаны с вопросом, не раздувай ответ."
    )
    if include_visual:
        system += " Вопрос просит визуалы, поэтому можно использовать визуальные заметки и кадры."
    else:
        system += " Не включай B-roll, визуальные заметки или предложения кадров."
    if profile == "source":
        system += " Так как пользователь просит источник, укажи конкретные файлы/ссылки из контекста."

    user = (
        f"Вопрос:\n{question}\n\n"
        f"Черновой ответ графа:\n{graph_answer}\n\n"
        f"Дополнительный контекст источников:\n{context}\n\n"
        "Ответь в 2-5 абзацах. Не добавляй факты вне предоставленных данных."
    )

    try:
        if config.QUERY_DELAY_SECONDS > 0:
            await asyncio.sleep(config.QUERY_DELAY_SECONDS)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if llm_backend.is_luna_role("fallback_synth"):
            try:
                answer = (
                    await llm_backend.complete_text_async(
                        messages,
                        role="fallback_synth",
                        timeout_seconds=config.FALLBACK_SYNTH_TIMEOUT_SECONDS,
                    )
                ).strip()
            except llm_backend.LLMBackendError:
                if not config.CODEX_FALLBACK_TO_API:
                    raise
                logger.warning("Codex fallback synthesis failed; explicit API fallback is enabled")
                client = _openai_client(
                    config.FALLBACK_SYNTH_API_KEY,
                    config.FALLBACK_SYNTH_BASE_URL,
                    timeout=min(config.LLM_TIMEOUT_SECONDS, config.FALLBACK_SYNTH_TIMEOUT_SECONDS),
                    max_retries=0,
                )
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=config.FALLBACK_SYNTH_MODEL,
                        messages=messages,
                        **_chat_completion_options(
                            max_tokens=config.FALLBACK_SYNTH_MAX_TOKENS,
                            temperature=0,
                        ),
                    ),
                    timeout=config.FALLBACK_SYNTH_TIMEOUT_SECONDS + 5,
                )
                answer = (response.choices[0].message.content or "").strip()
        else:
            client = _openai_client(
                config.FALLBACK_SYNTH_API_KEY,
                config.FALLBACK_SYNTH_BASE_URL,
                timeout=min(config.LLM_TIMEOUT_SECONDS, config.FALLBACK_SYNTH_TIMEOUT_SECONDS),
                max_retries=0,
            )
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=config.FALLBACK_SYNTH_MODEL,
                    messages=messages,
                    **_chat_completion_options(
                        max_tokens=config.FALLBACK_SYNTH_MAX_TOKENS,
                        temperature=0,
                    ),
                ),
                timeout=config.FALLBACK_SYNTH_TIMEOUT_SECONDS + 5,
            )
            answer = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning(f"Hybrid synthesis failed; keeping LightRAG answer with card references: {exc}")
        return _attach_card_context(result, card_context, prefer_card_references=prefer_card_references)

    if not answer:
        return _attach_card_context(result, card_context, prefer_card_references=prefer_card_references)
    if _answer_looks_corrupt(answer):
        logger.warning("Hybrid synthesis looked corrupt; keeping LightRAG answer with card references.")
        return _attach_card_context(result, card_context, prefer_card_references=prefer_card_references)

    answer = _postprocess_answer_text(answer, question, query_profile)
    fixed = _attach_card_context(result, card_context, prefer_card_references=prefer_card_references)
    llm_response = dict(fixed.get("llm_response") or {})
    llm_response["content"] = answer
    fixed["llm_response"] = llm_response
    fixed["response"] = answer
    fixed["hybrid_context"] = "cards"
    return fixed


async def _query_rag_result_legacy(
    rag: LightRAG,
    question: str,
    mode: str | None = None,
    query_profile: str | None = None,
) -> dict[str, Any]:
    """Preserve the pre-Late-Fusion query path for disabled flag and direct rollback."""
    if mode is None:
        mode = "mix" if config.RERANKER_ENABLED else "hybrid"
    profile = get_query_profile(query_profile)
    user_prompt = profile["user_prompt"]

    logger.info(
        f"Querying LightRAG with retrieval payload (mode={mode}, profile={query_profile or 'answer'}, rerank={'enabled' if config.RERANKER_ENABLED else 'disabled'})"
    )

    try:
        token = _LLM_ROLE.set("query")
        result = await asyncio.wait_for(
            rag.aquery_llm(
                question,
                param=QueryParam(
                    mode=mode,
                    enable_rerank=config.RERANKER_ENABLED,
                    include_references=True,
                    top_k=profile["top_k"],
                    chunk_top_k=profile["chunk_top_k"],
                    response_type=_QUERY_RESPONSE_TYPE,
                    user_prompt=user_prompt,
                ),
            ),
            timeout=config.QUERY_TIMEOUT_SECONDS,
        )
        _LLM_ROLE.reset(token)
    except TimeoutError:
        _LLM_ROLE.reset(token)
        logger.warning(
            "LightRAG query timed out after %ss; trying shadow-search fallback.",
            config.QUERY_TIMEOUT_SECONDS,
        )
        fallback = await _try_shadow_fallback_result(question, query_profile)
        if fallback:
            return fallback
        return {
            "response": "В базе не удалось получить ответ за отведённое время.",
            "llm_response": {"content": "В базе не удалось получить ответ за отведённое время."},
            "data": {"references": []},
            "fallback": "timeout_no_context",
        }
    except Exception as exc:
        _LLM_ROLE.reset(token)
        logger.warning(f"LightRAG query failed; trying shadow-search fallback: {exc}")
        fallback = await _try_shadow_fallback_result(question, query_profile)
        if fallback:
            return fallback
        return {
            "response": "В базе не удалось получить ответ.",
            "llm_response": {"content": "В базе не удалось получить ответ."},
            "data": {"references": []},
            "fallback": "error_no_context",
        }
    if isinstance(result, dict) and not _is_funding_question(question) and _response_has_no_context(result):
        fallback = await _try_shadow_fallback_result(question, query_profile)
        if fallback:
            logger.info("Using shadow-search fallback after no-context LightRAG answer.")
            return fallback
    if isinstance(result, dict) and not _is_funding_question(question) and _response_looks_corrupt(result):
        fallback = await _try_shadow_fallback_result(question, query_profile)
        if fallback:
            logger.info("Using shadow-search fallback after corrupt LightRAG answer.")
            return fallback
    if isinstance(result, dict):
        answer = str(result.get("llm_response", {}).get("content") or result.get("response") or "")
        fixed_answer = _postprocess_answer_text(answer, question, query_profile)
        if fixed_answer != answer:
            result = result.copy()
            llm_response = dict(result.get("llm_response") or {})
            llm_response["content"] = fixed_answer
            result["llm_response"] = llm_response
            result["response"] = fixed_answer
        if not _is_funding_question(question):
            card_context = _card_context_for_query(question, query_profile)
            if card_context:
                logger.info("Adding enriched-card context to LightRAG answer.")
                result = await _synthesize_hybrid_result(
                    question,
                    query_profile,
                    result,
                    card_context,
                )
        result = _attach_reference_hints(result, question)
    return result


async def query_rag_result(
    rag: LightRAG,
    question: str,
    mode: str | None = None,
    query_profile: str | None = None,
) -> dict[str, Any]:
    """Route query-time execution to Late Fusion or the unchanged legacy path."""
    if not config.LATE_FUSION_ENABLED:
        return await _query_rag_result_legacy(
            rag,
            question,
            mode=mode,
            query_profile=query_profile,
        )

    from geospoiler.answering.late_fusion import LateFusionFallbackRequired, query_late_fusion

    effective_mode = mode or "mix"
    try:
        return await query_late_fusion(
            rag,
            question,
            mode=effective_mode,
            query_profile=query_profile,
        )
    except LateFusionFallbackRequired as exc:
        logger.warning("Late Fusion requested direct legacy fallback: %s", exc)
        return await _legacy_after_late_fusion(
            rag,
            question,
            mode=effective_mode,
            query_profile=query_profile,
            reason=str(exc),
            recovered_references=exc.references,
            late_fusion_trace=exc.trace,
        )
    except Exception as exc:
        logger.exception("Unexpected Late Fusion failure; using direct legacy fallback")
        return await _legacy_after_late_fusion(
            rag,
            question,
            mode=effective_mode,
            query_profile=query_profile,
            reason=f"unexpected_late_fusion_error:{type(exc).__name__}",
        )


async def _legacy_after_late_fusion(
    rag: LightRAG,
    question: str,
    *,
    mode: str,
    query_profile: str | None,
    reason: str,
    recovered_references: list[dict[str, Any]] | None = None,
    late_fusion_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the private legacy implementation once, without re-entering the public router."""
    canonical_references = [
        dict(reference)
        for reference in (recovered_references or [])
        if str(reference.get("url") or "").casefold().startswith(("http://", "https://"))
    ]
    if query_profile == "source" and canonical_references:
        references = []
        lines = [
            "Не удалось надёжно сформировать связный ответ с цитатами. "
            "Вот проверяемые источники, которые уже были найдены:"
        ]
        for reference in canonical_references:
            fixed_reference = dict(reference)
            fixed_reference["cited_in_answer"] = True
            references.append(fixed_reference)
            reference_id = str(fixed_reference.get("reference_id") or "")
            title = str(fixed_reference.get("title") or "Источник").strip() or "Источник"
            lines.append(f"- [{reference_id}] {title}: {fixed_reference['url']}")
        answer = "\n".join(lines)
        trace = dict(late_fusion_trace or {})
        trace.update(
            {
                "pipeline": "legacy_fallback",
                "fallback_reason": reason,
                "recovered_reference_count": len(references),
            }
        )
        return {
            "response": answer,
            "llm_response": {"content": answer},
            "data": {"references": references, "late_fusion": trace},
        }

    legacy = await _query_rag_result_legacy(
        rag,
        question,
        mode=mode,
        query_profile=query_profile,
    )
    fixed = dict(legacy)
    data = dict(fixed.get("data") or {})
    trace = dict(late_fusion_trace or {})
    trace.update({"pipeline": "legacy_fallback", "fallback_reason": reason})
    data["late_fusion"] = trace
    fixed["data"] = data
    return fixed
