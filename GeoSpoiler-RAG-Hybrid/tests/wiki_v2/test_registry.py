from __future__ import annotations

import pytest

from models import EnrichedCardV2, YouTubeSegmentCardV2
from retrieval.wiki import (
    ProposalStateError,
    approve_proposal,
    create_alias_proposal,
    get_concept,
    ingest_card,
    list_proposals,
    normalize_surface,
    propose_identity_reviews_with_luna,
    publish_registry_dependencies,
    refresh_candidate_snapshot,
    reject_proposal,
    reopen_proposal,
    resolve_surface,
    scan_registry,
    update_concept_display,
)
from tests.wiki_v2.test_phase3_cards import enriched_data, youtube_segment_data
from wiki_reviewer import _proposal_evidence_examples


def _card(
    *,
    source_id: str,
    claim: str,
    entity: str = "Китай",
    category: str = "countries",
) -> EnrichedCardV2:
    entities = {category: [{"text": entity, "role": "subject", "salience": "primary"}]}
    return EnrichedCardV2.model_validate(
        enriched_data(
            source_id=source_id,
            key_points=[{"text": claim, "type": "reported_event", "importance": "high"}],
            entities=entities,
        )
    )


def _youtube_parent(
    *,
    source_id: str,
    parent_source_id: str | None = None,
    claim: str,
    entities: dict | None = None,
    topics: list[dict] | None = None,
) -> EnrichedCardV2:
    payload = enriched_data(
        source_id=source_id,
        content_type="youtube_transcript",
        key_points=[
            {
                "text": claim,
                "type": "reported_event",
                "importance": "high",
            }
        ],
        entities=entities or {},
        topics=topics or [],
    )
    payload["provenance"]["parent_source_id"] = parent_source_id
    return EnrichedCardV2.model_validate(payload)


def _youtube_segment(
    *,
    parent_source_id: str,
    segment_index: int,
    claim: str,
    entities: dict | None = None,
    topics: list[dict] | None = None,
) -> YouTubeSegmentCardV2:
    payload = youtube_segment_data(
        segment_id=f"{parent_source_id}:segment:{segment_index}"
    )
    payload.update(
        {
            "parent_source_id": parent_source_id,
            "video_id": parent_source_id.rsplit(":", 1)[-1],
            "segment_index": segment_index,
            "start_seconds": float(segment_index * 60),
            "end_seconds": float((segment_index + 1) * 60),
            "char_range": [segment_index * 100, (segment_index + 1) * 100],
            "summary": claim,
            "key_points": [{"text": claim}],
            "entities": entities or {},
            "topics": topics or [],
            "quotes": [],
        }
    )
    return YouTubeSegmentCardV2.model_validate(payload)


def test_registry_requires_two_distinct_content_clusters_not_two_reposts(wiki_db) -> None:
    ingest_card(wiki_db, _card(source_id="telegram:china:1", claim="Китай запустил аппарат"))
    first = scan_registry(wiki_db)
    assert first.proposals_created == 0
    assert list_proposals(wiki_db) == ()

    # Different source, byte-equivalent substantive claim: still one cluster.
    ingest_card(wiki_db, _card(source_id="telegram:china:2", claim="Китай запустил аппарат"))
    repost = scan_registry(wiki_db)
    assert repost.proposals_created == 0
    assert list_proposals(wiki_db) == ()

    ingest_card(wiki_db, _card(source_id="telegram:china:3", claim="Китай испытал ракету"))
    distinct = scan_registry(wiki_db)
    assert distinct.proposals_created == 1
    proposal = list_proposals(wiki_db)[0]
    assert proposal.display_label == "Китай"
    assert proposal.cluster_count == 2
    assert proposal.evidence_count == 3


