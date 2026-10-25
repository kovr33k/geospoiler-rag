from __future__ import annotations

import json

import pytest

import config
import retrieval.wiki.service as wiki_service
from models import EnrichedCardV2, YouTubeSegmentCardV2
from retrieval.wiki.projections import rebuild_all_projections
from retrieval.wiki.registry import (
    approve_proposal,
    create_alias_proposal,
    list_proposals,
)
from retrieval.wiki.schema import connect_database
from retrieval.wiki.search import search_wiki
from retrieval.wiki.service import (
    configured_input_paths,
    get_wiki_review_counts,
    refresh_wiki_after_review,
    run_configured_wiki_pipeline,
    run_wiki_pipeline,
    wiki_status,
)
from retrieval.wiki.sidecars import save_manual_sidecar
from tests.wiki_v2.test_phase3_cards import enriched_data, youtube_segment_data


def _write_card(path, source_id: str, claim: str) -> None:
    card = EnrichedCardV2.model_validate(
        enriched_data(
            source_id=source_id,
            summary=claim,
            key_points=[
                {
                    "text": claim,
                    "type": "reported_event",
                    "importance": "high",
                }
            ],
            entities={
                "countries": [
                    {
                        "text": "Китай",
                        "role": "subject",
                        "salience": "primary",
                    }
                ]
            },
        )
    )
    path.write_text(
        json.dumps(card.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )


def _write_youtube_card(path, segment_id: str, claim: str) -> None:
    payload = youtube_segment_data(segment_id=segment_id)
    payload["summary"] = claim
    payload["key_points"] = [{"text": claim}]
    payload["entities"] = {
        "countries": [
            {
                "text": "Китай",
                "role": "subject",
                "salience": "primary",
            }
        ]
    }
    card = YouTubeSegmentCardV2.model_validate(payload)
    path.write_text(
        json.dumps(card.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )


def test_master_switch_blocks_configured_wiki_without_opening_database(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "WIKI_ENABLED", False)
    monkeypatch.setattr(
        wiki_service,
        "connect_database",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Wiki database must not be opened")
        ),
    )

    assert configured_input_paths() == ()
    assert get_wiki_review_counts().total == 0
    assert wiki_status()["fts_documents"] == 0
    with pytest.raises(RuntimeError, match="Wiki disabled"):
        run_configured_wiki_pipeline()


def test_pipeline_proposes_then_review_approval_materializes_hub(
    wiki_db,
    tmp_path,
) -> None:
    cards = tmp_path / "cards"
    cards.mkdir()
    _write_card(cards / "one.enriched.json", "telegram:service:1", "Китай испытал ракету")
    _write_card(cards / "two.enriched.json", "telegram:service:2", "Китай показал дрон")
    output = tmp_path / "wiki"
    sidecars = tmp_path / "sidecars"

    first = run_wiki_pipeline(
        wiki_db,
        input_paths=(cards,),
        output_directory=output,
        sidecar_directory=sidecars,
        use_luna=False,
    )
    assert first.registry.proposals_created == 1
    assert first.review_counts.concepts == 1
    assert first.projections.hubs_built == 0

    proposal = list_proposals(wiki_db)[0]
    approved = approve_proposal(wiki_db, proposal.proposal_id)
    assert approved.concept_id
    refresh_wiki_after_review(
        wiki_db,
        output_directory=output,
        sidecar_directory=sidecars,
        use_luna=False,
    )
    assert get_wiki_review_counts(wiki_db).concepts == 0
    status = wiki_status(wiki_db)
    assert status["approved_concepts"] == 1
    assert status["projection_hubs"] == 1
    assert status["fts_documents"] == 5


def test_file_database_end_to_end_survives_projection_deletion_and_reopen(
    tmp_path,
) -> None:
    cards = tmp_path / "cards"
    cards.mkdir()
    _write_card(
        cards / "telegram.enriched.json",
        "telegram:e2e:1",
        "Китай испытал ракету",
    )
    _write_youtube_card(
        cards / "video.youtube-segment.json",
        "youtube:e2e:segment:0",
        "Китай представил беспилотник",
    )
    database = tmp_path / "wiki.sqlite"
    output = tmp_path / "generated"
    sidecars = tmp_path / "sidecars"
    connection = connect_database(database)
    first = run_wiki_pipeline(
        connection,
        input_paths=(cards,),
        output_directory=output,
        sidecar_directory=sidecars,
        use_luna=False,
        database_path=database,
    )
    assert first.review_counts.concepts == 1
    proposal = list_proposals(connection)[0]
    approved = approve_proposal(connection, proposal.proposal_id)
    assert approved.concept_id
    alias = create_alias_proposal(
        connection,
        display_surface="КНР",
        target_concept_id=approved.concept_id,
        alias_kind="abbreviation",
        rationale="Одобренная техническая эквивалентность",
    )
    approve_proposal(connection, alias.proposal_id)
    save_manual_sidecar(
        connection,
        concept_id=approved.concept_id,
        markdown_text="Проверенная вручную справка.",
        directory=sidecars,
    )
    refresh_wiki_after_review(
        connection,
        output_directory=output,
        sidecar_directory=sidecars,
        use_luna=False,
    )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()

    for generated in output.glob("*.md"):
        generated.unlink()

    reopened = connect_database(database)
    try:
        rebuilt = rebuild_all_projections(
            reopened,
            output_directory=output,
            sidecar_directory=sidecars,
        )
        assert rebuilt.hub_files_written == 2
        matches = search_wiki(
            reopened,
            "КНР ракета беспилотник",
            document_kinds=("hub",),
        )
        assert len(matches) == 1
        assert matches[0].scope_key == approved.concept_id
        assert isinstance(matches[0].source_refs, dict)
        source_ids = {
            source["source_id"]
            for source in matches[0].source_refs["sources"]
        }
        assert source_ids == {
            "telegram:e2e:1",
            "youtube:e2e:segment:0",
        }
        hub_files = [
            path
            for path in output.glob("*.md")
            if path.name != "README.md"
        ]
        assert len(hub_files) == 1
        assert "Проверенная вручную справка." in hub_files[0].read_text(
            encoding="utf-8"
        )
        assert len(tuple(sidecars.glob("*.md"))) == 1
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()
