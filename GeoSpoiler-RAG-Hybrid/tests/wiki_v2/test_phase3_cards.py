from __future__ import annotations

import json

import pytest

from models import EnrichedCardV2, YouTubeSegmentCardV2
from retrieval.wiki import (
    CLAIM_INPUT_KIND,
    ELIGIBILITY_INPUT_KIND,
    STRUCTURED_RELATION_INPUT_KIND,
    adapt_card,
    build_occurrence_blueprints,
    connect_database,
    ingest_card,
    load_card_file,
    occurrence_version_id,
    record_ingested_card,
)


def enriched_data(
    *,
    source_id: str = "telegram:test:phase3",
    content_type: str = "telegram_post",
    summary: str = "Summary",
    key_points: list[dict] | None = None,
    topics: list[dict] | None = None,
    entities: dict | None = None,
    theses: list[dict] | None = None,
    quotes: list[dict] | None = None,
    events: list[dict] | None = None,
    search_phrases: list[dict] | None = None,
    quality_flags: list[str] | None = None,
    search_text: str = "generated search",
    graph_text: str = "generated graph",
) -> dict:
    source_type = "youtube" if content_type == "youtube_transcript" else "telegram"
    return {
        "schema_version": "enriched_v2",
        "prompt_version": "prompt-v2",
        "enrichment_model": "test-model",
        "enriched_at": "2026-07-30T00:00:00Z",
        "provenance": {
            "source_id": source_id,
            "source_type": source_type,
            "source_title": "Native source",
        },
        "content_type": content_type,
        "language": "ru",
        "summary": summary,
        "key_points": key_points or [{"text": "Alpha"}],
        "entities": entities or {},
        "topics": topics or [],
        "theses": theses or [],
        "quotes": quotes or [],
        "events": events or [],
        "search_phrases": search_phrases or [],
        "source_chain": {},
        "graph_text": graph_text,
        "search_text": search_text,
        "ignored_blocks": [],
        "quality_flags": quality_flags or [],
        "extraction_issues": [],
    }


def youtube_segment_data(*, segment_id: str = "youtube:video-1:segment:0") -> dict:
    return {
        "schema_version": "youtube_segment_v2",
        "enrichment_model": "test-model",
        "segment_id": segment_id,
        "parent_source_id": "youtube:video-1",
        "video_id": "video-1",
        "segment_index": 0,
        "title": "Video",
        "start_seconds": 10.0,
        "end_seconds": 20.0,
        "start_url": "https://example.test/watch?t=10",
        "chapter_titles": ["Chapter"],
        "char_range": [0, 100],
        "transcript_text": "Native transcript evidence",
        "summary": "Segment summary",
        "key_points": [{"text": "Segment claim"}],
        "entities": {},
        "topics": [],
        "theses": [],
        "quotes": [{"text": "Exact  quote"}],
        "events": [],
        "search_phrases": [],
        "search_text": "generated segment search",
        "quality_flags": [],
        "extraction_issues": [],
    }


def _head_generations(connection, lineage_id: str) -> dict[str, int]:
    return {
        row["input_kind"]: int(row["current_input_generation"])
        for row in connection.execute(
            """
            SELECT input_kind, current_input_generation
            FROM lineage_input_heads
            WHERE source_lineage_id = ?
            """,
            (lineage_id,),
        )
    }


def test_telegram_youtube_enriched_and_segment_native_sources_are_accepted(wiki_db) -> None:
    telegram = adapt_card(EnrichedCardV2.model_validate(enriched_data()))
    youtube = adapt_card(
        EnrichedCardV2.model_validate(
            enriched_data(
                source_id="youtube:video-1",
                content_type="youtube_transcript",
            )
        )
    )
    segment = adapt_card(YouTubeSegmentCardV2.model_validate(youtube_segment_data()))

    assert telegram.source_kind == "telegram"
    assert telegram.external_key == "telegram:test:phase3"
    assert youtube.source_kind == "youtube"
    assert segment.source_kind == "youtube_segment"
    assert segment.external_key == "youtube:video-1:segment:0"
    assert segment.card_payload["source"]["transcript_text"] == "Native transcript evidence"

    telegram_result = ingest_card(wiki_db, telegram)
    youtube_result = ingest_card(wiki_db, youtube)
    segment_result = ingest_card(wiki_db, segment)
    assert telegram_result.extraction_status == "committed"
    assert youtube_result.extraction_status == "committed"
    assert segment_result.extraction_status == "committed"
    segment_locators = [
        json.loads(row["stable_locator_json"])
        for row in wiki_db.execute(
            """
            SELECT stable_locator_json
            FROM claim_occurrences
            WHERE source_lineage_id = ?
            """,
            (segment.source_lineage_id,),
        )
    ]
    assert all(locator["locator_kind"] == "content_fingerprint" for locator in segment_locators)