def test_youtube_parent_and_segments_are_one_registry_source_family(
    wiki_db,
) -> None:
    wrapper_source_id = "telegram:family-test"
    parent_source_id = "youtube:family-test"
    mentioned_radar = {
        "equipment": [
            {
                "text": "Radar",
                "role": "background reference",
                "salience": "mentioned",
            }
        ]
    }
    ingest_card(
        wiki_db,
        EnrichedCardV2.model_validate(
            enriched_data(
                source_id=wrapper_source_id,
                key_points=[
                    {
                        "text": "The Telegram wrapper mentions a radar",
                        "type": "reported_event",
                        "importance": "high",
                    }
                ],
                entities=mentioned_radar,
            )
        ),
    )
    ingest_card(
        wiki_db,
        _youtube_parent(
            source_id=parent_source_id,
            parent_source_id=wrapper_source_id,
            claim="The full video mentions a radar",
            entities=mentioned_radar,
        ),
    )
    ingest_card(
        wiki_db,
        _youtube_segment(
            parent_source_id=parent_source_id,
            segment_index=0,
            claim="The first section mentions a radar",
            entities=mentioned_radar,
        ),
    )
    ingest_card(
        wiki_db,
        _youtube_segment(
            parent_source_id=parent_source_id,
            segment_index=1,
            claim="The second section also mentions a radar",
            entities=mentioned_radar,
        ),
    )

    first = scan_registry(wiki_db)
    assert first.proposals_created == 0
    assert list_proposals(wiki_db) == ()

    ingest_card(
        wiki_db,
        _card(
            source_id="telegram:independent-radar",
            claim="A separate report describes a new radar",
            entity="Radar",
            category="equipment",
        ),
    )
    second = scan_registry(wiki_db)
    assert second.proposals_created == 1
    proposal = list_proposals(wiki_db)[0]
    assert proposal.display_label == "Radar"
    assert proposal.payload["qualification"]["rule_id"] == (
        "independent_source_families"
    )
    assert proposal.payload["threshold"]["observed_distinct_source_families"] == 2


def test_one_video_can_propose_multiple_primary_segment_topics_without_mention_flood(
    wiki_db,
) -> None:
    wrapper_source_id = "telegram:multi-topic"
    parent_source_id = "youtube:multi-topic"
    mentioned_beijing = {
        "locations": [
            {
                "text": "Beijing",
                "role": "background reference",
                "salience": "mentioned",
            }
        ]
    }
    ingest_card(
        wiki_db,
        EnrichedCardV2.model_validate(
            enriched_data(
                source_id=wrapper_source_id,
                key_points=[
                    {
                        "text": "The Telegram wrapper previews two subjects",
                        "type": "reported_event",
                        "importance": "high",
                    }
                ],
                entities=mentioned_beijing,
            )
        ),
    )
    ingest_card(
        wiki_db,
        _youtube_parent(
            source_id=parent_source_id,
            parent_source_id=wrapper_source_id,
            claim="The full video covers two separate subjects",
            entities=mentioned_beijing,
        ),
    )
    ingest_card(
        wiki_db,
        _youtube_segment(
            parent_source_id=parent_source_id,
            segment_index=0,
            claim="This section analyzes the drone industry",
            entities=mentioned_beijing,
            topics=[
                {
                    "label": "Chinese drone industry",
                    "type": "technology_topic",
                    "salience": "primary",
                }
            ],
        ),
    )
    ingest_card(
        wiki_db,
        _youtube_segment(
            parent_source_id=parent_source_id,
            segment_index=1,
            claim="This section analyzes the DPRK missile program",
            entities=mentioned_beijing,
            topics=[
                {
                    "label": "DPRK missile program",
                    "type": "military_topic",
                    "salience": "primary",
                }
            ],
        ),
    )

    result = scan_registry(wiki_db)
    assert result.proposals_created == 2
    proposals = list_proposals(wiki_db)
    assert {
        (proposal.proposal_kind, proposal.display_label)
        for proposal in proposals
    } == {
        ("topic", "Chinese drone industry"),
        ("topic", "DPRK missile program"),
    }
    assert {proposal.cluster_count for proposal in proposals} == {1}
    assert {
        proposal.payload["qualification"]["rule_id"]
        for proposal in proposals
    } == {"primary_youtube_segment_topic"}
    assert all(
        proposal.payload["threshold"]["observed_distinct_source_families"] == 1
        for proposal in proposals
    )


