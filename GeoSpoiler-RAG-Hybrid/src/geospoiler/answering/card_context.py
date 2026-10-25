"""Enriched-card shadow fallback and hybrid card context helpers."""

import asyncio
import json
from pathlib import Path
from typing import Any

from geospoiler import config
from geospoiler.answering.answer_postprocess import (
    _answer_looks_corrupt,
    _postprocess_answer_text,
    _question_requests_visuals,
)
from geospoiler.answering.reference_hints import _existing_references, _merge_references, _resolve_match_source_path
from geospoiler.providers import llm_backend
from geospoiler.providers.clients import _chat_completion_options, _openai_client
from geospoiler.providers.runtime import logger
from geospoiler.retrieval.cards.card_text import card_ranking_text
from geospoiler.sources.registry import resolve_source

_CLAIM_TYPE_LABELS = {
    "source_claim": "утверждение источника",
    "claim": "утверждение источника",
    "thesis": "утверждение источника",
    "quote": "цитата источника",
    "hypothesis": "гипотеза/оценка источника",
}
_CLAIM_TYPE_TAG_PROMPT = (
    "Теги [утверждение источника], [цитата источника] и [гипотеза/оценка источника] "
    "являются служебными сигналами: не выводи сами теги в финальном ответе. "
    "Если факт помечен таким тегом, не превращай его в установленный факт "
    "и сохраняй эту модальность в ответе. "
)
_USER_ENTITY_WORDING_PROMPT = (
    "Если пользователь использует конкретную формулировку имени, аббревиатуру "
    "или написание латиницей, сохрани эту формулировку пользователя в ответе хотя бы один раз. "
)


def _format_card_fact_for_context(fact: dict[str, Any]) -> str:
    text = str(fact.get("text") or "").strip()
    if not text:
        return ""
    point_type = str(fact.get("type") or "other").strip().casefold()
    if point_type in ("reported_statement", "reported_event", "announcement", "other"):
        return text
    label = _CLAIM_TYPE_LABELS.get(point_type, "утверждение источника")
    return f"[{label}] {text}"


def _card_fact_lines(
    card: dict[str, Any],
    limit: int = 4,
    *,
    include_visual: bool = False,
) -> list[str]:
    lines = []
    summary = str(card.get("summary") or "").strip()
    if summary:
        lines.append(summary)
    points = card.get("key_points") or []
    for fact in points:
        if not isinstance(fact, dict):
            continue
        text = _format_card_fact_for_context(fact)
        if text:
            lines.append(text)
        if len(lines) >= limit:
            break
    return lines[:limit]


