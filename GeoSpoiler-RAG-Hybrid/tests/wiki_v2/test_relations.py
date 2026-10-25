from __future__ import annotations

import pytest

import retrieval.wiki.relations as relation_module
from models import EnrichedCardV2
from retrieval.wiki import (
    approve_proposal,
    ingest_card,
    link_all_concepts,
    link_lineage_concepts,
    list_pending_ambiguities,
    list_proposals,
    resolve_ambiguity,
    scan_registry,
)
from tests.wiki_v2.test_phase3_cards import enriched_data


def _card(
    source_id: str,
    claim: str,
    *,
    surface: str,
    category: str = "countries",
    role: str = "subject",
    salience: str = "primary",
    topics: list[dict] | None = None,
    quality_flags: list[str] | None = None,
) -> EnrichedCardV2:
    return EnrichedCardV2.model_validate(
        enriched_data(
            source_id=source_id,
            key_points=[
                {
                    "text": claim,
                    "type": "reported_event",
                    "importance": "high",
                }
            ],
            entities={
                category: [
                    {
                        "text": surface,
                        "role": role,
                        "salience": salience,
                    }
                ]
            },
            topics=topics,
            quality_flags=quality_flags,
        )
    )


def _approve_all_pending(wiki_db) -> dict[str, str]:
    approved: dict[str, str] = {}
    for proposal in list_proposals(wiki_db):
        result = approve_proposal(wiki_db, proposal.proposal_id)
        assert result.concept_id
        approved[proposal.display_label] = result.concept_id
    return approved


def test_subject_links_are_direct_and_primary_entity_without_role_is_context(
    wiki_db,
) -> None:
    direct_a = ingest_card(
        wiki_db,
        _card(
            "telegram:relation:china:1",
            "Китай провёл запуск",
            surface="Китай",
        ),
    )
    ingest_card(
        wiki_db,
        _card(
            "telegram:relation:china:2",
            "Китай сообщил об испытании",
            surface="Китай",
        ),
    )
    context_a = ingest_card(
        wiki_db,
        _card(
            "telegram:relation:iran:1",
            "Иран упомянут в материале",
            surface="Иран",
            role="",
        ),
    )
    ingest_card(
        wiki_db,
        _card(
            "telegram:relation:iran:2",
            "Иран фигурирует во втором сообщении",
            surface="Иран",
            role="",
        ),
    )
    scan_registry(wiki_db)
    concepts = _approve_all_pending(wiki_db)
    stats = link_all_concepts(wiki_db)
    assert stats.active_links >= 4

    china_relation = wiki_db.execute(
        """
        SELECT relation_kind, strongest_relation_role
        FROM effective_card_relations
        WHERE card_revision_id = ? AND concept_id = ?
        """,
        (
            direct_a.recorded_card.card_revision.card_revision_id,
            concepts["Китай"],
        ),
    ).fetchone()
    assert tuple(china_relation) == ("direct", "subject")

    iran_relation = wiki_db.execute(
        """
        SELECT relation_kind, strongest_relation_role
        FROM effective_card_relations
        WHERE card_revision_id = ? AND concept_id = ?
        """,
        (
            context_a.recorded_card.card_revision.card_revision_id,
            concepts["Иран"],
        ),
    ).fetchone()
    assert tuple(iran_relation) == ("context", "context")


def test_primary_topic_is_direct_and_one_card_can_belong_to_two_hubs(wiki_db) -> None:
    topics = [
        {
            "label": "Ракетная программа Китая",
            "salience": "primary",
            "type": "military_topic",
        }
    ]
    first = ingest_card(
        wiki_db,
        _card(
            "telegram:relation:multi:1",
            "Китай испытал новый носитель",
            surface="Китай",
            topics=topics,
        ),
    )
    ingest_card(
        wiki_db,
        _card(
            "telegram:relation:multi:2",
            "Китай представил другую ракету",
            surface="Китай",
            topics=topics,
        ),
    )
    scan_registry(wiki_db)
    concepts = _approve_all_pending(wiki_db)
    link_all_concepts(wiki_db)
    rows = wiki_db.execute(
        """
        SELECT concept_id, relation_kind
        FROM effective_card_relations
        WHERE card_revision_id = ?
        ORDER BY concept_id
        """,
        (first.recorded_card.card_revision.card_revision_id,),
    ).fetchall()
    assert {row["concept_id"] for row in rows} == {
        concepts["Китай"],
        concepts["Ракетная программа Китая"],
    }
    assert {row["relation_kind"] for row in rows} == {"direct"}


