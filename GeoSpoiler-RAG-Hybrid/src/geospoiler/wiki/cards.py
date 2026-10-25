"""Native Enriched v2 card adapters and Wiki input decomposition."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from pydantic import ValidationError

from geospoiler.contracts.models import EnrichedCardV2, YouTubeSegmentCardV2
from geospoiler.wiki.hashing import JsonPath, canonical_json, sha256_hex
from geospoiler.wiki.state import deterministic_source_lineage_id

CardModel: TypeAlias = EnrichedCardV2 | YouTubeSegmentCardV2

CLAIM_INPUT_KIND = "claim_inputs"
STRUCTURED_RELATION_INPUT_KIND = "structured_relation_inputs"
CARD_PROJECTION_INPUT_KIND = "card_projection_inputs"
ELIGIBILITY_INPUT_KIND = "eligibility_inputs"

WIKI_RELEVANT_QUALITY_FLAGS = frozenset(
    {
        "extraction_unstable",
        "no_substantive_content",
        "partial_segment_failure",
    }
)

_CLAIM_COLLECTIONS: tuple[str, ...] = ("key_points", "theses", "quotes", "events")
_ENTITY_COLLECTIONS: tuple[str, ...] = (
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
)


class CardDocumentError(ValueError):
    """Raised when a JSON document is not a supported native card document."""


@dataclass(frozen=True)
class AdaptedWikiCard:
    """Canonical Wiki-facing representation of one validated native card."""

    native_card: CardModel
    source_kind: str
    external_key: str
    source_lineage_id: str
    card_revision_id: str
    card_payload: dict[str, Any]
    input_payloads: dict[str, dict[str, Any]]
    card_unordered_collection_paths: tuple[JsonPath, ...]
    card_exact_quote_paths: tuple[JsonPath, ...]
    input_unordered_collection_paths: dict[str, tuple[JsonPath, ...]]
    input_exact_quote_paths: dict[str, tuple[JsonPath, ...]]


def parse_card(value: Mapping[str, Any] | CardModel) -> CardModel:
    """Validate one native Enriched v2 or YouTube segment card."""
    if isinstance(value, EnrichedCardV2 | YouTubeSegmentCardV2):
        return value
    schema_version = value.get("schema_version")
    try:
        if schema_version == "enriched_v2":
            return EnrichedCardV2.model_validate(value)
        if schema_version == "youtube_segment_v2":
            return YouTubeSegmentCardV2.model_validate(value)
    except ValidationError as exc:
        raise CardDocumentError(str(exc)) from exc
    raise CardDocumentError(
        "Unsupported card schema_version; expected enriched_v2 or youtube_segment_v2"
    )


def load_card_file(path: str | Path) -> tuple[CardModel, ...]:
    """Load and validate a JSON object or array without using path/index as identity."""
    source_path = Path(path)
    try:
        raw = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CardDocumentError(f"{source_path}: {exc}") from exc

    values: Sequence[Any]
    if isinstance(raw, Mapping):
        values = (raw,)
    elif isinstance(raw, list):
        values = raw
    else:
        raise CardDocumentError(f"{source_path}: card JSON must be an object or array")

    parsed: list[CardModel] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise CardDocumentError(f"{source_path}: every array item must be an object")
        parsed.append(parse_card(value))
    return tuple(parsed)


def discover_card_files(path: str | Path) -> tuple[Path, ...]:
    """Return deterministic JSON file order for one file or a recursive directory."""
    root = Path(path)
    if root.is_file():
        return (root,)
    if not root.is_dir():
        raise CardDocumentError(f"Card input path does not exist: {root}")
    return tuple(
        sorted(
            (candidate for candidate in root.rglob("*.json") if candidate.is_file()),
            key=lambda candidate: candidate.as_posix(),
        )
    )


def adapt_card(value: Mapping[str, Any] | CardModel) -> AdaptedWikiCard:
    """Split one native card into overlapping, independently versioned Wiki inputs."""
    card = parse_card(value)
    if isinstance(card, EnrichedCardV2):
        return _adapt_enriched(card)
    return _adapt_youtube_segment(card)


def _adapt_enriched(card: EnrichedCardV2) -> AdaptedWikiCard:
    data = card.model_dump(mode="json")
    source_kind = _enriched_source_kind(card)
    external_key = card.provenance.source_id
    source_metadata = {
        "source_id": card.provenance.source_id,
        "source_type": card.provenance.source_type,
        "channel": card.provenance.channel,
        "date": card.provenance.date,
        "post_url": card.provenance.post_url,
        "message_id": card.provenance.message_id,
        "forwarded_from": card.provenance.forwarded_from,
        "source_title": card.provenance.source_title,
        "parent_source_id": card.provenance.parent_source_id,
    }
    card_payload = {
        "card_schema": card.schema_version,
        "source_kind": source_kind,
        "source": source_metadata,
        "content_type": card.content_type,
        "language": card.language,
        "summary": card.summary,
        **{field: data[field] for field in _CLAIM_COLLECTIONS},
        "entities": data["entities"],
        "topics": data["topics"],
        "search_phrases": data["search_phrases"],
        "quality_flags": sorted(set(card.quality_flags)),
        "extraction_issues": sorted(set(card.extraction_issues)),
        "source_chain": data["source_chain"],
    }
    input_payloads = _input_payloads(
        data=data,
        card_schema=card.schema_version,
        source_kind=source_kind,
        content_type=card.content_type,
        language=card.language,
        summary=card.summary,
        source_metadata=source_metadata,
        schema_eligible=True,
    )
    return _finish_adaptation(
        card=card,
        source_kind=source_kind,
        external_key=external_key,
        card_payload=card_payload,
        input_payloads=input_payloads,
        include_source_chain=True,
    )


def _adapt_youtube_segment(card: YouTubeSegmentCardV2) -> AdaptedWikiCard:
    data = card.model_dump(mode="json")
    source_kind = "youtube_segment"
    external_key = card.segment_id
    source_metadata = {
        "segment_id": card.segment_id,
        "parent_source_id": card.parent_source_id,
        "video_id": card.video_id,
        "title": card.title,
        "start_seconds": card.start_seconds,
        "end_seconds": card.end_seconds,
        "start_url": card.start_url,
        "chapter_titles": card.chapter_titles,
        "char_range": card.char_range,
        "transcript_text": card.transcript_text,
    }
    card_payload = {
        "card_schema": card.schema_version,
        "source_kind": source_kind,
        "source": source_metadata,
        "content_type": "youtube_segment",
        "language": "und",
        "summary": card.summary,
        **{field: data[field] for field in _CLAIM_COLLECTIONS},
        "entities": data["entities"],
        "topics": data["topics"],
        "search_phrases": data["search_phrases"],
        "quality_flags": sorted(set(card.quality_flags)),
        "extraction_issues": sorted(set(card.extraction_issues)),
    }
    input_payloads = _input_payloads(
        data=data,
        card_schema=card.schema_version,
        source_kind=source_kind,
        content_type="youtube_segment",
        language="und",
        summary=card.summary,
        source_metadata=source_metadata,
        schema_eligible=True,
    )
    return _finish_adaptation(
        card=card,
        source_kind=source_kind,
        external_key=external_key,
        card_payload=card_payload,
        input_payloads=input_payloads,
        include_source_chain=False,
    )


def _input_payloads(
    *,
    data: Mapping[str, Any],
    card_schema: str,
    source_kind: str,
    content_type: str,
    language: str,
    summary: str,
    source_metadata: Mapping[str, Any],
    schema_eligible: bool,
) -> dict[str, dict[str, Any]]:
    quality_flags = sorted(
        flag for flag in set(data["quality_flags"]) if flag in WIKI_RELEVANT_QUALITY_FLAGS
    )
    claim_inputs = {
        "card_schema": card_schema,
        "content_type": content_type,
        "language": language,
        **{field: data[field] for field in _CLAIM_COLLECTIONS},
    }
    relation_source = {
        "source_kind": source_kind,
        "source_type": source_metadata.get("source_type", source_kind),
    }
    structured_relation_inputs = {
        "entities": data["entities"],
        "topics": data["topics"],
        "source_context": relation_source,
    }
    display_source = {
        key: source_metadata.get(key)
        for key in (
            "source_id",
            "source_type",
            "channel",
            "date",
            "post_url",
            "source_title",
            "parent_source_id",
            "segment_id",
            "video_id",
            "title",
            "start_seconds",
            "end_seconds",
            "start_url",
            "chapter_titles",
        )
        if key in source_metadata
    }
    card_projection_inputs = {
        "summary": summary,
        "key_points": data["key_points"],
        "topics": data["topics"],
        "search_phrases": data["search_phrases"],
        "content_type": content_type,
        "language": language,
        "display_source": display_source,
    }
    eligibility_inputs = {
        "quality_flags": quality_flags,
        "schema_eligible": schema_eligible,
    }
    return {
        CLAIM_INPUT_KIND: claim_inputs,
        STRUCTURED_RELATION_INPUT_KIND: structured_relation_inputs,
        CARD_PROJECTION_INPUT_KIND: card_projection_inputs,
        ELIGIBILITY_INPUT_KIND: eligibility_inputs,
    }


def _finish_adaptation(
    *,
    card: CardModel,
    source_kind: str,
    external_key: str,
    card_payload: dict[str, Any],
    input_payloads: dict[str, dict[str, Any]],
    include_source_chain: bool,
) -> AdaptedWikiCard:
    card_unordered = _card_unordered_paths(include_source_chain=include_source_chain)
    card_quote_paths = (("quotes", "*", "text"),)
    canonical_payload = canonical_json(
        card_payload,
        unordered_collection_paths=card_unordered,
        exact_quote_paths=card_quote_paths,
    )
    card_revision_id = f"cardrev:v1:sha256:{sha256_hex(canonical_payload)}"
    source_lineage_id = deterministic_source_lineage_id(
        source_kind=source_kind,
        external_key=external_key,
    )
    return AdaptedWikiCard(
        native_card=card,
        source_kind=source_kind,
        external_key=external_key,
        source_lineage_id=source_lineage_id,
        card_revision_id=card_revision_id,
        card_payload=card_payload,
        input_payloads=input_payloads,
        card_unordered_collection_paths=card_unordered,
        card_exact_quote_paths=card_quote_paths,
        input_unordered_collection_paths=_input_unordered_paths(),
        input_exact_quote_paths={
            CLAIM_INPUT_KIND: (("quotes", "*", "text"),),
        },
    )


def _card_unordered_paths(*, include_source_chain: bool) -> tuple[JsonPath, ...]:
    paths: list[JsonPath] = [
        *((field,) for field in (*_CLAIM_COLLECTIONS, "topics", "search_phrases")),
        ("quality_flags",),
        ("extraction_issues",),
        *((("entities", category)) for category in _ENTITY_COLLECTIONS),
        ("events", "*", "actors"),
    ]
    if include_source_chain:
        paths.extend(
            [
                ("source_chain", "mentioned_sources"),
                ("source_chain", "external_links"),
            ]
        )
    return tuple(paths)


def _input_unordered_paths() -> dict[str, tuple[JsonPath, ...]]:
    return {
        CLAIM_INPUT_KIND: (
            *((field,) for field in _CLAIM_COLLECTIONS),
            ("events", "*", "actors"),
        ),
        STRUCTURED_RELATION_INPUT_KIND: (
            ("topics",),
            *((("entities", category)) for category in _ENTITY_COLLECTIONS),
        ),
        CARD_PROJECTION_INPUT_KIND: (
            ("key_points",),
            ("topics",),
            ("search_phrases",),
        ),
        ELIGIBILITY_INPUT_KIND: (("quality_flags",),),
    }


def _enriched_source_kind(card: EnrichedCardV2) -> str:
    source_id = card.provenance.source_id.casefold()
    source_type = card.provenance.source_type.strip().casefold()
    if source_id.startswith("telegram:") or source_type == "telegram":
        return "telegram"
    if (
        source_id.startswith("youtube:")
        or source_type == "youtube"
        or card.content_type == "youtube"
    ):
        return "youtube"
    return source_type or card.content_type or "enriched"
