from __future__ import annotations

from models import EnrichedCardV2
from retrieval.wiki.grouping import group_all_claims
from retrieval.wiki.ingest import ingest_card
from retrieval.wiki.projections import (
    get_projection_artifact,
    rebuild_all_projections,
)
from retrieval.wiki.registry import (
    approve_proposal,
    create_alias_proposal,
    list_proposals,
    scan_registry,
)
from retrieval.wiki.relations import link_all_concepts
from retrieval.wiki.search import resolve_wiki_context, search_wiki
from retrieval.wiki.service import wiki_status
from retrieval.wiki.sidecars import get_manual_sidecar, save_manual_sidecar
from tests.wiki_v2.test_phase3_cards import enriched_data


def _card(
    source_id: str,
    claim: str,
    *,
    entity: str = "Китай",
    entity_role: str = "subject",
    topics: list[dict] | None = None,
    quality_flags: list[str] | None = None,
) -> EnrichedCardV2:
    return EnrichedCardV2.model_validate(
        enriched_data(
            source_id=source_id,
            summary=f"Сводка: {claim}",
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
                        "text": entity,
                        "role": entity_role,
                        "salience": "primary",
                    }
                ]
            },
            topics=topics,
            quality_flags=quality_flags,
        )
    )


def _prepare_approved_wiki(wiki_db) -> str:
    ingest_card(
        wiki_db,
        _card("telegram:projection:1", "Китай испытал новую ракету"),
    )
    ingest_card(
        wiki_db,
        _card("telegram:projection:2", "Китай представил новый беспилотник"),
    )
    scan_registry(wiki_db)
    proposal = list_proposals(wiki_db)[0]
    result = approve_proposal(wiki_db, proposal.proposal_id)
    assert result.concept_id
    alias = create_alias_proposal(
        wiki_db,
        display_surface="КНР",
        target_concept_id=result.concept_id,
        alias_kind="abbreviation",
        rationale="Официальная аббревиатура",
    )
    approve_proposal(wiki_db, alias.proposal_id)
    group_all_claims(wiki_db)
    link_all_concepts(wiki_db)
    return result.concept_id


def test_approved_hub_keeps_claims_and_complete_card_references(
    wiki_db,
    tmp_path,
) -> None:
    concept_id = _prepare_approved_wiki(wiki_db)
    output = tmp_path / "generated"
    sidecars = tmp_path / "sidecars"
    first = rebuild_all_projections(
        wiki_db,
        output_directory=output,
        sidecar_directory=sidecars,
    )
    assert first.cards_built == 2
    assert first.claims_built == 2
    assert first.hubs_built == 1
    assert first.fts_documents == 5

    hub = get_projection_artifact(
        wiki_db,
        projection_kind="hub",
        scope_key=concept_id,
    )
    assert hub is not None
    assert "Китай испытал новую ракету" in hub.rendered_content
    assert "Китай представил новый беспилотник" in hub.rendered_content
    assert "Связанные Enriched-карточки" in hub.rendered_content
    assert hub.rendered_content.count("telegram:projection") >= 2
    assert any(path.name != "README.md" for path in output.glob("*.md"))
    assert len(tuple(sidecars.glob("*.md"))) == 1

    second = rebuild_all_projections(
        wiki_db,
        output_directory=output,
        sidecar_directory=sidecars,
    )
    assert second.cards_built == 0
    assert second.claims_built == 0
    assert second.hubs_built == 0


def test_sidecar_is_authoritative_and_generated_hub_is_disposable(
    wiki_db,
    tmp_path,
) -> None:
    concept_id = _prepare_approved_wiki(wiki_db)
    output = tmp_path / "generated"
    sidecars = tmp_path / "sidecars"
    rebuild_all_projections(
        wiki_db,
        output_directory=output,
        sidecar_directory=sidecars,
    )
    saved = save_manual_sidecar(
        wiki_db,
        concept_id=concept_id,
        markdown_text="Проверенная вручную заметка.",
        directory=sidecars,
    )
    assert saved.changed
    rebuild_all_projections(
        wiki_db,
        output_directory=output,
        sidecar_directory=sidecars,
    )
    hub = get_projection_artifact(
        wiki_db,
        projection_kind="hub",
        scope_key=concept_id,
    )
    assert hub is not None
    assert "Проверенная вручную заметка." in hub.rendered_content

    for path in output.glob("*.md"):
        path.unlink()
    rebuilt = rebuild_all_projections(
        wiki_db,
        output_directory=output,
        sidecar_directory=sidecars,
    )
    assert rebuilt.hub_files_written >= 2
    assert get_manual_sidecar(wiki_db, concept_id).markdown_text == (
        "Проверенная вручную заметка.\n"
    )
    assert any(
        "Проверенная вручную заметка." in path.read_text(encoding="utf-8")
        for path in output.glob("*.md")
    )


def test_fts_resolves_alias_hub_and_preserves_source_references(
    wiki_db,
) -> None:
    concept_id = _prepare_approved_wiki(wiki_db)
    rebuild_all_projections(wiki_db)
    matches = search_wiki(wiki_db, "КНР новая ракета")
    assert matches
    assert any(
        match.document_kind == "hub" and match.scope_key == concept_id
        for match in matches
    )
    context = resolve_wiki_context(wiki_db, "Китай беспилотник")
    assert "Китай представил новый беспилотник" in context.context_text
    assert context.source_refs