def _load_shadow_card(card_path: str | None) -> dict[str, Any]:
    if not card_path:
        return {}
    try:
        card = json.loads(config.resolve_project_path(card_path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return card if isinstance(card, dict) else {}


def _reference_from_card(reference_id: str, file_path: str, card: dict[str, Any]) -> dict[str, Any]:
    reference: dict[str, Any] = {"reference_id": reference_id, "file_path": file_path}
    source_id = _extract_source_id(card)
    if not source_id:
        return reference

    reference["source_id"] = source_id
    try:
        passport = resolve_source(source_id, db_path=config.SOURCE_REGISTRY_DB_PATH)
    except Exception as exc:
        logger.debug("Source registry lookup failed for %s: %s", source_id, exc)
        passport = None
    if not passport:
        return reference

    if passport.post_url:
        reference["post_url"] = passport.post_url
    if passport.primary_url:
        reference["primary_url"] = passport.primary_url
    if passport.youtube_url:
        reference["youtube_url"] = passport.youtube_url
    if passport.channel_name:
        reference["channel"] = passport.channel_name
    if passport.date:
        reference["date"] = passport.date
    return reference


def _extract_source_id(card: dict[str, Any]) -> str | None:
    """Resolve an Enriched source id from card provenance."""
    provenance = card.get("provenance")
    if not isinstance(provenance, dict):
        provenance = card

    source_id = str(provenance.get("source_id") or "").strip()
    if source_id:
        return source_id

    message_id = str(provenance.get("message_id") or "").strip()
    if not message_id:
        return None
    channel_id = str(provenance.get("channel_id") or "").strip()
    if channel_id:
        return f"telegram:{channel_id}:{message_id}"
    channel_name = str(
        provenance.get("channel_name") or provenance.get("channel") or ""
    ).strip()
    return f"telegram:{channel_name}:{message_id}" if channel_name else None


def _shadow_body_text(match: Any, card: dict[str, Any]) -> str:
    """Return the source body used for topic/entity coverage scoring."""
    return card_ranking_text(card, getattr(match, "card_path", None)) or str(match.snippet or "")


def _shadow_match_text(match: Any, card: dict[str, Any]) -> str:
    parts = [
        str(match.title),
        str(match.source_path),
        str(match.snippet),
        _shadow_body_text(match, card),
    ]
    for fact in card.get("key_points") or []:
        if isinstance(fact, dict) and fact.get("text"):
            parts.append(str(fact["text"]))
    return "\n".join(parts)


_GENERIC_CONTEXT_QUERY_TERMS = (
    "какие",
    "какой",
    "какая",
    "какую",
    "какими",
    "база",
    "описывает",
    "можно",
    "использовать",
    "используй",
    "кадр",
    "кадры",
    "визуал",
    "визуалы",
    "ролик",
    "ролика",
    "отношение",
    "попытку",
    "связь",
    "связи",
    "видео",
    "broll",
    "source",
    "sources",
    "дай",
    "дайте",
    "реально",
    "источник",
    "источники",
)


def _content_query_terms(query_terms: list[str], shadow_search_module: Any) -> list[str]:
    """Keep topic/entity terms separate from generic task wording for card ranking."""
    content_terms = []
    for term in query_terms:
        if any(
            shadow_search_module._matches_term(term, generic)
            or shadow_search_module._matches_term(generic, term)
            for generic in _GENERIC_CONTEXT_QUERY_TERMS
        ):
            continue
        content_terms.append(term)
    return content_terms or query_terms


def _shadow_fallback_result(question: str, query_profile: str | None) -> dict[str, Any] | None:
    """Build a deterministic lexical fallback when vector/graph retrieval misses exact cards."""
    try:
        from geospoiler.retrieval.cards import shadow_search
    except Exception as exc:
        logger.debug(f"Shadow fallback unavailable: {exc}")
        return None

    query_terms = list(dict.fromkeys(shadow_search.query_terms(question)))
    required_terms = 1 if len(query_terms) <= 1 else 2
    content_terms = _content_query_terms(query_terms, shadow_search)
    include_visual = _question_requests_visuals(question)
    candidates = []
    candidate_top_k = max(60, config.HYBRID_QUERY_CARDS_TOP_K * 20)
    for match in shadow_search.search(question, top_k=candidate_top_k):
        card = _load_shadow_card(match.card_path)
        text_tokens = shadow_search._tokenize(_shadow_match_text(match, card))
        body_tokens = shadow_search._tokenize(_shadow_body_text(match, card))
        path_title_tokens = shadow_search._tokenize(f"{match.title} {match.source_path}")
        matched_terms = {
            term
            for term in query_terms
            if any(shadow_search._matches_term(token, term) for token in text_tokens)
        }
        path_title_terms = {
            term
            for term in query_terms
            if any(shadow_search._matches_term(token, term) for token in path_title_tokens)
        }
        content_matched_terms = {
            term
            for term in content_terms
            if any(shadow_search._matches_term(token, term) for token in body_tokens)
        }
        if len(matched_terms) >= required_terms:
            candidates.append((match, card, matched_terms, path_title_terms, content_matched_terms))

    if not candidates:
        return None

    content_term_counts = {
        term: sum(1 for item in candidates if term in item[4])
        for term in content_terms
    }

    def specificity_score(item: tuple[Any, dict[str, Any], set[str], set[str], set[str]]) -> float:
        return sum(1.0 / content_term_counts[term] for term in item[4] if content_term_counts.get(term))

    if include_visual:
        candidates.sort(
            key=lambda item: (len(item[4]), item[0].score, specificity_score(item), len(item[2]), len(item[3])),
            reverse=True,
        )
    else:
        candidates.sort(
            key=lambda item: (len(item[4]), len(item[3]), item[0].score, specificity_score(item), len(item[2])),
            reverse=True,
        )
    top_parent = Path(_resolve_match_source_path(candidates[0][0].source_path)).parent
    max_context_matches = 1 if include_visual else 3
    strong_matches = [
        item
        for item in candidates
        if Path(_resolve_match_source_path(item[0].source_path)).parent == top_parent
    ][:max_context_matches]
    if not strong_matches:
        return None

    references = []
    sections = []
    context_items = []
    for idx, (match, card, _matched_terms, _path_title_terms, _content_matched_terms) in enumerate(strong_matches, start=1):
        source_path = _resolve_match_source_path(match.source_path)
        reference = _reference_from_card(str(idx), source_path, card)
        references.append(reference)

        facts = _card_fact_lines(card, include_visual=include_visual)
        if facts:
            body = "\n".join(f"- {fact}" for fact in facts)
        else:
            body = f"- {match.snippet.strip()}"
            facts = [match.snippet.strip()]
        sections.append(f"[{idx}] Источник: {source_path}\n{body}")
        context_item = dict(reference)
        context_item.update(
            {
                "facts": facts,
                "snippet": str(match.snippet or "").strip(),
                "title": str(match.title or "").strip(),
            }
        )
        context_items.append(context_item)

    if query_profile == "source":
        intro = "Найденные источники по этому тезису:"
    elif include_visual:
        intro = "По найденным карточкам можно использовать такие кадры и визуалы:"
    else:
        intro = "По найденным релевантным карточкам:"

    answer = intro + "\n\n" + "\n\n".join(sections)
    if "тезис" in question.casefold() and "тезис" not in answer.casefold():
        answer = "Главный тезис по найденным карточкам:\n\n" + answer
    answer += "\n\n### References\n"
    for ref in references:
        answer += f"- [{ref['reference_id']}] {ref['file_path']}\n"
    answer = _postprocess_answer_text(answer, question, query_profile)

    return {
        "response": answer,
        "llm_response": {"content": answer},
        "data": {"references": references, "shadow_context": context_items},
        "fallback": "shadow_search",
    }


def _format_shadow_context(context_items: list[dict[str, Any]]) -> str:
    blocks = []
    for item in context_items:
        facts = [str(fact).strip() for fact in item.get("facts", []) if str(fact).strip()]
        fact_text = "\n".join(f"- {fact}" for fact in facts)
        blocks.append(
            f"[{item.get('reference_id')}] {item.get('file_path')}\n"
            f"{fact_text}"
        )
    return "\n\n".join(blocks)


async def _synthesize_shadow_fallback_result(
    question: str,
    query_profile: str | None,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Turn shadow-search matches into a normal user-facing answer."""
    data = dict(fallback.get("data") or {})
    context_items = data.get("shadow_context") or []
    if not context_items or not config.HYBRID_SYNTH_ENABLED or (
        not config.FALLBACK_SYNTH_API_KEY and not llm_backend.is_luna_role("fallback_synth")
    ):
        return fallback

    include_visual = _question_requests_visuals(question)
    profile = query_profile or "answer"
    context = _format_shadow_context(context_items)
    if not context.strip():
        return fallback

    system = (
        "Ты пишешь финальный ответ RAG-системы на русском языке. "
        "Используй только предоставленный контекст карточек. "
        "Не упоминай LightRAG, shadow search, fallback, точный поиск по карточкам или технические детали системы. "
        "Не копируй сырые поля карточки как дамп; сделай связный ответ. "
        "Если контекст подтверждает ключевой предмет вопроса, явно назови этот ключевой предмет вопроса в ответе. "
        f"{_USER_ENTITY_WORDING_PROMPT}"
        "Сохраняй осторожные формулировки: подозрения остаются подозрениями, заявления источника остаются заявлениями источника. "
        f"{_CLAIM_TYPE_TAG_PROMPT}"
        "Не называй утверждения фальшивыми, ложными, дезинформацией или имитацией, если это явно не сказано в контексте."
    )
    if include_visual:
        system += " Вопрос просит визуалы, поэтому можно использовать визуальные заметки и кадры."
    else:
        system += " Не включай B-roll, визуальные заметки или предложения кадров, если они случайно попали в контекст."
    if profile == "source":
        system += " Так как пользователь просит источник, укажи конкретные файлы/ссылки из контекста."

    if profile == "overview":
        system += (
            " Для обзорных вопросов о странах или регионах явно перечисляй все страны и регионы, "
            "которые названы в фактах контекста и связаны с темой вопроса. Не сворачивай конкретные страны "
            "только в общий регион, если они прямо присутствуют в контексте."
        )

    user = (
        f"Вопрос:\n{question}\n\n"
        f"Контекст карточек:\n{context}\n\n"
        "Ответь в 2-4 абзацах. Для source-вопроса можно использовать короткий список источников."
    )
    if profile == "overview":
        user += (
            "\n\nДля обзорного ответа сначала выпиши страны/регионы из контекста по отдельности, "
            "а затем кратко объясни, в каких сюжетах они появляются."
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
                logger.warning("Codex card fallback failed; explicit API fallback is enabled")
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
        logger.warning(f"Shadow fallback synthesis failed; using deterministic answer: {exc}")
        return fallback

    if not answer:
        return fallback
    if _answer_looks_corrupt(answer):
        logger.warning("Shadow fallback synthesis looked corrupt; using deterministic card fallback.")
        return fallback
    answer = _postprocess_answer_text(answer, question, query_profile)

    fixed = fallback.copy()
    fixed["response"] = answer
    fixed["llm_response"] = {"content": answer}
    fixed["fallback"] = "shadow_search_llm"
    return fixed


def _wiki_context_for_query(question: str) -> dict[str, Any] | None:
    """Return approved-hub context while preserving underlying card references."""
    if (
        not config.WIKI_ENABLED
        or not config.HYBRID_QUERY_WIKI_ENABLED
        or not config.WIKI_STATE_DB_PATH.exists()
    ):
        return None
    try:
        from geospoiler.wiki.projections import get_projection_artifact
        from geospoiler.wiki.schema import connect_database
        from geospoiler.wiki.search import search_wiki

        connection = connect_database(config.WIKI_STATE_DB_PATH)
        try:
            matches = search_wiki(
                connection,
                question,
                limit=config.HYBRID_QUERY_WIKI_TOP_K,
                document_kinds=("hub",),
            )
            references: list[dict[str, Any]] = []
            context_items: list[dict[str, Any]] = []
            for hub_index, match in enumerate(matches, start=1):
                artifact = get_projection_artifact(
                    connection,
                    projection_kind="hub",
                    scope_key=match.scope_key,
                )
                if artifact is None:
                    continue
                raw_sources = (
                    match.source_refs.get("sources", [])
                    if isinstance(match.source_refs, dict)
                    else []
                )
                first_reference_id = f"wiki-{hub_index}"
                references_before = len(references)
                for source_index, source in enumerate(raw_sources, start=1):
                    if not isinstance(source, dict):
                        continue
                    reference_id = f"wiki-{hub_index}-{source_index}"
                    reference = {
                        "reference_id": reference_id,
                        "file_path": (
                            str(source.get("source_id") or "").strip()
                            or f"wiki://hub/{match.scope_key}"
                        ),
                    }
                    for source_key, reference_key in (
                        ("source_id", "source_id"),
                        ("url", "post_url"),
                        ("date", "date"),
                    ):
                        value = source.get(source_key)
                        if value:
                            reference[reference_key] = value
                    references.append(reference)
                    if source_index == 1:
                        first_reference_id = reference_id
                if len(references) == references_before:
                    references.append(
                        {
                            "reference_id": first_reference_id,
                            "file_path": f"wiki://hub/{match.scope_key}",
                        }
                    )
                context_items.append(
                    {
                        "reference_id": first_reference_id,
                        "file_path": f"Wiki hub: {match.title}",
                        "title": match.title,
                        "facts": [artifact.rendered_content[:12_000]],
                        "wiki_scope_key": match.scope_key,
                    }
                )
        finally:
            connection.close()
    except Exception as exc:
        logger.debug("Wiki context unavailable: %s", exc)
        return None
    if not context_items:
        return None
    return {
        "references": references,
        "shadow_context": context_items,
    }


def _card_context_for_query(question: str, query_profile: str | None) -> dict[str, Any] | None:
    """Return approved Wiki hubs plus strong Enriched-card context."""
    wiki_context = _wiki_context_for_query(question)
    if not config.HYBRID_QUERY_CARDS_ENABLED:
        return wiki_context
    fallback = _shadow_fallback_result(question, query_profile)
    if not fallback:
        return wiki_context

    data = dict(fallback.get("data") or {})
    context_items = list(data.get("shadow_context") or [])[: max(1, config.HYBRID_QUERY_CARDS_TOP_K)]
    if not context_items:
        return wiki_context

    references = []
    for idx, item in enumerate(context_items, start=1):
        item["reference_id"] = f"card-{idx}"
        file_path = str(item.get("file_path") or "").strip()
        if file_path:
            reference = {"reference_id": f"card-{idx}", "file_path": file_path}
            for key in ("source_id", "post_url", "primary_url", "youtube_url", "channel", "date"):
                value = item.get(key)
                if value:
                    reference[key] = value
            references.append(reference)
    if not references:
        return wiki_context

    if wiki_context is None:
        return {"references": references, "shadow_context": context_items}
    return {
        "references": _merge_references(
            list(wiki_context.get("references") or []),
            references,
        ),
        "shadow_context": [
            *list(wiki_context.get("shadow_context") or []),
            *context_items,
        ],
    }


def _attach_card_context(
    result: dict[str, Any],
    card_context: dict[str, Any],
    *,
    prefer_card_references: bool = False,
) -> dict[str, Any]:
    fixed = result.copy()
    data = dict(fixed.get("data") or {})
    card_references = list(card_context.get("references") or [])
    existing_references = _existing_references(fixed)
    if prefer_card_references:
        data["references"] = _merge_references(card_references, existing_references)
    else:
        data["references"] = _merge_references(existing_references, card_references)
    data["shadow_context"] = list(card_context.get("shadow_context") or [])
    fixed["data"] = data
    return fixed


def _card_references_should_be_first(question: str, query_profile: str | None) -> bool:
    """Keep card references first because source-selection quality is card-grounded."""
    _ = (question, query_profile)
    return True