def test_overlapping_input_generation_matrix_and_generated_search_exclusion(wiki_db) -> None:
    base = record_ingested_card(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data()),
    )
    lineage_id = base.lineage.source_lineage_id
    assert set(_head_generations(wiki_db, lineage_id)) == {
        CLAIM_INPUT_KIND,
        STRUCTURED_RELATION_INPUT_KIND,
        "card_projection_inputs",
        ELIGIBILITY_INPUT_KIND,
    }
    assert set(_head_generations(wiki_db, lineage_id).values()) == {1}

    generated_only = record_ingested_card(
        wiki_db,
        EnrichedCardV2.model_validate(
            enriched_data(search_text="different generated", graph_text="different graph")
        ),
    )
    assert not generated_only.card_revision.changed
    assert set(_head_generations(wiki_db, lineage_id).values()) == {1}

    summary = record_ingested_card(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data(summary="Changed summary")),
    )
    assert summary.card_revision.changed
    assert _head_generations(wiki_db, lineage_id) == {
        CLAIM_INPUT_KIND: 1,
        STRUCTURED_RELATION_INPUT_KIND: 1,
        "card_projection_inputs": 2,
        ELIGIBILITY_INPUT_KIND: 1,
    }

    record_ingested_card(
        wiki_db,
        EnrichedCardV2.model_validate(
            enriched_data(summary="Changed summary", key_points=[{"text": "Beta"}])
        ),
    )
    assert _head_generations(wiki_db, lineage_id) == {
        CLAIM_INPUT_KIND: 2,
        STRUCTURED_RELATION_INPUT_KIND: 1,
        "card_projection_inputs": 3,
        ELIGIBILITY_INPUT_KIND: 1,
    }

    record_ingested_card(
        wiki_db,
        EnrichedCardV2.model_validate(
            enriched_data(
                summary="Changed summary",
                key_points=[{"text": "Beta"}],
                topics=[{"label": "China"}],
            )
        ),
    )
    assert _head_generations(wiki_db, lineage_id) == {
        CLAIM_INPUT_KIND: 2,
        STRUCTURED_RELATION_INPUT_KIND: 2,
        "card_projection_inputs": 4,
        ELIGIBILITY_INPUT_KIND: 1,
    }

    record_ingested_card(
        wiki_db,
        EnrichedCardV2.model_validate(
            enriched_data(
                summary="Changed summary",
                key_points=[{"text": "Beta"}],
                topics=[{"label": "China"}],
                quality_flags=["extraction_unstable"],
            )
        ),
    )
    assert _head_generations(wiki_db, lineage_id) == {
        CLAIM_INPUT_KIND: 2,
        STRUCTURED_RELATION_INPUT_KIND: 2,
        "card_projection_inputs": 4,
        ELIGIBILITY_INPUT_KIND: 2,
    }


def test_unordered_array_reorder_preserves_hashes_and_occurrence_ids() -> None:
    first_data = enriched_data(
        key_points=[{"text": "Alpha"}, {"text": "Beta"}],
        topics=[{"label": "China"}, {"label": "Drones"}],
        events=[
            {
                "description": "Event",
                "actors": ["B", "A"],
            }
        ],
    )
    second_data = enriched_data(
        key_points=list(reversed(first_data["key_points"])),
        topics=list(reversed(first_data["topics"])),
        events=[
            {
                "description": "Event",
                "actors": ["A", "B"],
            }
        ],
    )
    first = adapt_card(EnrichedCardV2.model_validate(first_data))
    second = adapt_card(EnrichedCardV2.model_validate(second_data))
    assert first.card_revision_id == second.card_revision_id

    first_items = build_occurrence_blueprints(first.input_payloads[CLAIM_INPUT_KIND])
    second_items = build_occurrence_blueprints(second.input_payloads[CLAIM_INPUT_KIND])
    first_ids = {
        occurrence_version_id(
            source_lineage_id=first.source_lineage_id,
            blueprint=item,
        )
        for item in first_items
    }
    second_ids = {
        occurrence_version_id(
            source_lineage_id=second.source_lineage_id,
            blueprint=item,
        )
        for item in second_items
    }
    assert first_ids == second_ids