def test_removed_structured_surface_writes_absent_tombstone(wiki_db) -> None:
    original = _card(
        "telegram:relation:remove",
        "Китай провёл запуск",
        surface="Китай",
    )
    ingest_card(wiki_db, original)
    ingest_card(
        wiki_db,
        _card(
            "telegram:relation:remove:evidence",
            "Китай сообщил о втором запуске",
            surface="Китай",
        ),
    )
    scan_registry(wiki_db)
    concepts = _approve_all_pending(wiki_db)
    link_all_concepts(wiki_db)
    assert wiki_db.execute(
        """
        SELECT COUNT(*)
        FROM effective_occurrence_concept_links
        WHERE concept_id = ?
        """,
        (concepts["Китай"],),
    ).fetchone()[0] == 2

    changed = EnrichedCardV2.model_validate(
        enriched_data(
            source_id="telegram:relation:remove",
            key_points=[
                {
                    "text": "Китай провёл запуск",
                    "type": "reported_event",
                    "importance": "high",
                }
            ],
            entities={},
        )
    )
    changed_result = ingest_card(wiki_db, changed)
    scan_registry(wiki_db)
    result = link_lineage_concepts(
        wiki_db,
        changed_result.recorded_card.lineage.source_lineage_id,
    )
    assert result.absent_links == 1
    assert wiki_db.execute(
        """
        SELECT COUNT(*)
        FROM effective_occurrence_concept_links AS link
        JOIN claim_occurrences AS occurrence
          ON occurrence.occurrence_version_id = link.occurrence_version_id
        WHERE occurrence.source_lineage_id = ?
        """,
        (changed_result.recorded_card.lineage.source_lineage_id,),
    ).fetchone()[0] == 0


