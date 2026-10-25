from __future__ import annotations

import pytest

from retrieval.wiki import (
    MAX_RELATED_CONCEPTS,
    HierarchyCycleError,
    HierarchyLimitError,
    approve_hierarchy_proposal,
    create_hierarchy_proposal,
    list_concept_tree,
    list_hierarchy_proposals,
    propose_hierarchy_reviews_with_luna,
    reject_hierarchy_proposal,
    remove_primary_parent,
    remove_related_edge,
    reopen_hierarchy_proposal,
    review_hierarchy_batch,
)
from tests.wiki_v2.helpers import insert_approved_concept


def _concepts(wiki_db, *concept_ids: str) -> None:
    for concept_id in concept_ids:
        insert_approved_concept(wiki_db, concept_id=concept_id)


def test_hierarchy_proposals_do_nothing_until_approved_and_cycles_are_rejected(
    wiki_db,
) -> None:
    _concepts(wiki_db, "world", "asia", "china")
    asia = create_hierarchy_proposal(
        wiki_db,
        child_concept_id="asia",
        other_concept_id="world",
        edge_kind="primary_parent",
        rationale="Азия входит в мировую географию",
    )
    assert wiki_db.execute(
        "SELECT COUNT(*) FROM effective_primary_hierarchy_edges"
    ).fetchone()[0] == 0
    approve_hierarchy_proposal(wiki_db, asia.proposal_id)

    china = create_hierarchy_proposal(
        wiki_db,
        child_concept_id="china",
        other_concept_id="asia",
        edge_kind="primary_parent",
        rationale="Китай расположен в Азии",
    )
    approve_hierarchy_proposal(wiki_db, china.proposal_id)
    cycle = create_hierarchy_proposal(
        wiki_db,
        child_concept_id="world",
        other_concept_id="china",
        edge_kind="primary_parent",
        rationale="Ошибочная циклическая ветка",
    )
    with pytest.raises(HierarchyCycleError):
        approve_hierarchy_proposal(wiki_db, cycle.proposal_id)
    assert next(
        proposal
        for proposal in list_hierarchy_proposals(wiki_db)
        if proposal.proposal_id == cycle.proposal_id
    ).status == "pending"

    tree = {node.concept_id: node for node in list_concept_tree(wiki_db)}
    assert tree["asia"].parent_concept_id == "world"
    assert tree["china"].parent_concept_id == "asia"
    assert tree["world"].child_concept_ids == ("asia",)


def test_explicit_approval_can_move_one_primary_parent_and_remove_it(wiki_db) -> None:
    _concepts(wiki_db, "root-a", "root-b", "child")
    first = create_hierarchy_proposal(
        wiki_db,
        child_concept_id="child",
        other_concept_id="root-a",
        edge_kind="primary_parent",
        rationale="Первая предложенная ветка",
    )
    approve_hierarchy_proposal(wiki_db, first.proposal_id)
    second = create_hierarchy_proposal(
        wiki_db,
        child_concept_id="child",
        other_concept_id="root-b",
        edge_kind="primary_parent",
        rationale="Пользователь явно подтверждает перенос",
    )
    approve_hierarchy_proposal(wiki_db, second.proposal_id)
    row = wiki_db.execute(
        """
        SELECT parent_concept_id
        FROM effective_primary_hierarchy_edges
        WHERE child_concept_id = 'child'
        """
    ).fetchone()
    assert row["parent_concept_id"] == "root-b"
    remove_primary_parent(wiki_db, "child")
    assert wiki_db.execute(
        """
        SELECT COUNT(*)
        FROM effective_primary_hierarchy_edges
        WHERE child_concept_id = 'child'
        """
    ).fetchone()[0] == 0