def test_media_sources_never_become_wiki_candidates_or_relation_surfaces(
    wiki_db,
) -> None:
    first = ingest_card(
        wiki_db,
        _card(
            source_id="telegram:media-source:1",
            claim="Первое сообщение ссылается на Bloomberg",
            entity="Bloomberg",
            category="media_sources",
        ),
    )
    ingest_card(
        wiki_db,
        _card(
            source_id="telegram:media-source:2",
            claim="Второе независимое сообщение также ссылается на Bloomberg",
            entity="Bloomberg",
            category="media_sources",
        ),
    )

    result = scan_registry(wiki_db)
    snapshot = refresh_candidate_snapshot(
        wiki_db,
        first.recorded_card.lineage.source_lineage_id,
    )

    assert result.proposals_created == 0
    assert result.surfaces_seen == 0
    assert list_proposals(wiki_db) == ()
    assert snapshot.surfaces == ()


def test_approval_is_the_only_path_to_concept_and_historical_reject_can_reopen(
    wiki_db,
) -> None:
    ingest_card(wiki_db, _card(source_id="telegram:china:a", claim="Первый факт"))
    ingest_card(wiki_db, _card(source_id="telegram:china:b", claim="Второй факт"))
    scan_registry(wiki_db)
    proposal = list_proposals(wiki_db)[0]

    assert resolve_surface(wiki_db, "КИТАЙ") == ()
    reject_proposal(wiki_db, proposal.proposal_id, rationale="Слишком широкая сущность")
    assert list_proposals(wiki_db) == ()
    assert list_proposals(wiki_db, statuses=("rejected",))[0].status == "rejected"

    reopen_proposal(wiki_db, proposal.proposal_id, rationale="Проверено вручную")
    approved = approve_proposal(
        wiki_db,
        proposal.proposal_id,
        canonical_label="Китай",
        description="Китайская Народная Республика",
    )
    assert approved.concept_id is not None
    concept = get_concept(wiki_db, approved.concept_id)
    assert concept.canonical_label == "Китай"
    assert resolve_surface(wiki_db, "китай")[0].concept_id == concept.concept_id
    with pytest.raises(ProposalStateError):
        approve_proposal(wiki_db, proposal.proposal_id)

    edited = update_concept_display(
        wiki_db,
        concept.concept_id,
        canonical_label="Китайская Народная Республика",
    )
    assert edited.generation == 2
    assert {item.concept_id for item in resolve_surface(wiki_db, "Китай")} == {
        concept.concept_id
    }
    assert {item.concept_id for item in resolve_surface(
        wiki_db, "Китайская Народная Республика"
    )} == {concept.concept_id}


def test_semantic_alias_requires_separate_approval_and_metonym_kind_is_impossible(
    wiki_db,
) -> None:
    ingest_card(
        wiki_db,
        _card(
            source_id="telegram:dprk:a",
            claim="Северная Корея провела запуск",
            entity="Северная Корея",
        ),
    )
    ingest_card(
        wiki_db,
        _card(
            source_id="telegram:dprk:b",
            claim="Северная Корея сообщила об испытании",
            entity="Северная Корея",
        ),
    )
    scan_registry(wiki_db)
    base = list_proposals(wiki_db)[0]
    concept_id = approve_proposal(wiki_db, base.proposal_id).concept_id
    assert concept_id

    alias = create_alias_proposal(
        wiki_db,
        display_surface="КНДР",
        target_concept_id=concept_id,
        alias_kind="abbreviation",
        rationale="Общепринятая аббревиатура",
    )
    assert resolve_surface(wiki_db, "КНДР") == ()
    approve_proposal(wiki_db, alias.proposal_id)
    assert resolve_surface(wiki_db, "КНДР")[0].concept_id == concept_id

    with pytest.raises(ValueError, match="Unsupported identity alias kind"):
        create_alias_proposal(
            wiki_db,
            display_surface="Пхеньян",
            target_concept_id=concept_id,
            alias_kind="metonym",  # type: ignore[arg-type]
            rationale="Контекстная метонимия не является identity alias",
        )