def test_same_surface_two_approved_concepts_goes_to_ambiguity_review(wiki_db) -> None:
    for ordinal, (category, claim) in enumerate(
        (
            ("countries", "Грузия провела выборы"),
            ("countries", "Грузия приняла закон"),
        )
    ):
        ingest_card(
            wiki_db,
            _card(
                f"telegram:relation:georgia:country:{ordinal}",
                claim,
                surface="Грузия",
                category=category,
            ),
        )
    scan_registry(wiki_db)
    country_id = approve_proposal(
        wiki_db, list_proposals(wiki_db)[0].proposal_id
    ).concept_id
    assert country_id

    for ordinal, claim in enumerate(
        (
            "В штате Джорджия открыли завод",
            "В штате Джорджия прошли слушания",
        )
    ):
        ingest_card(
            wiki_db,
            _card(
                f"telegram:relation:georgia:state:{ordinal}",
                claim,
                surface="Грузия",
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
    assert state_id

    ambiguous = ingest_card(
        wiki_db,
        _card(
            "telegram:relation:georgia:ambiguous",
            "Грузия сообщила о новом решении",
            surface="Грузия",
            category="other",
            role="",
        ),
    )
    scan_registry(wiki_db)
    link_all_concepts(wiki_db)
    pending = [
        item
        for item in list_pending_ambiguities(wiki_db)
        if item["occurrence_version_id"]
        in {
            row["occurrence_version_id"]
            for row in wiki_db.execute(
                """
                SELECT occurrence_version_id
                FROM effective_active_occurrences
                WHERE source_lineage_id = ?
                """,
                (ambiguous.recorded_card.lineage.source_lineage_id,),
            )
        }
    ]
    assert len(pending) == 1
    assert {candidate["concept_id"] for candidate in pending[0]["candidates"]} == {
        country_id,
        state_id,
    }
    resolve_ambiguity(
        wiki_db,
        occurrence_version_id=pending[0]["occurrence_version_id"],
        normalized_surface="Грузия",
        selected_concept_id=country_id,
        relation_role="context",
        rationale="В этом claim речь о стране",
    )
    effective = wiki_db.execute(
        """
        SELECT concept_id, relation_role, link_source
        FROM effective_occurrence_concept_links
        WHERE occurrence_version_id = ?
        """,
        (pending[0]["occurrence_version_id"],),
    ).fetchall()
    assert [tuple(row) for row in effective] == [
        (country_id, "context", "override")
    ]
    current_card_id = ambiguous.recorded_card.card_revision.card_revision_id
    relation = wiki_db.execute(
        """
        SELECT concept_id, relation_kind
        FROM effective_card_relations
        WHERE card_revision_id = ?
        """,
        (current_card_id,),
    ).fetchone()
    assert tuple(relation) == (country_id, "context")

    resolve_ambiguity(
        wiki_db,
        occurrence_version_id=pending[0]["occurrence_version_id"],
        normalized_surface="Грузия",
        selected_concept_id=None,
        rationale="Недостаточно контекста для выбора",
    )
    assert wiki_db.execute(
        """
        SELECT COUNT(*)
        FROM effective_occurrence_concept_links
        WHERE occurrence_version_id = ?
        """,
        (pending[0]["occurrence_version_id"],),
    ).fetchone()[0] == 0
    assert wiki_db.execute(
        """
        SELECT COUNT(*)
        FROM effective_card_relations
        WHERE card_revision_id = ?
        """,
        (current_card_id,),
    ).fetchone()[0] == 0


def test_luna_can_refine_unclear_role_but_cannot_create_an_alias(
    wiki_db,
    monkeypatch,
) -> None:
    first = ingest_card(
        wiki_db,
        _card(
            "telegram:relation:luna:1",
            "Иран заявил о новом решении",
            surface="Иран",
            role="",
        ),
    )
    ingest_card(
        wiki_db,
        _card(
            "telegram:relation:luna:2",
            "Иран сообщил о переговорах",
            surface="Иран",
            role="",
        ),
    )
    scan_registry(wiki_db)
    concept_id = _approve_all_pending(wiki_db)["Иран"]
    occurrence_id = wiki_db.execute(
        """
        SELECT occurrence_version_id
        FROM effective_active_occurrences
        WHERE source_lineage_id = ?
        """,
        (first.recorded_card.lineage.source_lineage_id,),
    ).fetchone()[0]
    monkeypatch.setattr(
        "llm_backend.is_luna_role",
        lambda role: role == "default",
    )
    monkeypatch.setattr(
        "llm_backend.active_model_for",
        lambda role: "codex-cli:gpt-5.6-luna@xhigh",
    )
    resolver_calls = 0

    def resolve_role(*args, **kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return {
            "resolutions": [
                {
                    "occurrence_version_id": occurrence_id,
                    "concept_id": concept_id,
                    "relation_role": "actor",
                    "explanation": "Иран является говорящим актором в claim",
                }
            ]
        }

    monkeypatch.setattr(
        "llm_backend.complete_json_sync",
        resolve_role,
    )
    result = link_lineage_concepts(
        wiki_db,
        first.recorded_card.lineage.source_lineage_id,
        use_luna=True,
    )
    assert result.resolver_error is None
    relation = wiki_db.execute(
        """
        SELECT relation_kind, strongest_relation_role
        FROM effective_card_relations
        WHERE card_revision_id = ? AND concept_id = ?
        """,
        (first.recorded_card.card_revision.card_revision_id, concept_id),
    ).fetchone()
    assert tuple(relation) == ("direct", "actor")
    automatic = wiki_db.execute(
        """
        SELECT resolver_version, rule_id
        FROM occurrence_concept_automatic_links
        WHERE occurrence_version_id = ? AND concept_id = ?
        ORDER BY automatic_generation DESC
        LIMIT 1
        """,
        (occurrence_id, concept_id),
    ).fetchone()
    assert automatic["resolver_version"].startswith("codex-cli:gpt-5.6-luna")
    assert automatic["rule_id"] == "luna_relation_role"
    assert wiki_db.execute(
        """
        SELECT COUNT(*)
        FROM identity_aliases
        WHERE concept_id = ?
        """,
        (concept_id,),
    ).fetchone()[0] == 1

    repeated = link_lineage_concepts(
        wiki_db,
        first.recorded_card.lineage.source_lineage_id,
        use_luna=True,
    )
    assert repeated.status == "no_op"
    assert resolver_calls == 1

    summary_only = ingest_card(
        wiki_db,
        EnrichedCardV2.model_validate(
            enriched_data(
                source_id="telegram:relation:luna:1",
                summary="Обновлённое краткое описание",
                key_points=[
                    {
                        "text": "Иран заявил о новом решении",
                        "type": "reported_event",
                        "importance": "high",
                    }
                ],
                entities={
                    "countries": [
                        {
                            "text": "Иран",
                            "role": "",
                            "salience": "primary",
                        }
                    ]
                },
            )
        ),
    )
    summary_refresh = link_lineage_concepts(
        wiki_db,
        first.recorded_card.lineage.source_lineage_id,
        use_luna=True,
    )
    assert summary_refresh.status == "no_op"
    assert resolver_calls == 1
    refreshed_relation = wiki_db.execute(
        """
        SELECT relation_kind, strongest_relation_role
        FROM effective_card_relations
        WHERE card_revision_id = ? AND concept_id = ?
        """,
        (
            summary_only.recorded_card.card_revision.card_revision_id,
            concept_id,
        ),
    ).fetchone()
    assert tuple(refreshed_relation) == ("direct", "actor")


def test_luna_metonym_is_claim_specific_and_never_becomes_identity_alias(
    wiki_db,
    monkeypatch,
) -> None:
    ingest_card(
        wiki_db,
        _card(
            "telegram:relation:russia:1",
            "Россия приняла решение",
            surface="Россия",
        ),
    )
    ingest_card(
        wiki_db,
        _card(
            "telegram:relation:russia:2",
            "Россия сообщила о переговорах",
            surface="Россия",
        ),
    )
    scan_registry(wiki_db)
    russia_id = _approve_all_pending(wiki_db)["Россия"]

    moscow = ingest_card(
        wiki_db,
        _card(
            "telegram:relation:moscow:1",
            "Москва заявила о готовности к переговорам",
            surface="Москва",
            category="locations",
            role="",
        ),
    )
    scan_registry(wiki_db)
    occurrence_id = wiki_db.execute(
        """
        SELECT occurrence_version_id
        FROM effective_active_occurrences
        WHERE source_lineage_id = ?
        """,
        (moscow.recorded_card.lineage.source_lineage_id,),
    ).fetchone()[0]
    monkeypatch.setattr(
        "llm_backend.is_luna_role",
        lambda role: role == "default",
    )
    monkeypatch.setattr(
        "llm_backend.active_model_for",
        lambda role: "codex-cli:gpt-5.6-luna@xhigh",
    )
    monkeypatch.setattr(
        "llm_backend.complete_json_sync",
        lambda *args, **kwargs: {
            "resolutions": [],
            "metonym_resolutions": [
                {
                    "occurrence_version_id": occurrence_id,
                    "surface": "Москва",
                    "status": "resolved",
                    "selected_concept_id": russia_id,
                    "candidate_concept_ids": [russia_id],
                    "relation_role": "actor",
                    "explanation": (
                        "Глагол «заявила» указывает на российскую государственную "
                        "сторону, а не на физический город"
                    ),
                }
            ],
        },
    )
    result = link_lineage_concepts(
        wiki_db,
        moscow.recorded_card.lineage.source_lineage_id,
        use_luna=True,
    )
    assert result.resolver_error is None
    link = wiki_db.execute(
        """
        SELECT concept_id, relation_role, link_source
        FROM effective_occurrence_concept_links
        WHERE occurrence_version_id = ?
        """,
        (occurrence_id,),
    ).fetchone()
    assert tuple(link) == (russia_id, "actor", "automatic")
    automatic = wiki_db.execute(
        """
        SELECT rule_id, source_locator_json
        FROM occurrence_concept_automatic_links
        WHERE occurrence_version_id = ? AND concept_id = ?
        ORDER BY automatic_generation DESC
        LIMIT 1
        """,
        (occurrence_id, russia_id),
    ).fetchone()
    assert automatic["rule_id"] == "luna_claim_metonym"
    assert "claim_metonym" in automatic["source_locator_json"]
    assert wiki_db.execute(
        """
        SELECT COUNT(*)
        FROM identity_aliases
        WHERE concept_id = ? AND normalized_surface = 'москва'
        """,
        (russia_id,),
    ).fetchone()[0] == 0


def test_relation_stage_commit_output_and_dependency_are_atomic(
    wiki_db,
    monkeypatch,
) -> None:
    first = ingest_card(
        wiki_db,
        _card(
            "telegram:relation:atomic:1",
            "Китай провёл запуск",
            surface="Китай",
        ),
    )
    ingest_card(
        wiki_db,
        _card(
            "telegram:relation:atomic:2",
            "Китай сообщил об испытании",
            surface="Китай",
        ),
    )
    scan_registry(wiki_db)
    concept_id = _approve_all_pending(wiki_db)["Китай"]
    original_publish = relation_module._publish_relation_dependency

    def fail_publish(*args, **kwargs):
        raise RuntimeError("simulated dependency write failure")

    monkeypatch.setattr(
        relation_module,
        "_publish_relation_dependency",
        fail_publish,
    )
    with pytest.raises(RuntimeError, match="simulated dependency"):
        link_lineage_concepts(
            wiki_db,
            first.recorded_card.lineage.source_lineage_id,
        )

    assert wiki_db.execute(
        """
        SELECT COUNT(*)
        FROM stage_runs
        WHERE source_lineage_id = ?
          AND stage_kind = 'concept_linking'
          AND status = 'committed'
        """,
        (first.recorded_card.lineage.source_lineage_id,),
    ).fetchone()[0] == 0
    assert wiki_db.execute(
        """
        SELECT COUNT(*)
        FROM occurrence_concept_automatic_links
        WHERE concept_id = ?
        """,
        (concept_id,),
    ).fetchone()[0] == 0

    monkeypatch.setattr(
        relation_module,
        "_publish_relation_dependency",
        original_publish,
    )
    recovered = link_lineage_concepts(
        wiki_db,
        first.recorded_card.lineage.source_lineage_id,
    )
    assert recovered.status == "committed"
    assert wiki_db.execute(
        """
        SELECT COUNT(*)
        FROM effective_card_relations
        WHERE concept_id = ?
        """,
        (concept_id,),
    ).fetchone()[0] == 1


def test_eligibility_toggle_reuses_precomputed_luna_relation(
    wiki_db,
    monkeypatch,
) -> None:
    for ordinal, claim in enumerate(
        (
            "Иран провёл переговоры",
            "Иран сообщил о консультациях",
        ),
        start=1,
    ):
        ingest_card(
            wiki_db,
            _card(
                f"telegram:relation:eligibility:evidence:{ordinal}",
                claim,
                surface="Иран",
            ),
        )
    scan_registry(wiki_db)
    concept_id = _approve_all_pending(wiki_db)["Иран"]
    unstable = ingest_card(
        wiki_db,
        _card(
            "telegram:relation:eligibility:target",
            "Иран заявил о новом решении",
            surface="Иран",
            role="",
            quality_flags=["extraction_unstable"],
        ),
    )
    occurrence_id = wiki_db.execute(
        """
        SELECT occurrence_version_id
        FROM lifecycle_active_occurrences
        WHERE source_lineage_id = ?
        """,
        (unstable.recorded_card.lineage.source_lineage_id,),
    ).fetchone()[0]
    resolver_calls = 0

    def resolve_role(*args, **kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return {
            "resolutions": [
                {
                    "occurrence_version_id": occurrence_id,
                    "concept_id": concept_id,
                    "relation_role": "actor",
                    "explanation": "Иран является говорящим актором",
                }
            ]
        }

    monkeypatch.setattr(
        "llm_backend.is_luna_role",
        lambda role: role == "default",
    )
    monkeypatch.setattr(
        "llm_backend.active_model_for",
        lambda role: "codex-cli:gpt-5.6-luna@xhigh",
    )
    monkeypatch.setattr("llm_backend.complete_json_sync", resolve_role)
    first = link_lineage_concepts(
        wiki_db,
        unstable.recorded_card.lineage.source_lineage_id,
        use_luna=True,
    )
    assert first.status == "committed"
    assert resolver_calls == 1
    assert wiki_db.execute(
        """
        SELECT COUNT(*)
        FROM effective_active_occurrences
        WHERE source_lineage_id = ?
        """,
        (unstable.recorded_card.lineage.source_lineage_id,),
    ).fetchone()[0] == 0

    eligible = ingest_card(
        wiki_db,
        _card(
            "telegram:relation:eligibility:target",
            "Иран заявил о новом решении",
            surface="Иран",
            role="",
        ),
    )
    activated = link_lineage_concepts(
        wiki_db,
        unstable.recorded_card.lineage.source_lineage_id,
        use_luna=True,
    )
    assert activated.status == "no_op"
    assert resolver_calls == 1
    relation = wiki_db.execute(
        """
        SELECT relation_kind, strongest_relation_role
        FROM effective_card_relations
        WHERE card_revision_id = ? AND concept_id = ?
        """,
        (eligible.recorded_card.card_revision.card_revision_id, concept_id),
    ).fetchone()
    assert tuple(relation) == ("direct", "actor")
    dependency_kinds = {
        row["dependency_kind"]
        for row in wiki_db.execute(
            """
            SELECT dependency_kind
            FROM stage_dependency_bindings
            WHERE stage_version_id = ?
            """,
            (activated.stage_version_id,),
        )
    }
    assert "eligibility_state" not in dependency_kinds


def test_clear_structured_direct_role_does_not_spend_luna_call(
    wiki_db,
    monkeypatch,
) -> None:
    first = ingest_card(
        wiki_db,
        _card(
            "telegram:relation:clear-role:1",
            "Китай провёл запуск",
            surface="Китай",
        ),
    )
    ingest_card(
        wiki_db,
        _card(
            "telegram:relation:clear-role:2",
            "Китай сообщил об испытании",
            surface="Китай",
        ),
    )
    scan_registry(wiki_db)
    _approve_all_pending(wiki_db)
    resolver_calls = 0

    def unexpected_resolver(*args, **kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return {"resolutions": [], "metonym_resolutions": []}

    monkeypatch.setattr(
        "llm_backend.is_luna_role",
        lambda role: role == "default",
    )
    monkeypatch.setattr(
        "llm_backend.active_model_for",
        lambda role: "codex-cli:gpt-5.6-luna@xhigh",
    )
    monkeypatch.setattr(
        "llm_backend.complete_json_sync",
        unexpected_resolver,
    )
    result = link_lineage_concepts(
        wiki_db,
        first.recorded_card.lineage.source_lineage_id,
        use_luna=True,
    )
    assert result.status == "committed"
    assert resolver_calls == 0
