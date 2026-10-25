"""
LLM Enricher v2 — extracts semantic payload from preprocessed text.

Returns LLMPayload only: summary, key_points, entities, topics, theses,
quotes, events, search_phrases, quality_flags.

Does NOT return: graph_text, search_text, provenance, content_type,
source_chain, ignored_blocks, schema_version.
"""

import json
import logging

import requests

from geospoiler import config
from geospoiler.contracts.models import LLMPayload
from geospoiler.ingestion.enricher.preprocessor import PreprocessedText
from geospoiler.providers import llm_backend
from geospoiler.providers.llm_auth import auth_headers

logger = logging.getLogger("geospoiler.enricher.llm")


class EmptyLLMResponseError(ValueError):
    """Raised when an extraction call returns no JSON object at all."""


class TransientLLMError(RuntimeError):
    """Raised when a bounded retryable backend failure exhausted its budget."""

# ── System prompt v2 — production version ─────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an information extraction module for a local personal RAG/OSINT knowledge base.

Your task is to convert one NORMALIZED TEXT document into one compact enriched JSON payload for search and retrieval.

You are not an analyst, fact-checker, wiki writer, or canonical resolver.

Hard rules:
- Use only the provided normalized text and metadata.
- Do not use external knowledge.
- Do not verify, correct, dispute, or judge source claims.
- Do not add causes, consequences, motives, background, or context unless explicitly present in the normalized text.
- Do not canonicalize entities.
- Do not create canonical IDs.
- Extract entities only as raw surface forms present in the normalized text.
- Do not add aliases that are not present in the normalized text.
- Do not open links or infer contents of external pages.
- Do not process video, audio, images, Instagram, YouTube, or Telegram media yourself.
- Native/legacy media placeholders are noise and must be ignored.
- If information is missing, use null, "", or [].
- quality_flags may describe source/content quality only. Do not emit
  technical pipeline flags (contains_legacy_media_placeholders,
  transcript_unavailable, timestamps_unavailable, partial_segment_failure,
  extraction_unstable, or possible_duplicate); those require deterministic
  runtime evidence after enrichment.
- Return valid JSON only. No markdown. No commentary.
- Do not output graph_text, search_text, provenance, content_type, source_chain, schema_version, or ignored_blocks.

Language policy:
- summary, key_points, topics, theses, events — write in RUSSIAN.
- entities.text — keep original surface form as it appears in the text (NATO, DPRK, Xi Jinping, etc.).
- quotes.text — keep in original language (verbatim).
- search_phrases — mix: Russian meaning + original terms from text.

Important interpretation rules:
- summary describes what the source says, claims, reports, discusses, or quotes.
- key_points are source-stated content points, not verified facts.
- topics are specific search-oriented tags based only on the text.
- theses are only explicit positions, framings, narratives, accusations, or predictions in the text.
- quotes must be verbatim or near-verbatim from the text.
- events are concrete reported actions/statements/decisions/etc., not broad themes.
- search_phrases are useful search queries built only from terms present in the text. Morphological normalization is OK, adding synonyms/translations/aliases is NOT.

Content cleaning:
- Exclude boilerplate, promotional lines, donation requests, subscription calls, generic disclaimers, navigation text, and technical placeholders.
- Do NOT remove politically meaningful rhetoric, accusations, or emotional language if it is part of substantive source content.

Output schema:

{
  "summary": "1-3 sentences in Russian",
  "key_points": [{"text": "...", "type": "reported_statement|reported_event|opinion|prediction|accusation|quote_summary|source_reference|announcement|numeric_claim|other", "importance": "high|medium|low", "evidence": "...or null"}],
  "entities": {"people": [{"text":"...","role":"...","salience":"primary|secondary|mentioned"}], "organizations":[], "countries":[], "locations":[], "military_units":[], "equipment":[], "weapons":[], "programs_projects":[], "media_sources":[], "other":[]},
  "topics": [{"label":"...","salience":"primary|secondary","type":"case_topic|policy_topic|military_topic|diplomatic_topic|economic_topic|rhetoric_topic|source_topic|regional_topic|technology_topic|sanctions_topic|energy_topic|migration_topic|other"}],
  "theses": [{"text":"...","speaker":"...or null","stance":"supportive|critical|accusatory|alarmist|sarcastic|neutral_explanatory|interpretive|predictive|mobilizing|unclear","evidence":"...or null"}],
  "quotes": [{"text":"...","speaker":"...or null","context":"...or null"}],
  "events": [{"event_type":"reported_statement|meeting|agreement|attack|strike|military_movement|exercise|launch|decision|vote|publication|announcement|negotiation|sanction|accusation|arrest|border_incident|economic_measure|unknown","description":"...","date_text":"...or null","date_normalized":"YYYY-MM-DD or null","location":"...or null","actors":[]}],
  "search_phrases": [{"text":"...","source":"surface_form|phrase_from_text|constructed_from_present_terms"}],
  "quality_flags": []
}

