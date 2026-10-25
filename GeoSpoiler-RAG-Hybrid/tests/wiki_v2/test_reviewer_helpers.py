from __future__ import annotations

from types import SimpleNamespace

from wiki_reviewer import (
    _actionable_registry_proposals,
    _best_proposal_snippet,
    _proposal_action_text,
    _proposal_reason_text,
    _proposal_support_label,
    _proposal_title,
)


def _proposal(
    proposal_id: str,
    kind: str,
    payload: dict | None = None,
    *,
    cluster_count: int = 2,
):
    return SimpleNamespace(
        proposal_id=proposal_id,
        proposal_kind=kind,
        display_label=proposal_id,
        source_category=(payload or {}).get("source_category", "other"),
        payload=payload or {},
        cluster_count=cluster_count,
    )


def test_composite_identity_review_hides_member_candidates() -> None:
    north = _proposal("north-korea", "entity")
    dprk = _proposal("dprk", "entity")
    merge = _proposal(
        "merge-korea",
        "merge",
        {
            "member_proposal_ids": [
                north.proposal_id,
                dprk.proposal_id,
            ]
        },
    )

    actionable = _actionable_registry_proposals((north, dprk, merge))
    assert [proposal.proposal_id for proposal in actionable] == ["merge-korea"]


def test_rejected_composite_absence_reveals_original_candidates() -> None:
    north = _proposal("north-korea", "entity")
    dprk = _proposal("dprk", "entity")

    actionable = _actionable_registry_proposals((north, dprk))
    assert {proposal.proposal_id for proposal in actionable} == {
        "north-korea",
        "dprk",
    }


def test_registry_support_label_explains_hybrid_youtube_qualification() -> None:
    primary_topic = _proposal(
        "dprk-missiles",
        "topic",
        {
            "qualification": {
                "rule_id": "primary_youtube_segment_topic",
            },
            "threshold": {
                "observed_distinct_source_families": 1,
            },
        },
        cluster_count=1,
    )
    independent = _proposal(
        "china",
        "entity",
        {
            "qualification": {
                "rule_id": "independent_source_families",
            },
            "threshold": {
                "observed_distinct_source_families": 3,
            },
        },
        cluster_count=4,
    )

    assert _proposal_support_label(primary_topic) == (
        "primary-тема содержательного YouTube-сегмента"
    )
    assert _proposal_support_label(independent) == (
        "4 content clusters · 3 source families"
    )


def test_registry_proposal_copy_explains_the_decision_in_plain_language() -> None:
    proposal = _proposal(
        "Европы",
        "entity",
        {
            "source_category": "locations",
            "qualification": {
                "rule_id": "independent_source_families",
            },
            "threshold": {
                "observed_distinct_content_clusters": 3,
                "observed_distinct_source_families": 2,
            },
        },
        cluster_count=3,
    )

    assert _proposal_title(proposal) == (
        "Новый hub-сущность «Европы» · место или регион"
    )
    assert _proposal_action_text(proposal) == (
        "Предлагается создать approved Wiki hub «Европы». "
        "Это одобрение сущности, а не подтверждение истинности связанных claims."
    )
    assert _proposal_reason_text(proposal) == (
        "Форма «Европы» встретилась в 3 разных содержательных кластерах "
        "из 2 независимых source families. Порог показа: минимум 2 кластера "
        "из 2 families."
    )


def test_identity_group_copy_shows_forms_result_and_luna_reason() -> None:
    proposal = _proposal(
        "merge-europe",
        "merge",
        {
            "display_label": "Европа",
            "member_surfaces": [
                {"display_label": "Европа"},
                {"display_label": "Европе"},
                {"display_label": "Европы"},
            ],
            "rationale": (
                "Это падежные формы одного географического concept."
            ),
        },
    )
    proposal.display_label = "Европа"

    assert _proposal_title(proposal) == (
        "Один hub «Европа»: Европа / Европе / Европы"
    )
    assert "одним concept" in _proposal_action_text(proposal)
    assert "До одобрения registry не изменяется" in _proposal_action_text(proposal)
    assert _proposal_reason_text(proposal) == (
        "Это падежные формы одного географического concept."
    )


def test_single_candidate_canonicalization_is_an_explicit_review_action() -> None:
    proposal = _proposal(
        "normalize-ukraine",
        "merge",
        {
            "display_label": "Украина",
            "identity_review_kind": "canonicalization",
            "member_surfaces": [{"display_label": "Украиной"}],
            "rationale": "«Украиной» — падежная форма названия «Украина».",
        },
    )
    proposal.display_label = "Украина"

    assert _proposal_title(proposal) == (
        "Нормальная форма: «Украиной» → «Украина»"
    )
    action = _proposal_action_text(proposal)
    assert "назвать hub «Украина»" in action
    assert "исходная форма сохранится как alias" in action
    assert "До одобрения registry не изменяется" in action


def test_best_proposal_snippet_prefers_matching_claim_or_event() -> None:
    card_payload = {
        "summary": "Материал посвящён военной связи.",
        "key_points": [
            {
                "text": "Связь является основой управления войсками.",
            }
        ],
        "events": [
            {
                "description": (
                    "В гипотетическом конфликте действия происходят "
                    "на территории Европы."
                )
            }
        ],
        "theses": [],
        "quotes": [],
    }

    snippet = _best_proposal_snippet(card_payload, "европы")

    assert snippet["field_label"] == "Событие"
    assert "Европы" in snippet["text"]
