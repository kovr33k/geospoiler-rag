"""Project-specific LightRAG extraction prompt and response cleanup."""

import re

from lightrag.prompt import PROMPTS

from geospoiler import config

_TUPLE_DELIMITER = PROMPTS["DEFAULT_TUPLE_DELIMITER"]
_COMPLETION_DELIMITER = PROMPTS["DEFAULT_COMPLETION_DELIMITER"]
_ALLOWED_ENTITY_TYPES = {entity_type.casefold() for entity_type in config.LIGHTRAG_ENTITY_TYPES}
_ENTITY_TYPE_REMAP = {
    key.casefold(): value.casefold()
    for key, value in config.LIGHTRAG_ENTITY_TYPE_REMAP.items()
}
_ENTITY_ALIAS_MAP = {
    key.casefold(): value.strip()
    for key, value in config.LIGHTRAG_ENTITY_ALIASES.items()
    if value.strip()
}
_URL_ENTITY_RE = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)
_DATE_ENTITY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?$")

def _configure_lightrag_prompts() -> None:
    """Override inconsistent upstream examples with a project-specific one."""
    PROMPTS["entity_extraction_examples"] = [
        """<Entity_types>
["person","organization","country","military_unit","event","location","conflict","document","other"]

<Input Text>
```
[Канал: Example Channel | Дата: 2026-01-01 10:00 | Пост: https://t.me/example/1]

Robert Fico formed a coalition with the Slovak National Party.
```

<Output>
entity{tuple_delimiter}Robert Fico{tuple_delimiter}person{tuple_delimiter}Robert Fico is a politician who formed a coalition described in the source text.
entity{tuple_delimiter}Slovak National Party{tuple_delimiter}organization{tuple_delimiter}Slovak National Party is a political organization that formed a coalition with Robert Fico.
relation{tuple_delimiter}Robert Fico{tuple_delimiter}Slovak National Party{tuple_delimiter}coalition,political alliance{tuple_delimiter}Robert Fico formed a coalition with the Slovak National Party.
{completion_delimiter}
"""
    ]

def _build_extraction_policy() -> str:
    """Project-specific extraction guidance layered on top of LightRAG prompts."""
    relation_policy = (
        "Prefer only explicitly stated relationships. Do not add speculative links."
        if config.RELATION_EXTRACTION_MODE == "explicit"
        else "Interpretive relationships are allowed only when the source text itself frames them as a clear alignment, backing, or strategic connection."
    )
    alias_lines = "\n".join(
        f"- `{alias}` -> `{canonical}`"
        for alias, canonical in sorted(_ENTITY_ALIAS_MAP.items())
    )
    return (
        "Ignore metadata headers and technical wrappers.\n"
        "Do not extract channel names, dates, URLs, post numbers, filenames, or placeholder media/review notes as entities.\n"
        "Use only the provided entity types. If a type does not fit exactly, map it to `other`.\n"
        f"{relation_policy}\n"
        "Canonicalize frequent aliases to these preferred names:\n"
        f"{alias_lines}"
    )


def _is_extraction_prompt(prompt: str, system_prompt: str | None) -> bool:
    """Detect LightRAG extraction prompts so we can constrain and sanitize them."""
    if not system_prompt:
        return False
    if "Knowledge Graph Specialist" not in system_prompt:
        return False
    return (
        "Data to be Processed" in prompt
        or "last extraction task" in prompt
        or "Completion Signal" in prompt
    )


def _canonicalize_entity_name(name: str) -> str:
    """Map common aliases to one canonical node label."""
    stripped = name.strip()
    return _ENTITY_ALIAS_MAP.get(stripped.casefold(), stripped)


def _sanitize_extraction_field(value: str) -> str:
    """Strip control delimiters so LightRAG can parse a stable tuple shape."""
    cleaned = str(value).replace(_TUPLE_DELIMITER, " ").replace(_COMPLETION_DELIMITER, " ")
    cleaned = cleaned.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _normalize_entity_type(entity_type: str) -> str:
    """Map model-produced types back into the local ontology."""
    normalized = entity_type.strip().casefold()
    if normalized in _ALLOWED_ENTITY_TYPES:
        return normalized
    remapped = _ENTITY_TYPE_REMAP.get(normalized, "other")
    return remapped if remapped in _ALLOWED_ENTITY_TYPES else "other"


def _is_noise_entity(entity_name: str) -> bool:
    """Reject technical artifacts that should never become graph nodes."""
    normalized = entity_name.strip()
    if not normalized:
        return True
    if _URL_ENTITY_RE.match(normalized):
        return True
    if _DATE_ENTITY_RE.match(normalized):
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        return True
    if "t.me/" in normalized.lower():
        return True
    return False


def _postprocess_extraction_response(response_text: str) -> str:
    """Normalize raw extraction output before LightRAG parses it."""
    entities: dict[str, tuple[str, str, str]] = {}
    relations: dict[tuple[str, str, str], tuple[str, str, str, str]] = {}

    for raw_line in str(response_text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == _COMPLETION_DELIMITER:
            continue
        if line.startswith(f"entity{_TUPLE_DELIMITER}"):
            parts = line.split(_TUPLE_DELIMITER)
            if len(parts) < 4:
                continue
            name = _canonicalize_entity_name(_sanitize_extraction_field(parts[1]))
            if _is_noise_entity(name):
                continue
            entity_type = _normalize_entity_type(_sanitize_extraction_field(parts[2]))
            description = _sanitize_extraction_field(_TUPLE_DELIMITER.join(parts[3:]))
            key = name.casefold()
            current = entities.get(key)
            if current is None or len(description) > len(current[2]):
                entities[key] = (name, entity_type, description)
            continue

        if line.startswith(f"relation{_TUPLE_DELIMITER}"):
            parts = line.split(_TUPLE_DELIMITER)
            if len(parts) < 5:
                continue
            source = _canonicalize_entity_name(_sanitize_extraction_field(parts[1]))
            target = _canonicalize_entity_name(_sanitize_extraction_field(parts[2]))
            if _is_noise_entity(source) or _is_noise_entity(target):
                continue
            keywords = _sanitize_extraction_field(parts[3])
            description = _sanitize_extraction_field(_TUPLE_DELIMITER.join(parts[4:]))
            relation_key = tuple(sorted((source.casefold(), target.casefold()))) + (keywords.casefold(),)
            current = relations.get(relation_key)
            if current is None or len(description) > len(current[3]):
                relations[relation_key] = (source, target, keywords, description)

    output_lines = [
        f"entity{_TUPLE_DELIMITER}{name}{_TUPLE_DELIMITER}{entity_type}{_TUPLE_DELIMITER}{description}"
        for name, entity_type, description in entities.values()
    ]
    output_lines.extend(
        f"relation{_TUPLE_DELIMITER}{source}{_TUPLE_DELIMITER}{target}{_TUPLE_DELIMITER}{keywords}{_TUPLE_DELIMITER}{description}"
        for source, target, keywords, description in relations.values()
        if source.casefold() in entities and target.casefold() in entities
    )
    output_lines.append(_COMPLETION_DELIMITER)
    return "\n".join(output_lines)