Return valid JSON only."""

_USER_PROMPT = """\
Normalized text (content_type: {content_type}):
---
{text}
---

Extract structured information. Return only valid JSON."""

_SYSTEM_PROMPT_SHORT = _SYSTEM_PROMPT + """\

You are an information extraction module for a local personal RAG/OSINT knowledge base.
Extract minimal structure from a short post. Same rules: no external knowledge, no fact-checking, no canonicalization, raw surface forms only.
Write summary and key_points in RUSSIAN. Entities in original form.
Do not output graph_text, search_text, provenance, content_type, source_chain, schema_version, or ignored_blocks.
Return every field from the exact Output schema. Use empty arrays when a category has no content.
Return only valid JSON, no markdown."""

_USER_PROMPT_SHORT = """\
Short post (content_type: {content_type}):
---
{text}
---

Return the complete payload using the exact Output schema from the system prompt.
Keep summary to 1-2 sentences, key_points to 1-3 items, and topics to 1-3 items."""

_SYSTEM_PROMPT_CHUNK = _SYSTEM_PROMPT + """\

You are an information extraction module. Extract structured data from this FRAGMENT of a long transcript.
Same rules: no external knowledge, no fact-checking, raw surface forms only.
Write summary and key_points in RUSSIAN. Entities in original form.
Return every field from the exact Output schema. Use empty arrays when a category has no content.
Return only valid JSON, no markdown."""

_USER_PROMPT_CHUNK = """\
Fragment #{chunk_index} of {total_chunks}:
---
{text}
---

Return the complete payload using the exact Output schema from the system prompt.
Preserve summary, key_points, entities, topics, theses, quotes, events,
search_phrases, and quality_flags from this fragment."""

_SYSTEM_PROMPT_MERGE = _SYSTEM_PROMPT + """\

You are an information extraction module. Merge fragment results into one unified extraction payload.
Deduplicate facts, preserve ALL unique information.
Write all text fields in RUSSIAN except verbatim quotes and entity surface forms.
Same rules: no external knowledge, no fact-checking, raw surface forms only.
Do not output graph_text, search_text, provenance, content_type, source_chain, schema_version, or ignored_blocks.
Return only valid JSON, no markdown."""

_USER_PROMPT_MERGE = """\
Post header: {header}

Fragment data:
{chunks_data}