def test_luna_identity_group_remains_review_only_then_creates_one_hub(
    wiki_db,
    monkeypatch,
) -> None:
    for ordinal, (surface, claim) in enumerate(
        (
            ("Северная Корея", "Северная Корея провела запуск"),
            ("Северная Корея", "Северная Корея сообщила об учениях"),
            ("КНДР", "КНДР испытала ракету"),
            ("КНДР", "КНДР объявила о решении"),
        )
    ):
        ingest_card(
            wiki_db,
            _card(
                source_id=f"telegram:identity:{ordinal}",
                claim=claim,
                entity=surface,
            ),
        )
    scan_registry(wiki_db)
    candidates = list_proposals(wiki_db)
    by_label = {proposal.display_label: proposal for proposal in candidates}
    north = by_label["Северная Корея"]
    dprk = by_label["КНДР"]

    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.is_luna_role",
        lambda role: role == "default",
    )
    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.active_model_for",
        lambda role: "codex-cli:gpt-5.6-luna@xhigh",
    )
    resolver_calls = 0

    def resolve_identity(*args, **kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return {
            "identity_groups": [
                {
                    "members": [
                        {
                            "proposal_id": north.proposal_id,
                            "alias_kind": "translation",
                        },
                        {
                            "proposal_id": dprk.proposal_id,
                            "alias_kind": "abbreviation",
                        },
                    ],
                    "canonical_member_proposal_id": north.proposal_id,
                    "canonical_label": "Северная Корея",
                    "reason": "КНДР — официальная русская аббревиатура того же государства",
                }
            ],
            "aliases_to_existing": [],
            "canonicalizations": [],
        }

    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.complete_json_sync",
        resolve_identity,
    )
    analysis = propose_identity_reviews_with_luna(wiki_db)
    assert analysis.identity_groups_created == 1
    repeated = propose_identity_reviews_with_luna(wiki_db)
    assert repeated.cache_hit
    assert resolver_calls == 1
    assert resolve_surface(wiki_db, "КНДР") == ()

    merge = next(
        proposal
        for proposal in list_proposals(wiki_db)
        if proposal.proposal_kind == "merge"
    )
    concept_id = approve_proposal(wiki_db, merge.proposal_id).concept_id
    assert concept_id
    assert resolve_surface(wiki_db, "Северная Корея")[0].concept_id == concept_id
    assert resolve_surface(wiki_db, "КНДР")[0].concept_id == concept_id
    assert {
        proposal.status
        for proposal in list_proposals(wiki_db, statuses=("approved",))
        if proposal.proposal_id in {north.proposal_id, dprk.proposal_id, merge.proposal_id}
    } == {"approved"}


def test_luna_groups_inflected_forms_with_claim_context_for_review(
    wiki_db,
    monkeypatch,
) -> None:
    examples = (
        ("Европа", "Европа обсуждает новую систему безопасности"),
        ("Европа", "Европа увеличила расходы на инфраструктуру"),
        ("Европе", "В Европе открыли новый исследовательский центр"),
        ("Европе", "Европе предложили новый формат переговоров"),
        ("Европы", "Страны Европы согласовали совместное заявление"),
        ("Европы", "Экономика Европы показала рост"),
    )
    for ordinal, (surface, claim) in enumerate(examples):
        ingest_card(
            wiki_db,
            _card(
                source_id=f"telegram:europe-form:{ordinal}",
                claim=claim,
                entity=surface,
                category="locations",
            ),
        )
    scan_registry(wiki_db)
    candidates = list_proposals(wiki_db)
    by_label = {proposal.display_label: proposal for proposal in candidates}
    assert set(by_label) == {"Европа", "Европе", "Европы"}

    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.is_luna_role",
        lambda role: role == "default",
    )
    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.active_model_for",
        lambda role: "codex-cli:gpt-5.6-luna@xhigh",
    )
    captured_prompt = ""

    def resolve_identity(messages, **kwargs):
        nonlocal captured_prompt
        captured_prompt = messages[-1]["content"]
        return {
            "identity_groups": [
                {
                    "members": [
                        {
                            "proposal_id": by_label[label].proposal_id,
                            "alias_kind": "technical",
                        }
                        for label in ("Европа", "Европе", "Европы")
                    ],
                    "canonical_member_proposal_id": by_label["Европа"].proposal_id,
                    "canonical_label": "Европа",
                    "reason": (
                        "Это именительный и падежные формы одного географического "
                        "concept, что подтверждается контекстом claims."
                    ),
                }
            ],
            "aliases_to_existing": [],
            "canonicalizations": [],
        }

    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.complete_json_sync",
        resolve_identity,
    )
    analysis = propose_identity_reviews_with_luna(wiki_db)

    assert analysis.identity_groups_created == 1
    assert '"evidence_examples"' in captured_prompt
    assert "В Европе открыли новый исследовательский центр" in captured_prompt
    assert "canonical_label всегда пиши в нормальной словарной форме" in captured_prompt

    merge = next(
        proposal
        for proposal in list_proposals(wiki_db)
        if proposal.proposal_kind == "merge"
    )
    assert [
        item["display_label"] for item in merge.payload["member_surfaces"]
    ] == ["Европа", "Европе", "Европы"]
    evidence = _proposal_evidence_examples(wiki_db, merge, limit=10)
    assert {item["matched_surface"] for item in evidence} == {
        "Европа",
        "Европе",
        "Европы",
    }

    concept_id = approve_proposal(wiki_db, merge.proposal_id).concept_id
    assert concept_id
    assert {
        resolve_surface(wiki_db, surface)[0].concept_id
        for surface in ("Европа", "Европе", "Европы")
    } == {concept_id}
    alias_rows = wiki_db.execute(
        """
        SELECT display_surface, alias_kind
        FROM identity_aliases
        WHERE concept_id = ?
        ORDER BY display_surface
        """,
        (concept_id,),
    ).fetchall()
    assert {(row["display_surface"], row["alias_kind"]) for row in alias_rows} == {
        ("Европа", "canonical"),
        ("Европе", "technical"),
        ("Европы", "technical"),
    }