def test_exact_quotes_only_receive_conservative_normalization() -> None:
    spaced = adapt_card(
        EnrichedCardV2.model_validate(
            enriched_data(quotes=[{"text": "  Alpha  \r\nBeta  "}])
        )
    )
    same = adapt_card(
        EnrichedCardV2.model_validate(
            enriched_data(quotes=[{"text": "Alpha  \nBeta"}])
        )
    )
    collapsed = adapt_card(
        EnrichedCardV2.model_validate(
            enriched_data(quotes=[{"text": "Alpha \nBeta"}])
        )
    )
    spaced_blueprint = next(
        item
        for item in build_occurrence_blueprints(spaced.input_payloads[CLAIM_INPUT_KIND])
        if item.field_kind == "quote"
    )
    same_blueprint = next(
        item
        for item in build_occurrence_blueprints(same.input_payloads[CLAIM_INPUT_KIND])
        if item.field_kind == "quote"
    )
    collapsed_blueprint = next(
        item
        for item in build_occurrence_blueprints(collapsed.input_payloads[CLAIM_INPUT_KIND])
        if item.field_kind == "quote"
    )
    assert spaced_blueprint.exact_payload_hash == same_blueprint.exact_payload_hash
    assert spaced_blueprint.exact_payload_hash != collapsed_blueprint.exact_payload_hash


def test_duplicate_occurrences_remain_distinct_and_stable() -> None:
    first = adapt_card(
        EnrichedCardV2.model_validate(
            enriched_data(key_points=[{"text": "Same"}, {"text": "Same"}, {"text": "Other"}])
        )
    )
    second = adapt_card(
        EnrichedCardV2.model_validate(
            enriched_data(key_points=[{"text": "Other"}, {"text": "Same"}, {"text": "Same"}])
        )
    )
    first_ids = [
        occurrence_version_id(
            source_lineage_id=first.source_lineage_id,
            blueprint=item,
        )
        for item in build_occurrence_blueprints(first.input_payloads[CLAIM_INPUT_KIND])
    ]
    second_ids = [
        occurrence_version_id(
            source_lineage_id=second.source_lineage_id,
            blueprint=item,
        )
        for item in build_occurrence_blueprints(second.input_payloads[CLAIM_INPUT_KIND])
    ]
    assert len(first_ids) == 3
    assert len(set(first_ids)) == 3
    assert set(first_ids) == set(second_ids)


def test_one_changed_key_point_preserves_other_occurrence_identity() -> None:
    before = adapt_card(
        EnrichedCardV2.model_validate(
            enriched_data(key_points=[{"text": "Changes"}, {"text": "Stable"}])
        )
    )
    after = adapt_card(
        EnrichedCardV2.model_validate(
            enriched_data(key_points=[{"text": "Changed"}, {"text": "Stable"}])
        )
    )

    def ids_by_text(adapted) -> dict[str, str]:
        return {
            item.exact_payload["text"]: occurrence_version_id(
                source_lineage_id=adapted.source_lineage_id,
                blueprint=item,
            )
            for item in build_occurrence_blueprints(
                adapted.input_payloads[CLAIM_INPUT_KIND]
            )
        }

    assert ids_by_text(before)["Stable"] == ids_by_text(after)["Stable"]
    assert ids_by_text(before)["Changes"] != ids_by_text(after)["Changed"]


def test_file_loader_validates_whole_array_before_returning(tmp_path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps([enriched_data(), {"schema_version": "unknown"}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_card_file(path)


def test_fresh_databases_produce_same_deterministic_native_ids(tmp_path) -> None:
    identities: list[tuple[str, str, str, str, tuple[str, ...]]] = []
    for ordinal in range(2):
        connection = connect_database(tmp_path / f"fresh-{ordinal}.sqlite")
        try:
            result = ingest_card(
                connection,
                EnrichedCardV2.model_validate(enriched_data()),
            )
            artifact = connection.execute(
                """
                SELECT extraction_artifact_id, extraction_artifact_key
                FROM extraction_artifacts
                """
            ).fetchone()
            identities.append(
                (
                    result.recorded_card.lineage.source_lineage_id,
                    result.recorded_card.card_revision.card_revision_id,
                    artifact["extraction_artifact_id"],
                    artifact["extraction_artifact_key"],
                    tuple(
                        row["occurrence_version_id"]
                        for row in connection.execute(
                            """
                            SELECT occurrence_version_id
                            FROM claim_occurrences
                            ORDER BY occurrence_version_id
                            """
                        )
                    ),
                )
            )
        finally:
            connection.close()
    assert identities[0] == identities[1]
