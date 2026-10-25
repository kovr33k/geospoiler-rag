import logging
import re
from functools import lru_cache

import requests
from lingua import Language, LanguageDetectorBuilder

from geospoiler import config
from geospoiler.providers import llm_backend
from geospoiler.providers.llm_auth import auth_headers

logger = logging.getLogger("geospoiler.normalizer.translator")

TRANSLATOR_SYSTEM_PROMPT = """You are an expert translator and OSINT analyst.
Your task is to ensure the provided text is in Russian.
If the text is already entirely or predominantly in Russian, return it exactly as is, without any modifications.
If it is in Ukrainian, English, or any other language, translate it to Russian accurately.
Preserve all original meaning, entities, quotes, formatting, and tone.
DO NOT add any conversational filler like 'Here is the translation'. Output ONLY the final Russian text."""


@lru_cache(maxsize=1)
def _russian_detector():
    return LanguageDetectorBuilder.from_languages(Language.RUSSIAN, Language.UKRAINIAN, Language.BULGARIAN).build()


def is_confidently_russian(text: str) -> bool:
    """Conservative local bypass; unknown and mixed prose still reaches translation.

    Require local Russian confidence against Ukrainian/Bulgarian, not just
    Cyrillic. Check clauses separately so a Russian introduction cannot mask a
    foreign continuation. Very short and ambiguous fragments retain translation.
    """
    candidate = re.sub(r"https?://\S+|www\.\S+", " ", text)
    candidate = re.sub(
        r"(?m)^\[(?:(?:Изображение|Видео|Аудио|Документ):[^\n]*|Изображение|Текст с экрана \(OCR\)|Текст документа|(?:Video |Voice )?[Tt]ranscript[^\n]*)\]\s*$",
        "", candidate,
    )
    found_prose = False
    for sentence in re.split(r"[.!?,;:\n]+", candidate):
        words = re.findall(r"[^\W\d_]+", sentence, re.UNICODE)
        if not words:
            continue
        acronyms = [word for word in words if word.isascii() and word.isupper() and len(word) <= 5]
        if len(acronyms) > 1:
            return False
        words = [word for word in words if word not in acronyms]
        if len(words) < 3:
            return False
        found_prose = True
        if any(not re.fullmatch(r"[а-яё]+", word.casefold()) for word in words):
            return False
        confidence = _russian_detector().compute_language_confidence_values(" ".join(words))[0]
        if confidence.language != Language.RUSSIAN or confidence.value < 0.95:
            return False
    return found_prose


def translate_to_russian_if_needed(text: str) -> str:
    """
    Translates the text to Russian using the configured LLM,
    or leaves it unchanged if it is already in Russian.
    """
    if not text or not text.strip():
        return text
    if is_confidently_russian(text):
        return text

    messages = [
        {"role": "system", "content": TRANSLATOR_SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    if llm_backend.is_luna_role("default"):
        try:
            return llm_backend.complete_text_sync(
                messages,
                role="default",
                timeout_seconds=config.CODEX_LLM_TIMEOUT_SECONDS,
            ).strip()
        except llm_backend.LLMBackendError as exc:
            if not config.CODEX_FALLBACK_TO_API:
                logger.warning("Codex translation failed; returning original text: %s", exc)
                return text
            logger.warning("Codex translation failed; explicit API fallback is enabled: %s", exc)

    api_key = config.TRANSLATION_API_KEY
    if not api_key or api_key == "your-api-key-here":
        logger.warning("No TRANSLATION_API_KEY configured; skipping translation.")
        return text

    payload = {
        "model": config.TRANSLATION_MODEL,
            "messages": messages,
        "temperature": 0.1,  # Low temperature for accurate translation
    }
    payload.update(_deepseek_v4_options(config.TRANSLATION_MODEL, config.TRANSLATION_BASE_URL))

    try:
        response = requests.post(
            f"{config.TRANSLATION_BASE_URL}/chat/completions",
            headers=auth_headers(api_key, config.TRANSLATION_BASE_URL),
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        translated_text = data["choices"][0]["message"]["content"].strip()
        return translated_text
    except requests.Timeout:
        logger.warning("LLM translation timeout. Returning original text.")
        return text
    except Exception as e:
        logger.error(f"LLM translation error: {e}")
        return text


def _deepseek_v4_options(model: str, base_url: str) -> dict:
    endpoint = f"{model} {base_url}".casefold()
    if config.LLM_REASONING_EFFORT:
        return {}
    if "deepseek-v4" not in endpoint and "api.deepseek.com" not in endpoint:
        return {}
    return {"thinking": {"type": "disabled"}}