def test_luna_identity_group_cannot_cross_entity_categories(
    wiki_db,
    monkeypatch,
) -> None:
    for category in ("countries", "locations"):
        for variant in range(2):
            ingest_card(
                wiki_db,
                _card(
                    source_id=f"telegram:georgia:{category}:{variant}",
                    claim=f"Грузия: контекст {category} {variant}",
                    entity="Грузия",
                    category=category,
                ),
            )
    scan_registry(wiki_db)
    candidates = list_proposals(wiki_db)
    by_category = {proposal.source_category: proposal for proposal in candidates}
    assert set(by_category) == {"countries", "locations"}

    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.is_luna_role",
        lambda role: role == "default",
    )
    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.active_model_for",
        lambda role: "codex-cli:gpt-5.6-luna@xhigh",
    )
    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.complete_json_sync",
        lambda *args, **kwargs: {
            "identity_groups": [
                {
                    "members": [
                        {
                            "proposal_id": proposal.proposal_id,
                            "alias_kind": "technical",
                        }
                        for proposal in by_category.values()
                    ],
                    "canonical_member_proposal_id": by_category[
                        "countries"
                    ].proposal_id,
                    "canonical_label": "Грузия",
                    "reason": "Одинаковая строка",
                }
            ],
            "aliases_to_existing": [],
            "canonicalizations": [],
        },
    )

    analysis = propose_identity_reviews_with_luna(wiki_db)

    assert analysis.identity_groups_created == 0
    assert analysis.skipped_suggestions == 1
    assert all(
        proposal.proposal_kind != "merge"
        for proposal in list_proposals(wiki_db)
    )


def test_luna_proposes_dictionary_form_for_single_inflected_candidate(
    wiki_db,
    monkeypatch,
) -> None:
    for ordinal, claim in enumerate(
        (
            "С Украиной обсудили новый формат переговоров",
            "Украиной были предложены дополнительные гарантии",
        )
    ):
        ingest_card(
            wiki_db,
            _card(
                source_id=f"telegram:ukraine-form:{ordinal}",
                claim=claim,
                entity="Украиной",
                category="countries",
            ),
        )
    scan_registry(wiki_db)
    source_proposal = list_proposals(wiki_db)[0]
    assert source_proposal.display_label == "Украиной"

    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.is_luna_role",
        lambda role: role == "default",
    )
    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.active_model_for",
        lambda role: "codex-cli:gpt-5.6-luna@xhigh",
    )
    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.complete_json_sync",
        lambda *args, **kwargs: {
            "identity_groups": [],
            "aliases_to_existing": [],
            "canonicalizations": [
                {
                    "proposal_id": source_proposal.proposal_id,
                    "canonical_label": "Украина",
                    "alias_kind": "technical",
                    "reason": (
                        "«Украиной» — творительный падеж названия страны "
                        "«Украина»."
                    ),
                }
            ],
        },
    )

    analysis = propose_identity_reviews_with_luna(wiki_db)

    assert analysis.identity_groups_created == 0
    assert analysis.canonicalizations_created == 1
    normalization = next(
        proposal
        for proposal in list_proposals(wiki_db)
        if proposal.proposal_kind == "merge"
    )
    assert normalization.display_label == "Украина"
    assert normalization.payload["identity_review_kind"] == "canonicalization"
    assert normalization.payload["canonical_surface_alias_kind"] == "technical"
    assert normalization.payload["member_proposal_ids"] == [
        source_proposal.proposal_id
    ]

    concept_id = approve_proposal(wiki_db, normalization.proposal_id).concept_id
    assert concept_id
    assert resolve_surface(wiki_db, "Украина")[0].concept_id == concept_id
    assert resolve_surface(wiki_db, "Украиной")[0].concept_id == concept_id
    alias_rows = wiki_db.execute(
        """
        SELECT display_surface, alias_kind
        FROM identity_aliases
        WHERE concept_id = ?
        """,
        (concept_id,),
    ).fetchall()
    assert {(row["display_surface"], row["alias_kind"]) for row in alias_rows} == {
        ("Украина", "canonical"),
        ("Украиной", "technical"),
    }