Return unified JSON with: summary (3-5 sentences), key_points, entities, topics, theses, quotes, events, search_phrases, quality_flags."""


# ── Public API ──────────────────────────────────────────────────────────────

def enrich_short_post(preprocessed: PreprocessedText, content_type: str) -> LLMPayload:
    """Enrich a short post (<500 chars body) with minimal LLM extraction."""
    return _normalize_to_payload(extract_short_post_raw(preprocessed, content_type))


def extract_short_post_raw(preprocessed: PreprocessedText, content_type: str) -> dict:
    """Return raw JSON for a short post so the pipeline can apply repair policy."""
    if preprocessed.body_char_count < 20:
        return {}

    return _call_llm(
        system=_SYSTEM_PROMPT_SHORT,
        user=_USER_PROMPT_SHORT.format(content_type=content_type, text=preprocessed.clean_text),
    )


def enrich_full_post(preprocessed: PreprocessedText, content_type: str) -> LLMPayload:
    """Enrich a regular post with full LLM extraction."""
    return _normalize_to_payload(extract_full_post_raw(preprocessed, content_type))


def extract_full_post_raw(preprocessed: PreprocessedText, content_type: str) -> dict:
    """Return raw JSON for a regular post so the pipeline can apply repair policy."""
    return _call_llm(
        system=_SYSTEM_PROMPT,
        user=_USER_PROMPT.format(content_type=content_type, text=preprocessed.clean_text),
    )


def enrich_chunk(text: str, chunk_index: int, total_chunks: int) -> dict:
    """Enrich a single chunk of a long-form post. Returns raw dict for merging."""
    return _normalize_to_payload(
        extract_chunk_raw(text, chunk_index, total_chunks)
    ).model_dump(mode="json")


def extract_chunk_raw(text: str, chunk_index: int, total_chunks: int) -> dict:
    """Return raw JSON for one internal chunk."""
    return _call_llm(
        system=_SYSTEM_PROMPT_CHUNK,
        user=_USER_PROMPT_CHUNK.format(
            chunk_index=chunk_index + 1,
            total_chunks=total_chunks,
            text=text,
        ),
    )


def merge_chunk_results(header: str, chunk_results: list[dict]) -> LLMPayload:
    """Merge multiple chunk results into a single LLMPayload."""
    return _normalize_to_payload(merge_chunk_results_raw(header, chunk_results))


def merge_chunk_results_raw(header: str, chunk_results: list[dict]) -> dict:
    """Return raw merged JSON while preserving every semantic chunk category."""
    chunks_data = _serialize_chunk_results(chunk_results)
    return _call_llm(
        system=_SYSTEM_PROMPT_MERGE,
        user=_USER_PROMPT_MERGE.format(header=header, chunks_data=chunks_data),
    )


def _serialize_chunk_results(chunk_results: list[dict]) -> str:
    """Serialize complete internal chunk payloads for the merge prompt."""
    semantic_fields = (
        "summary",
        "key_points",
        "entities",
        "topics",
        "theses",
        "quotes",
        "events",
        "search_phrases",
        "quality_flags",
    )
    fragments = []
    for index, chunk_result in enumerate(chunk_results):
        payload = {field: chunk_result.get(field) for field in semantic_fields}
        fragments.append(
            {
                "fragment_index": index + 1,
                "char_range": chunk_result.get("char_range"),
                "payload": payload,
            }
        )
    return json.dumps(fragments, ensure_ascii=False, indent=2)


# ── LLM call ───────────────────────────────────────────────────────────────

_CODEX_ENRICHMENT_ATTEMPTS = 2


def _is_retryable_codex_error(exc: Exception) -> bool:
    """Recognize transport failures that are safe to retry once."""
    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "timed out",
            "timeout",
            "connection",
            "temporarily unavailable",
            "broken pipe",
            "transport",
        )
    )


def _complete_luna_json(messages: list[dict], *, schema: dict) -> dict:
    """Call Codex with one bounded retry for transient enrichment failures."""
    for attempt in range(1, _CODEX_ENRICHMENT_ATTEMPTS + 1):
        try:
            return llm_backend.complete_json_sync(
                messages,
                role="enrichment",
                schema=schema,
                timeout_seconds=config.CODEX_LLM_TIMEOUT_SECONDS,
            )
        except llm_backend.LLMBackendError as exc:
            if not _is_retryable_codex_error(exc):
                raise
            if attempt < _CODEX_ENRICHMENT_ATTEMPTS:
                logger.warning(
                    "Codex enrichment backend failure; retrying bounded attempt %s/%s: %s",
                    attempt + 1,
                    _CODEX_ENRICHMENT_ATTEMPTS,
                    exc,
                )
                continue
            if config.CODEX_FALLBACK_TO_API:
                raise
            raise TransientLLMError(
                f"Codex enrichment backend failed after {_CODEX_ENRICHMENT_ATTEMPTS} attempts: {exc}"
            ) from exc
    raise AssertionError("unreachable")

def _call_llm(system: str, user: str) -> dict:
    """Call the LLM API and parse JSON response."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if llm_backend.is_luna_role("enrichment"):
        try:
            return _complete_luna_json(messages, schema=LLMPayload.model_json_schema())
        except llm_backend.LLMBackendError as exc:
            if not config.CODEX_FALLBACK_TO_API:
                logger.error("Codex enrichment failed: %s", exc)
                return {}
            logger.warning("Codex enrichment failed; explicit API fallback is enabled: %s", exc)

    api_key = config.ENRICHMENT_API_KEY
    if not api_key or api_key == "your-api-key-here":
        logger.warning("No ENRICHMENT_API_KEY configured; returning empty enrichment.")
        return {}

    payload = {
        "model": config.ENRICHMENT_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    payload.update(_deepseek_v4_options(config.ENRICHMENT_MODEL, config.ENRICHMENT_BASE_URL))

    try:
        content = _post_with_hard_timeout(payload)
        return _parse_json_response(content)

    except _HardTimeoutError:
        logger.warning(
            f"LLM enrichment hard timeout ({config.LLM_TIMEOUT_SECONDS}s)."
        )
        return {}
    except requests.Timeout:
        logger.warning("LLM enrichment timeout (no bytes received).")
        return {}
    except requests.HTTPError as e:
        logger.error(f"LLM enrichment HTTP error: {e}")
        if e.response is not None:
            if e.response.status_code == 429:
                logger.warning("Rate limited (429). Returning empty.")
                return {}
            if e.response.status_code == 400:
                return _call_llm_fallback(system, user)
        return {}
    except Exception as e:
        logger.error(f"LLM enrichment error: {e}")
        return {}


class _HardTimeoutError(Exception):
    pass


def _post_with_hard_timeout(payload: dict) -> str:
    import concurrent.futures
    import time

    def _do_request():
        if config.LLM_DELAY_SECONDS > 0:
            time.sleep(config.LLM_DELAY_SECONDS)

        response = requests.post(
            f"{config.ENRICHMENT_BASE_URL}/chat/completions",
            headers=auth_headers(config.ENRICHMENT_API_KEY, config.ENRICHMENT_BASE_URL),
            json=payload,
            timeout=config.LLM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_do_request)
        try:
            return future.result(timeout=config.LLM_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise _HardTimeoutError() from None


def _call_llm_fallback(system: str, user: str) -> dict:
    """Fallback LLM call without response_format."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if llm_backend.is_luna_role("enrichment"):
        try:
            return llm_backend.complete_json_sync(
                messages,
                role="enrichment",
                schema=LLMPayload.model_json_schema(),
                timeout_seconds=config.CODEX_LLM_TIMEOUT_SECONDS,
            )
        except llm_backend.LLMBackendError as exc:
            if not config.CODEX_FALLBACK_TO_API:
                logger.error("Codex enrichment repair failed: %s", exc)
                return {}
            logger.warning("Codex enrichment repair failed; explicit API fallback is enabled: %s", exc)

    api_key = config.ENRICHMENT_API_KEY
    payload = {
        "model": config.ENRICHMENT_MODEL,
        "messages": messages,
        "temperature": 0.1,
    }
    payload.update(_deepseek_v4_options(config.ENRICHMENT_MODEL, config.ENRICHMENT_BASE_URL))

    try:
        response = requests.post(
            f"{config.ENRICHMENT_BASE_URL}/chat/completions",
            headers=auth_headers(api_key, config.ENRICHMENT_BASE_URL),
            json=payload,
            timeout=config.LLM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
        return _parse_json_response(content)
    except Exception as e:
        logger.error(f"LLM enrichment fallback error: {e}")
        return {}


# ── Normalization ─────────────────────────────────────────────────────────────

def _normalize_to_payload(raw: object) -> LLMPayload:
    """Validate raw LLM JSON at the v2 payload boundary.

    The model occasionally uses ``name`` for an entity surface form even
    after being shown the v2 schema.  This narrow boundary adapter keeps the
    final card strict while preventing a harmless field-name variation from
    consuming the job's only structural repair attempt.
    """
    if not isinstance(raw, dict) or not raw:
        raise EmptyLLMResponseError("LLM extraction returned an empty JSON payload")
    normalized = _normalize_entity_surface_keys(raw)
    normalized = _normalize_quality_flag_aliases(normalized)
    return LLMPayload.model_validate(normalized)


def _normalize_entity_surface_keys(raw: dict) -> dict:
    """Map the common model-only entity ``name`` spelling to v2 ``text``."""
    entities = raw.get("entities")
    if not isinstance(entities, dict):
        return raw

    normalized = dict(raw)
    normalized_entities = dict(entities)
    changed = False
    for category, items in entities.items():
        if not isinstance(items, list):
            continue
        normalized_items = []
        for item in items:
            if isinstance(item, dict) and "text" not in item and "name" in item:
                item = {**item, "text": item["name"]}
                item.pop("name", None)
                changed = True
            normalized_items.append(item)
        if normalized_items != items:
            normalized_entities[category] = normalized_items
    if changed:
        normalized["entities"] = normalized_entities
    return normalized


_QUALITY_FLAG_ALIASES = {
    "noisy_boilerplate_detected": "mostly_boilerplate",
}


def _normalize_quality_flag_aliases(raw: dict) -> dict:
    """Map known model wording variants to the v2 quality-flag vocabulary."""
    flags = raw.get("quality_flags")
    if not isinstance(flags, list):
        return raw

    normalized_flags = [
        _QUALITY_FLAG_ALIASES.get(str(flag).casefold(), flag)
        for flag in flags
    ]
    if normalized_flags == flags:
        return raw

    normalized = dict(raw)
    normalized["quality_flags"] = list(dict.fromkeys(normalized_flags))
    return normalized


# ── Helpers ─────────────────────────────────────────────────────────────────

def _deepseek_v4_options(model: str, base_url: str) -> dict:
    text = f"{model} {base_url}".casefold()
    if config.LLM_REASONING_EFFORT:
        return {}
    if "deepseek-v4" not in text and "api.deepseek.com" not in text:
        return {}
    return {"thinking": {"type": "disabled"}}


def _parse_json_response(content: str) -> dict:
    """Parse JSON from LLM response, handling markdown code blocks."""
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines)

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse LLM JSON: {e}")
        logger.debug(f"Raw content: {content[:500]}")
        return {}
