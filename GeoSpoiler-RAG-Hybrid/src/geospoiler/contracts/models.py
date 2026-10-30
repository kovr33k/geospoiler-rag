"""Pydantic data contracts for GeoSpoiler enriched v2 pipeline.

Two core models:
  LLMPayload     — what the LLM returns (semantic extraction only)
  EnrichedCardV2 — final assembled card (LLM payload + code-built fields)

No backward compatibility with v1. All cards are regenerated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── Allowed values ────────────────────────────────────────────────────────────

ALLOWED_POINT_TYPES = {
    "reported_statement", "reported_event", "opinion", "prediction",
    "accusation", "quote_summary", "source_reference", "announcement",
    "numeric_claim", "other",
}
ALLOWED_IMPORTANCE = {"high", "medium", "low"}
ALLOWED_SALIENCE = {"primary", "secondary", "mentioned"}
ALLOWED_TOPIC_TYPES = {
    "case_topic", "policy_topic", "military_topic", "diplomatic_topic",
    "economic_topic", "rhetoric_topic", "source_topic", "regional_topic",
    "technology_topic", "sanctions_topic", "energy_topic", "migration_topic",
    "other",
}
ALLOWED_STANCES = {
    "supportive", "critical", "accusatory", "alarmist", "sarcastic",
    "neutral_explanatory", "interpretive", "predictive", "mobilizing", "unclear",
}
ALLOWED_EVENT_TYPES = {
    "reported_statement", "meeting", "agreement", "attack", "strike",
    "military_movement", "exercise", "launch", "decision", "vote",
    "publication", "announcement", "negotiation", "sanction", "accusation",
    "arrest", "border_incident", "economic_measure", "unknown",
}
ALLOWED_CONTENT_TYPES = {
    "telegram_post", "telegram_forward", "youtube_transcript",
    "instagram_text", "web_article_text", "mixed_normalized_text", "unknown",
}
ALLOWED_QUALITY_FLAGS = {
    "mostly_boilerplate", "very_short_text", "contains_legacy_media_placeholders",
    "no_substantive_content", "unclear_source_chain", "mixed_topics",
    "possible_duplicate", "extraction_unstable",
    "transcript_unavailable", "timestamps_unavailable", "partial_segment_failure",
}
ALLOWED_SEARCH_PHRASE_SOURCES = {
    "surface_form", "phrase_from_text", "constructed_from_present_terms",
}
ALLOWED_IGNORED_BLOCK_TYPES = {
    "image", "video", "audio", "instagram", "youtube", "ai_chat", "media_omitted", "unknown",
}


# ── Base ──────────────────────────────────────────────────────────────────────

class StrictModel(BaseModel):
    """Strict model — no extra fields allowed."""
    model_config = ConfigDict(extra="forbid")


class FlexModel(BaseModel):
    """Forward-compatible model — unknown fields preserved."""
    model_config = ConfigDict(extra="allow")


# ── Shared sub-models ─────────────────────────────────────────────────────────

class KeyPoint(StrictModel):
    text: str
    type: str = Field(
        default="other",
        json_schema_extra={"enum": sorted(ALLOWED_POINT_TYPES)},
    )
    importance: str = Field(
        default="medium",
        json_schema_extra={"enum": sorted(ALLOWED_IMPORTANCE)},
    )
    evidence: str | None = None

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        return _require_allowed(value, ALLOWED_POINT_TYPES, "key point type")

    @field_validator("importance")
    @classmethod
    def _validate_importance(cls, value: str) -> str:
        return _require_allowed(value, ALLOWED_IMPORTANCE, "key point importance")


class EntityItem(StrictModel):
    text: str
    role: str = ""
    salience: str = Field(
        default="mentioned",
        json_schema_extra={"enum": sorted(ALLOWED_SALIENCE)},
    )

    @field_validator("salience")
    @classmethod
    def _validate_salience(cls, value: str) -> str:
        return _require_allowed(value, ALLOWED_SALIENCE, "entity salience")


class Entities(StrictModel):
    people: list[EntityItem] = Field(default_factory=list)
    organizations: list[EntityItem] = Field(default_factory=list)
    countries: list[EntityItem] = Field(default_factory=list)
    locations: list[EntityItem] = Field(default_factory=list)
    military_units: list[EntityItem] = Field(default_factory=list)
    equipment: list[EntityItem] = Field(default_factory=list)
    weapons: list[EntityItem] = Field(default_factory=list)
    programs_projects: list[EntityItem] = Field(default_factory=list)
    media_sources: list[EntityItem] = Field(default_factory=list)
    other: list[EntityItem] = Field(default_factory=list)


class Topic(StrictModel):
    label: str
    salience: str = Field(
        default="primary",
        json_schema_extra={"enum": sorted(ALLOWED_SALIENCE)},
    )
    type: str = Field(
        default="other",
        json_schema_extra={"enum": sorted(ALLOWED_TOPIC_TYPES)},
    )

    @field_validator("salience")
    @classmethod
    def _validate_salience(cls, value: str) -> str:
        return _require_allowed(value, ALLOWED_SALIENCE, "topic salience")

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        return _require_allowed(value, ALLOWED_TOPIC_TYPES, "topic type")


class Thesis(StrictModel):
    text: str
    speaker: str | None = None
    stance: str = Field(
        default="unclear",
        json_schema_extra={"enum": sorted(ALLOWED_STANCES)},
    )
    evidence: str | None = None

    @field_validator("stance")
    @classmethod
    def _validate_stance(cls, value: str) -> str:
        return _require_allowed(value, ALLOWED_STANCES, "thesis stance")


class Quote(StrictModel):
    text: str
    speaker: str | None = None
    context: str | None = None


class Event(StrictModel):
    event_type: str = Field(
        default="unknown",
        json_schema_extra={"enum": sorted(ALLOWED_EVENT_TYPES)},
    )
    description: str = ""
    date_text: str | None = None
    date_normalized: str | None = None
    location: str | None = None
    actors: list[str] = Field(default_factory=list)

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, value: str) -> str:
        return _require_allowed(value, ALLOWED_EVENT_TYPES, "event type")


class SearchPhrase(StrictModel):
    text: str
    source: str = Field(
        default="surface_form",
        json_schema_extra={"enum": sorted(ALLOWED_SEARCH_PHRASE_SOURCES)},
    )

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        return _require_allowed(value, ALLOWED_SEARCH_PHRASE_SOURCES, "search phrase source")


class IgnoredBlock(StrictModel):
    type: str = Field(
        default="unknown",
        json_schema_extra={"enum": sorted(ALLOWED_IGNORED_BLOCK_TYPES)},
    )
    text: str = ""

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        return _require_allowed(value, ALLOWED_IGNORED_BLOCK_TYPES, "ignored block type")


# ── LLM Payload ───────────────────────────────────────────────────────────────

class LLMPayload(StrictModel):
    """What the LLM returns — semantic extraction only.

    Does NOT include: schema_version, provenance, content_type, source_chain,
    graph_text, search_text, ignored_blocks (preprocessor fills those).
    """
    summary: str = ""
    key_points: list[KeyPoint] = Field(default_factory=list)
    entities: Entities = Field(default_factory=Entities)
    topics: list[Topic] = Field(default_factory=list)
    theses: list[Thesis] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    search_phrases: list[SearchPhrase] = Field(default_factory=list)
    quality_flags: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "items": {"type": "string", "enum": sorted(ALLOWED_QUALITY_FLAGS)}
        },
    )

    @field_validator("quality_flags")
    @classmethod
    def _validate_quality_flags(cls, values: list[str]) -> list[str]:
        for value in values:
            _require_allowed(value, ALLOWED_QUALITY_FLAGS, "quality flag")
        return values


# ── Provenance & Source Chain ─────────────────────────────────────────────────

class SourceId(FlexModel):
    value: str

    @classmethod
    def from_provenance(cls, provenance: Provenance | dict[str, Any]) -> SourceId | None:
        data = provenance.model_dump() if isinstance(provenance, BaseModel) else provenance
        existing = _clean_str(data.get("source_id"))
        if existing:
            return cls(value=existing)

        message_id = _clean_str(data.get("message_id"))
        if not message_id:
            return None

        channel_id = _clean_str(data.get("channel_id"))
        if channel_id:
            return cls(value=f"telegram:{channel_id}:{message_id}")

        channel = _clean_str(data.get("channel")) or _clean_str(data.get("channel_name"))
        if channel:
            return cls(value=f"telegram:{channel}:{message_id}")

        return None


class Provenance(StrictModel):
    source_id: str = Field(min_length=1)
    source_type: str = ""
    channel: str = ""
    date: str | None = None
    post_url: str = ""
    message_id: str | int | None = None
    forwarded_from: str | None = None
    normalized_path: str = ""
    source_title: str = ""
    parent_source_id: str | None = None

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("source_id must not be empty")
        return value


class SourceChain(StrictModel):
    original_source: str | None = None
    forwarded_from: str | None = None
    mentioned_sources: list[str] = Field(default_factory=list)
    external_links: list[dict[str, str]] = Field(default_factory=list)


# ── Enriched Card V2 (final assembled card) ───────────────────────────────────

class EnrichedCardV2(StrictModel):
    """Final enriched card — assembled by postprocessor from LLMPayload + code fields."""
    schema_version: Literal["enriched_v2"]
    prompt_version: str = Field(min_length=1)
    enrichment_model: str = Field(min_length=1)
    media_contract_version: str = ""
    enriched_at: str = Field(min_length=1)
    provenance: Provenance
    content_type: str = "unknown"
    language: str = "ru"

    # LLM payload fields
    summary: str = ""
    key_points: list[KeyPoint] = Field(default_factory=list)
    entities: Entities = Field(default_factory=Entities)
    topics: list[Topic] = Field(default_factory=list)
    theses: list[Thesis] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    search_phrases: list[SearchPhrase] = Field(default_factory=list)

    # Code-built fields
    source_chain: SourceChain = Field(default_factory=SourceChain)
    graph_text: str = ""
    search_text: str = ""
    ignored_blocks: list[IgnoredBlock] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    extraction_issues: list[str] = Field(default_factory=list)
    # source_id of the earlier source with identical content; a copy reuses
    # its semantic fields and stays out of the search indexes.
    duplicate_of: str | None = None

    @field_validator("content_type")
    @classmethod
    def _validate_content_type(cls, value: str) -> str:
        return _require_allowed(value, ALLOWED_CONTENT_TYPES, "content type")

    @field_validator("quality_flags")
    @classmethod
    def _validate_quality_flags(cls, values: list[str]) -> list[str]:
        for value in values:
            _require_allowed(value, ALLOWED_QUALITY_FLAGS, "quality flag")
        return values

    @property
    def source_id(self) -> SourceId | None:
        return SourceId.from_provenance(self.provenance)


class YouTubeSegmentCardV2(StrictModel):
    """Retrieval child card for one deterministic YouTube transcript segment."""

    schema_version: Literal["youtube_segment_v2"]
    enrichment_model: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    parent_source_id: str = Field(min_length=1)
    video_id: str = Field(min_length=1)
    segment_index: int = Field(ge=0)
    title: str = ""
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    start_url: str = ""
    chapter_titles: list[str] = Field(default_factory=list)
    char_range: list[int] = Field(default_factory=list)
    transcript_text: str = ""

    summary: str = ""
    key_points: list[KeyPoint] = Field(default_factory=list)
    entities: Entities = Field(default_factory=Entities)
    topics: list[Topic] = Field(default_factory=list)
    theses: list[Thesis] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    search_phrases: list[SearchPhrase] = Field(default_factory=list)
    search_text: str = ""
    quality_flags: list[str] = Field(default_factory=list)
    extraction_issues: list[str] = Field(default_factory=list)
    # Episode source_id this segment was copied from; see EnrichedCardV2.duplicate_of.
    duplicate_of: str | None = None

    @model_validator(mode="after")
    def _validate_ranges(self) -> YouTubeSegmentCardV2:
        if len(self.char_range) not in (0, 2) or (
            len(self.char_range) == 2 and self.char_range[1] < self.char_range[0]
        ):
            raise ValueError("char_range must contain an ordered start/end pair")
        if self.start_seconds is not None and self.end_seconds is not None:
            if self.end_seconds < self.start_seconds:
                raise ValueError("end_seconds must be >= start_seconds")
        return self

    @field_validator("quality_flags")
    @classmethod
    def _validate_quality_flags(cls, values: list[str]) -> list[str]:
        for value in values:
            _require_allowed(value, ALLOWED_QUALITY_FLAGS, "quality flag")
        return values


# ── Normalized Meta (unchanged) ───────────────────────────────────────────────

class NormalizedMeta(FlexModel):
    channel_name: str = ""
    channel_id: int | str | None = None
    channel_username: str = ""
    message_id: int | str | None = None
    date: str = ""
    post_url: str = ""
    is_forward: bool = False
    forward_from_name: str | None = None
    forward_from_id: int | str | None = None
    forward_date: str | None = None
    has_text: bool = False
    has_body_text: bool | None = None
    has_images: bool = False
    image_count: int = 0
    has_video: bool = False
    has_voice: bool = False
    has_document: bool = False
    youtube_urls: list[str] = Field(default_factory=list)
    instagram_urls: list[str] = Field(default_factory=list)
    ai_chat_urls: list[str] = Field(default_factory=list)
    web_urls: list[str] = Field(default_factory=list)
    media: list[Any] = Field(default_factory=list)

    @field_validator("youtube_urls", "instagram_urls", "ai_chat_urls", "web_urls", mode="before")
    @classmethod
    def _coerce_url_list(cls, value: Any) -> list[str]:
        return _string_list(value)

    @property
    def source_id(self) -> SourceId | None:
        return SourceId.from_provenance(self.model_dump())


# ── Other models (retrieval/eval) ─────────────────────────────────────────────

class QueryProfile(FlexModel):
    name: Literal["answer", "source", "overview"]
    mode: str = "hybrid"
    hybrid_synth_enabled: bool = True


class ExperimentRun(FlexModel):
    run_id: str
    model: str
    provider: str = ""
    score: float | None = None
    passed: int | None = None
    total: int | None = None
    avg_duration_seconds: float | None = None
    cache_cleared: bool = False
    artifact_paths: list[Path] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_clean_str(item) for item in value if _clean_str(item)]
    text = _clean_str(value)
    return [text] if text else []


def _require_allowed(value: str, allowed: set[str], label: str) -> str:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"Unknown {label} '{value}'. Allowed: {choices}")
    return value


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