def test_registry_dependency_snapshots_are_narrow_and_idempotent(wiki_db) -> None:
    ingest_card(wiki_db, _card(source_id="telegram:deps:a", claim="Первый факт"))
    ingest_card(wiki_db, _card(source_id="telegram:deps:b", claim="Второй факт"))
    scan_registry(wiki_db)
    proposal = list_proposals(wiki_db)[0]
    concept_id = approve_proposal(wiki_db, proposal.proposal_id).concept_id
    assert concept_id

    first = publish_registry_dependencies(wiki_db)
    assert first.concept_snapshots_changed == 1
    assert first.display_snapshots_changed == 1
    assert first.alias_snapshots_changed == 1
    assert first.surface_snapshots_changed >= 1
    second = publish_registry_dependencies(wiki_db)
    assert second.concept_snapshots_changed == 0
    assert second.display_snapshots_changed == 0
    assert second.alias_snapshots_changed == 0
    assert second.surface_snapshots_changed == 0
    assert wiki_db.execute(
        """
        SELECT 1
        FROM dependency_heads
        WHERE dependency_kind = 'surface_resolution'
          AND dependency_scope_key = 'китай'
        """
    ).fetchone()


def test_ambiguous_split_is_proposed_only_after_second_concept_has_content(
    wiki_db,
) -> None:
    ingest_card(
        wiki_db,
        _card(
            source_id="telegram:georgia:country:1",
            claim="Грузия провела выборы",
            entity="Грузия",
            category="countries",
        ),
    )
    ingest_card(
        wiki_db,
        _card(
            source_id="telegram:georgia:country:2",
            claim="Грузия изменила закон",
            entity="Грузия",
            category="countries",
        ),
    )
    scan_registry(wiki_db)
    country = list_proposals(wiki_db)[0]
    country_id = approve_proposal(wiki_db, country.proposal_id).concept_id
    assert country_id

    ingest_card(
        wiki_db,
        _card(
            source_id="telegram:georgia:state:1",
            claim="В штате Джорджия открыли завод",
            entity="Грузия",
            category="locations",
        ),
    )
    scan_registry(wiki_db)
    assert not [
        proposal
        for proposal in list_proposals(wiki_db)
        if proposal.proposal_kind == "split"
    ]

    ingest_card(
        wiki_db,
        _card(
            source_id="telegram:georgia:state:2",
            claim="В штате Джорджия объявили чрезвычайное положение",
            entity="Грузия",
            category="locations",
        ),
    )
    scan_registry(wiki_db)
    split = next(
        proposal
        for proposal in list_proposals(wiki_db)
        if proposal.proposal_kind == "split"
    )
    state_id = approve_proposal(
        wiki_db,
        split.proposal_id,
        canonical_label="Джорджия (штат США)",
    ).concept_id
    assert state_id and state_id != country_id
    assert {concept.concept_id for concept in resolve_surface(wiki_db, "Грузия")} == {
        country_id,
        state_id,
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  КИТАЙ. ", "китай"),
        ("D.P.R.K.", "dprk"),
        ("Северная—Корея", "северная-корея"),
    ],
)
def test_surface_normalization_is_technical_only(raw: str, expected: str) -> None:
    assert normalize_surface(raw) == expected