def test_unapproved_card_gets_card_fts_but_never_a_hub(wiki_db) -> None:
    ingest_card(
        wiki_db,
        _card("telegram:projection:unapproved", "Арктика меняет логистику", entity="Арктика"),
    )
    group_all_claims(wiki_db)
    link_all_concepts(wiki_db)
    stats = rebuild_all_projections(wiki_db)
    assert stats.cards_built == 1
    assert stats.hubs_built == 0
    assert wiki_db.execute(
        "SELECT COUNT(*) FROM projection_heads WHERE projection_kind = 'hub'"
    ).fetchone()[0] == 0
    assert any(
        match.document_kind == "card"
        for match in search_wiki(wiki_db, "Арктика логистика")
    )


def test_one_card_is_projected_into_every_approved_matching_hub(
    wiki_db,
    tmp_path,
) -> None:
    topic = {
        "label": "Ракетная программа Китая",
        "salience": "primary",
        "type": "military_topic",
    }
    for ordinal, claim in enumerate(
        (
            "Китай испытал новую ракету",
            "Китай представил новый ракетный двигатель",
        ),
        start=1,
    ):
        ingest_card(
            wiki_db,
            _card(
                f"telegram:projection:multi:{ordinal}",
                claim,
                topics=[topic],
            ),
        )
    scan_registry(wiki_db)
    concepts = {}
    for proposal in list_proposals(wiki_db):
        result = approve_proposal(wiki_db, proposal.proposal_id)
        assert result.concept_id
        concepts[proposal.display_label] = result.concept_id
    group_all_claims(wiki_db)
    link_all_concepts(wiki_db)

    output = tmp_path / "generated"
    stats = rebuild_all_projections(
        wiki_db,
        output_directory=output,
        sidecar_directory=tmp_path / "sidecars",
    )
    assert stats.hubs_built == 2
    assert set(concepts) == {"Китай", "Ракетная программа Китая"}
    for concept_id in concepts.values():
        hub = get_projection_artifact(
            wiki_db,
            projection_kind="hub",
            scope_key=concept_id,
        )
        assert hub is not None
        assert "Китай испытал новую ракету" in hub.rendered_content
        assert "Китай представил новый ракетный двигатель" in hub.rendered_content
    assert len(tuple(output.glob("*.md"))) == 3


def test_context_only_material_is_kept_but_collapsed_in_hub(wiki_db) -> None:
    for ordinal, claim in enumerate(
        (
            "Иран упомянут в обзоре переговоров",
            "Иран фигурирует в сравнительном материале",
        ),
        start=1,
    ):
        ingest_card(
            wiki_db,
            _card(
                f"telegram:projection:context:{ordinal}",
                claim,
                entity="Иран",
                entity_role="",
            ),
        )
    scan_registry(wiki_db)
    proposal = list_proposals(wiki_db)[0]
    concept_id = approve_proposal(wiki_db, proposal.proposal_id).concept_id
    assert concept_id
    group_all_claims(wiki_db)
    link_all_concepts(wiki_db)
    rebuild_all_projections(wiki_db)

    hub = get_projection_artifact(
        wiki_db,
        projection_kind="hub",
        scope_key=concept_id,
    )
    assert hub is not None
    assert "Пока нет прямых утверждений" in hub.rendered_content
    assert "Контекстные утверждения и упоминания (2)" in hub.rendered_content
    assert "Контекст и упоминания (2)" in hub.rendered_content


def test_invalid_sidecar_is_reported_without_blocking_projection(
    wiki_db,
    tmp_path,
) -> None:
    _prepare_approved_wiki(wiki_db)
    sidecars = tmp_path / "sidecars"
    sidecars.mkdir()
    (sidecars / "unknown.md").write_text(
        "---\nconcept_id: missing-concept\n---\nНеизвестная заметка.\n",
        encoding="utf-8",
    )

    stats = rebuild_all_projections(
        wiki_db,
        output_directory=tmp_path / "generated",
        sidecar_directory=sidecars,
    )
    assert stats.hubs_built == 1
    assert len(stats.sidecar_errors) == 1
    assert "missing-concept" in stats.sidecar_errors[0]


def test_ineligible_revisions_remove_hub_from_files_status_and_search(
    wiki_db,
    tmp_path,
) -> None:
    concept_id = _prepare_approved_wiki(wiki_db)
    output = tmp_path / "generated"
    rebuild_all_projections(
        wiki_db,
        output_directory=output,
        sidecar_directory=tmp_path / "sidecars",
    )
    assert any(match.document_kind == "hub" for match in search_wiki(wiki_db, "Китай"))

    ingest_card(
        wiki_db,
        _card(
            "telegram:projection:1",
            "Китай испытал новую ракету",
            quality_flags=["extraction_unstable"],
        ),
    )
    ingest_card(
        wiki_db,
        _card(
            "telegram:projection:2",
            "Китай представил новый беспилотник",
            quality_flags=["extraction_unstable"],
        ),
    )
    group_all_claims(wiki_db)
    link_all_concepts(wiki_db)
    rebuilt = rebuild_all_projections(
        wiki_db,
        output_directory=output,
        sidecar_directory=tmp_path / "sidecars",
    )

    assert rebuilt.stale_hub_files_removed == 1
    assert tuple(output.glob("*.md")) == (output / "README.md",)
    assert not any(
        match.document_kind == "hub"
        for match in search_wiki(wiki_db, "Китай")
    )
    assert wiki_status(wiki_db)["projection_hubs"] == 0
    assert get_projection_artifact(
        wiki_db,
        projection_kind="hub",
        scope_key=concept_id,
    ) is not None