def test_related_edges_are_reviewed_bounded_and_removable(wiki_db) -> None:
    center = "center"
    others = [f"related-{index}" for index in range(MAX_RELATED_CONCEPTS + 1)]
    _concepts(wiki_db, center, *others)
    approved_ids: list[str] = []
    for concept_id in others[:MAX_RELATED_CONCEPTS]:
        proposal = create_hierarchy_proposal(
            wiki_db,
            child_concept_id=center,
            other_concept_id=concept_id,
            edge_kind="related",
            rationale="Значимая связь",
        )
        approve_hierarchy_proposal(wiki_db, proposal.proposal_id)
        approved_ids.append(concept_id)
    overflow = create_hierarchy_proposal(
        wiki_db,
        child_concept_id=center,
        other_concept_id=others[-1],
        edge_kind="related",
        rationale="Лишняя шестая связь",
    )
    with pytest.raises(HierarchyLimitError):
        approve_hierarchy_proposal(wiki_db, overflow.proposal_id)
    tree = {node.concept_id: node for node in list_concept_tree(wiki_db)}
    assert len(tree[center].related_concept_ids) == MAX_RELATED_CONCEPTS

    remove_related_edge(wiki_db, center, approved_ids[0])
    assert len(
        {
            node.concept_id: node for node in list_concept_tree(wiki_db)
        }[center].related_concept_ids
    ) == MAX_RELATED_CONCEPTS - 1


def test_hierarchy_reject_and_reopen_preserve_history(wiki_db) -> None:
    _concepts(wiki_db, "parent", "child")
    proposal = create_hierarchy_proposal(
        wiki_db,
        child_concept_id="child",
        other_concept_id="parent",
        edge_kind="primary_parent",
        rationale="Кандидат",
    )
    reject_hierarchy_proposal(
        wiki_db,
        proposal.proposal_id,
        rationale="Пока недостаточно оснований",
    )
    assert list_hierarchy_proposals(wiki_db) == ()
    reopen_hierarchy_proposal(
        wiki_db,
        proposal.proposal_id,
        rationale="Появились новые основания",
    )
    assert list_hierarchy_proposals(wiki_db)[0].status == "pending"
    approve_hierarchy_proposal(wiki_db, proposal.proposal_id)
    assert len(
        list_hierarchy_proposals(wiki_db, statuses=("approved",))
    ) == 1


def test_luna_hierarchy_analysis_only_creates_review_batch(
    wiki_db,
    monkeypatch,
) -> None:
    _concepts(wiki_db, "world", "asia", "china")
    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.is_luna_role",
        lambda role: role == "default",
    )
    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.active_model_for",
        lambda role: "codex-cli:gpt-5.6-luna@xhigh",
    )
    resolver_calls = 0

    def resolve_hierarchy(*args, **kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return {
            "primary_edges": [
                {
                    "child_concept_id": "china",
                    "parent_concept_id": "asia",
                    "reason": "Китай — страна Азии",
                }
            ],
            "related_edges": [
                {
                    "left_concept_id": "asia",
                    "right_concept_id": "world",
                    "reason": "Значимая навигационная связь",
                }
            ],
        }

    monkeypatch.setattr(
        "retrieval.wiki.analysis.llm_backend.complete_json_sync",
        resolve_hierarchy,
    )
    result = propose_hierarchy_reviews_with_luna(wiki_db)
    assert result.primary_proposals_created == 1
    assert result.related_proposals_created == 1
    assert result.batch_id
    repeated = propose_hierarchy_reviews_with_luna(wiki_db)
    assert repeated.cache_hit
    assert resolver_calls == 1
    assert wiki_db.execute(
        "SELECT COUNT(*) FROM effective_primary_hierarchy_edges"
    ).fetchone()[0] == 0
    assert len(list_hierarchy_proposals(wiki_db)) == 2


def test_hierarchy_batch_rolls_back_every_edge_when_one_creates_cycle(
    wiki_db,
) -> None:
    _concepts(wiki_db, "alpha", "beta")
    first = create_hierarchy_proposal(
        wiki_db,
        child_concept_id="alpha",
        other_concept_id="beta",
        edge_kind="primary_parent",
        rationale="Первая половина цикла",
    )
    second = create_hierarchy_proposal(
        wiki_db,
        child_concept_id="beta",
        other_concept_id="alpha",
        edge_kind="primary_parent",
        rationale="Вторая половина цикла",
    )

    with pytest.raises(HierarchyCycleError):
        review_hierarchy_batch(
            wiki_db,
            [first.proposal_id, second.proposal_id],
            decision="approve",
            rationale="Проверка атомарного пакета",
        )

    assert wiki_db.execute(
        "SELECT COUNT(*) FROM effective_primary_hierarchy_edges"
    ).fetchone()[0] == 0
    assert {
        proposal.status
        for proposal in list_hierarchy_proposals(wiki_db)
        if proposal.proposal_id in {first.proposal_id, second.proposal_id}
    } == {"pending"}
