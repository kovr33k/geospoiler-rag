from __future__ import annotations

from models import EnrichedCardV2
from retrieval.wiki import (
    group_all_claims,
    ingest_card,
    set_group_override,
)
from tests.wiki_v2.test_phase3_cards import enriched_data


def _claim_card(source_id: str, claim: str, *, unstable: bool = False) -> EnrichedCardV2:
    return EnrichedCardV2.model_validate(
        enriched_data(
            source_id=source_id,
            key_points=[
                {
                    "text": claim,
                    "type": "reported_statement",
                    "importance": "high",
                }
            ],
            quality_flags=["extraction_unstable"] if unstable else [],
        )
    )


def test_grouping_merges_only_canonical_exact_claims_and_is_idempotent(wiki_db) -> None:
    ingest_card(wiki_db, _claim_card("telegram:group:1", "Китай провёл запуск"))
    ingest_card(wiki_db, _claim_card("telegram:group:2", "Китай провёл запуск"))
    ingest_card(wiki_db, _claim_card("telegram:group:3", "Китай провёл новый запуск"))

    first = group_all_claims(wiki_db)
    assert first.lineages_committed == 3
    assert first.occurrences_seen == 3
    assert first.groups_created == 2
    memberships = wiki_db.execute(
        """
        SELECT claim_group_id, COUNT(*) AS occurrence_count
        FROM effective_claim_group_memberships
        GROUP BY claim_group_id
        ORDER BY occurrence_count DESC
        """
    ).fetchall()
    assert [row["occurrence_count"] for row in memberships] == [2, 1]

    second = group_all_claims(wiki_db)
    assert second.lineages_no_op == 3
    assert second.groups_created == 0
    assert second.memberships_written == 0
    assert second.dependencies_changed == 0


def test_eligibility_only_hides_precomputed_group_membership(wiki_db) -> None:
    result = ingest_card(
        wiki_db,
        _claim_card("telegram:group:unstable", "Нестабильное извлечение", unstable=True),
    )
    assert result.lifecycle_result
    assert result.lifecycle_result.counts.active == 1
    stats = group_all_claims(wiki_db)
    assert stats.occurrences_seen == 1
    assert wiki_db.execute("SELECT COUNT(*) FROM claim_occurrences").fetchone()[0] == 1
    assert (
        wiki_db.execute(
            "SELECT COUNT(*) FROM effective_claim_group_memberships"
        ).fetchone()[0]
        == 1
    )
    assert wiki_db.execute(
        "SELECT COUNT(*) FROM lifecycle_active_occurrences"
    ).fetchone()[0] == 1
    assert wiki_db.execute(
        "SELECT COUNT(*) FROM effective_active_occurrences"
    ).fetchone()[0] == 0

    ingest_card(
        wiki_db,
        _claim_card("telegram:group:unstable", "Нестабильное извлечение"),
    )
    activated = group_all_claims(wiki_db)
    assert activated.lineages_no_op == 1
    assert activated.memberships_written == 0
    assert wiki_db.execute(
        "SELECT COUNT(*) FROM effective_active_occurrences"
    ).fetchone()[0] == 1


def test_manual_group_override_overlays_automatic_membership(wiki_db) -> None:
    ingest_card(wiki_db, _claim_card("telegram:group:override", "Исходный claim"))
    group_all_claims(wiki_db)
    row = wiki_db.execute(
        """
        SELECT occurrence_version_id, claim_group_id
        FROM effective_claim_group_memberships
        """
    ).fetchone()
    assert row
    set_group_override(
        wiki_db,
        occurrence_version_id=row["occurrence_version_id"],
        claim_group_id=None,
        rationale="Не включать occurrence в автоматическую группу",
    )
    assert (
        wiki_db.execute(
            "SELECT COUNT(*) FROM effective_claim_group_memberships"
        ).fetchone()[0]
        == 0
    )
    set_group_override(
        wiki_db,
        occurrence_version_id=row["occurrence_version_id"],
        claim_group_id=row["claim_group_id"],
        rationale="Вернуть вручную",
    )
    effective = wiki_db.execute(
        """
        SELECT claim_group_id, membership_source
        FROM effective_claim_group_memberships
        """
    ).fetchone()
    assert tuple(effective) == (row["claim_group_id"], "override")
