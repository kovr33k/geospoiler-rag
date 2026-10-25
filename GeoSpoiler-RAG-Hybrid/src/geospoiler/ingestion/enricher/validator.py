"""
Validator — checks LLMPayload for contract violations.

Does NOT check truth. Checks:
- Required fields present
- No canonical_id
- No external aliases
- Quotes exist in source text
- Topics not overly generic
- Entities not from ignored blocks
- No forbidden quality_flags
- Payload not empty when text is substantive
"""

import logging
import re
from dataclasses import dataclass, field

from geospoiler.contracts.models import LLMPayload
from geospoiler.retrieval.cards.query_terms import matches_term

logger = logging.getLogger("geospoiler.enricher.validator")

_FORBIDDEN_FLAGS = {"fake_news", "propaganda", "false_claim", "misinformation", "disinformation"}
_GENERIC_TOPICS = {"геополитика", "политика", "война", "новости", "мир", "общество", "экономика"}
_GENERIC_TOPIC_THRESHOLD = 0.6
LANGUAGE_VIOLATION_PREFIX = "Russian-language contract violation"
_UKRAINIAN_SPECIFIC_RE = re.compile(r"[іїєґІЇЄҐ]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_HAN_RE = re.compile(r"[\u3400-\u9fff]")
_REQUIRED_RUSSIAN_FIELDS = (
    ("summary", lambda payload: [payload.summary]),
    ("key_points", lambda payload: [item.text for item in payload.key_points]),
    ("topics", lambda payload: [item.label for item in payload.topics]),
    ("theses", lambda payload: [item.text for item in payload.theses]),
    ("events", lambda payload: [item.description for item in payload.events]),
)


@dataclass
class ValidationResult:
    is_valid: bool = True
    violations: list[str] = field(default_factory=list)
    should_repair: bool = False

    def add(self, violation: str) -> None:
        self.violations.append(violation)
        self.is_valid = False
        self.should_repair = True


def validate_payload(
    payload: LLMPayload,
    clean_text: str,
    ignored_block_texts: list[str] | None = None,
) -> ValidationResult:
    """
    Validate LLMPayload against contract rules.

    Args:
        payload: The LLM extraction result
        clean_text: The preprocessed clean text that was sent to LLM
        ignored_block_texts: Texts of blocks that were removed by preprocessor
    """
    result = ValidationResult()
    ignored_texts = ignored_block_texts or []
    text_lower = clean_text.lower()

    _check_forbidden_flags(payload, result)
    _check_generic_topics(payload, result)
    _check_quotes_in_text(payload, clean_text, result)
    _check_entities_not_from_ignored(payload, ignored_texts, result)
    _check_search_phrases(payload, text_lower, result)
    _check_payload_not_empty(payload, clean_text, result)
    for violation in required_russian_violations(payload):
        result.add(violation)

    return result


def required_russian_violations(payload: LLMPayload) -> list[str]:
    """Return language violations for semantic fields that must be Russian.

    Quotes, search phrases, and entity surface forms intentionally remain outside
    this check because they preserve source language and multilingual retrieval.
    """
    entity_forms = _entity_surface_forms(payload)
    violations: list[str] = []
    for field_name, values_getter in _REQUIRED_RUSSIAN_FIELDS:
        for index, value in enumerate(values_getter(payload)):
            text = str(value or "").strip()
            if not text:
                continue
            candidate = _without_entity_surface_forms(text, entity_forms)
            reason = non_russian_prose_reason(candidate)
            if reason is None:
                continue
            suffix = "" if field_name == "summary" else f"[{index}]"
            violations.append(
                f"{LANGUAGE_VIOLATION_PREFIX}: {field_name}{suffix} {reason}. "
                "Rewrite this semantic field in Russian while preserving quotes "
                "and entity surface forms in their dedicated fields."
            )
    return violations


def has_required_language_violations(
    validation_or_messages: ValidationResult | list[str] | tuple[str, ...],
) -> bool:
    """Return whether validation output contains a Russian-language breach."""
    if isinstance(validation_or_messages, ValidationResult):
        messages = validation_or_messages.violations
    else:
        messages = validation_or_messages
    return any(str(message).startswith(LANGUAGE_VIOLATION_PREFIX) for message in messages)


def _entity_surface_forms(payload: LLMPayload) -> list[str]:
    forms: list[str] = []
    for category in (
        "people",
        "organizations",
        "countries",
        "locations",
        "military_units",
        "equipment",
        "weapons",
        "programs_projects",
        "media_sources",
        "other",
    ):
        forms.extend(
            item.text.strip()
            for item in getattr(payload.entities, category)
            if item.text.strip()
        )
    return sorted(set(forms), key=len, reverse=True)


def _without_entity_surface_forms(text: str, entity_forms: list[str]) -> str:
    candidate = text
    for form in entity_forms:
        candidate = re.sub(re.escape(form), " ", candidate, flags=re.IGNORECASE)
    return candidate


def non_russian_prose_reason(text: str) -> str | None:
    ukrainian_count = len(_UKRAINIAN_SPECIFIC_RE.findall(text))
    if ukrainian_count:
        return f"contains Ukrainian prose ({ukrainian_count} language-specific character(s))"

    cyrillic_count = len(_CYRILLIC_RE.findall(text))
    latin_count = len(_LATIN_RE.findall(text))
    han_count = len(_HAN_RE.findall(text))
    if latin_count >= 12 and latin_count > max(8, cyrillic_count * 2):
        return "is dominated by non-Russian Latin prose"
    if han_count >= 4 and han_count > cyrillic_count:
        return "is dominated by non-Russian Han prose"
    return None


def _check_forbidden_flags(payload: LLMPayload, result: ValidationResult) -> None:
    for flag in payload.quality_flags:
        if flag.lower() in _FORBIDDEN_FLAGS:
            result.add(f"Forbidden quality_flag: '{flag}'. Remove it.")


def _check_generic_topics(payload: LLMPayload, result: ValidationResult) -> None:
    if not payload.topics:
        return
    generic_count = sum(
        1 for t in payload.topics if t.label.lower().strip() in _GENERIC_TOPICS
    )
    ratio = generic_count / len(payload.topics) if payload.topics else 0
    if ratio > _GENERIC_TOPIC_THRESHOLD:
        generic_labels = [t.label for t in payload.topics if t.label.lower().strip() in _GENERIC_TOPICS]
        result.add(
            f"Too many generic topics ({generic_count}/{len(payload.topics)}): "
            f"{', '.join(generic_labels)}. Replace with specific topics from text."
        )


def _check_quotes_in_text(payload: LLMPayload, clean_text: str, result: ValidationResult) -> None:
    for i, quote in enumerate(payload.quotes):
        quote_text = quote.text.strip()
        if not quote_text:
            continue
        if not _fuzzy_quote_match(quote_text, clean_text):
            result.add(
                f"quotes[{i}] not found in source text: "
                f"'{quote_text[:80]}...'. Remove or fix."
            )


def _check_entities_not_from_ignored(
    payload: LLMPayload,
    ignored_texts: list[str],
    result: ValidationResult,
) -> None:
    if not ignored_texts:
        return
    ignored_combined = " ".join(_ignored_content_text(text) for text in ignored_texts).lower()
    for cat_name in [
        "people", "organizations", "countries", "locations",
        "military_units", "equipment", "weapons",
        "programs_projects", "media_sources", "other",
    ]:
        items = getattr(payload.entities, cat_name)
        for i, entity in enumerate(items):
            if entity.text.lower() in ignored_combined:
                result.add(
                    f"entities.{cat_name}[{i}] '{entity.text}' appears to be "
                    f"extracted from an ignored block. Remove it."
                )


def _ignored_content_text(text: str) -> str:
    """Remove technical placeholder metadata before entity comparison."""
    if "отправлено в очередь" in text.casefold():
        return ""
    return re.sub(
        r"\b(?:status|path|mime)=[^\s|]+",
        "",
        text,
        flags=re.IGNORECASE,
    )


def _check_search_phrases(payload: LLMPayload, text_lower: str, result: ValidationResult) -> None:
    source_words = _tokenize(text_lower)
    for i, phrase in enumerate(payload.search_phrases):
        words = _tokenize(phrase.text)
        if not words:
            continue
        missing = [
            word
            for word in words
            if not any(matches_term(word, source_word) for source_word in source_words)
        ]
        if len(missing) > len(words) * 0.5:
            result.add(
                f"search_phrases[{i}] '{phrase.text}' contains terms not in text: "
                f"{', '.join(missing[:5])}. Remove or fix."
            )


def drop_invalid_optional_items(payload: LLMPayload, validation: ValidationResult) -> list[str]:
    """Drop only invalid optional quote/search entries.

    These fields improve retrieval but are not the card's substantive record.
    Keeping a useful summary unusable because one generated quote or phrase is
    not grounded in the source is worse than omitting that optional item.
    Returns the violations that were removed, or an empty list when a fatal
    violation is also present.
    """
    invalid: dict[str, set[int]] = {"quotes": set(), "search_phrases": set()}
    pattern = re.compile(r"^(quotes|search_phrases)\[(\d+)\]")
    for violation in validation.violations:
        match = pattern.match(violation)
        if not match:
            return []
        invalid[match.group(1)].add(int(match.group(2)))

    removed = list(validation.violations)
    for field_name, indexes in invalid.items():
        if indexes:
            values = getattr(payload, field_name)
            setattr(payload, field_name, [value for i, value in enumerate(values) if i not in indexes])
    return removed


def _check_payload_not_empty(payload: LLMPayload, clean_text: str, result: ValidationResult) -> None:
    if len(clean_text.strip()) < 30:
        return
    has_content = bool(payload.summary) or bool(payload.key_points)
    if not has_content:
        result.add("Payload is empty but source text has content. Extract at minimum a summary.")


def _fuzzy_quote_match(quote: str, text: str) -> bool:
    """Check if quote is approximately present in text (handles minor whitespace/punctuation diffs)."""
    normalized_quote = _normalize_for_match(quote)
    normalized_text = _normalize_for_match(text)

    if normalized_quote in normalized_text:
        return True

    if "..." in normalized_quote:
        parts = [part.strip() for part in normalized_quote.split("...") if part.strip()]
        cursor = 0
        if parts and all(len(part.split()) >= 3 for part in parts):
            for part in parts:
                position = normalized_text.find(part, cursor)
                if position < 0:
                    break
                cursor = position + len(part)
            else:
                return True

    words = normalized_quote.split()
    if len(words) >= 5:
        middle_chunk = " ".join(words[1:-1])
        if middle_chunk in normalized_text:
            return True

    return False


def _normalize_for_match(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[«»\"''""„‟]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _tokenize(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", text.lower().strip()) if len(w) > 2]
