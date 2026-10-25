from __future__ import annotations

import sqlite3

import pytest

from retrieval.wiki.state import (
    DependencyKind,
    IdempotencyConflictError,
    OutboxEventSpec,
    commit_stage_run,
    ensure_source_lineage,
    fail_stage_run,
    publish_dependency,
    record_card_revision,
    start_stage_run,
)
from tests.wiki_v2.helpers import (
    insert_approved_concept,
    insert_occurrence_fixture,
    prepare_stage,
)


def _insert_root_event(
    connection: sqlite3.Connection,
    *,
    state_event_id: str,
    occurrence_version_id: str,
    extraction_run_id: str,
    source_lineage_id: str,
) -> None:
    connection.execute(
        """
        INSERT INTO occurrence_state_events (
            state_event_id,
            occurrence_version_id,
            extraction_run_id,
            source_lineage_id,
            to_status,
            created_at
        ) VALUES (?, ?, ?, ?, 'active', 'audit')
        """,
        (
            state_event_id,
            occurrence_version_id,
            extraction_run_id,
            source_lineage_id,
        ),
    )


def _insert_projection_artifact(
    connection: sqlite3.Connection,
    *,
    prepared,
    artifact_id: str,
    projection_kind: str,
    scope_key: str,
    concept_id: str | None = None,
    card_revision_id: str | None = None,
    claim_group_id: str | None = None,
    generation: int = 1,
    inputs_hash: str = "inputs",
    output_hash: str = "output",
    fts_hash: str = "fts",
) -> None:
    connection.execute(
        """
        INSERT INTO projection_artifacts (
            projection_artifact_id,
            projection_kind,
            projection_scope_key,
            card_revision_id,
            concept_id,
            claim_group_id,
            projection_generation,
            projection_inputs_hash,
            projection_output_hash,
            fts_document_hash,
            rendered_content,
            search_text,
            processor_contract_version_id,
            produced_by_stage_version_id,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, 'audit')
        """,
        (
            artifact_id,
            projection_kind,
            scope_key,
            card_revision_id,
            concept_id,
            claim_group_id,
            generation,
            inputs_hash,
            output_hash,
            fts_hash,
            prepared.stage.processor_contract_version_id,
            prepared.stage.stage_version_id,
        ),
    )


def test_occurrence_events_require_committed_extraction_run(wiki_db) -> None:
    prepared = prepare_stage(wiki_db, external_key="telegram:event-lifecycle:1")
    started = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="event-started",
    )
    started_occurrence = insert_occurrence_fixture(
        wiki_db,
        prepared=prepared,
        stage_run_id=started.stage_run_id,
        suffix="started",
    )

    with pytest.raises(sqlite3.IntegrityError, match="committed extraction run"):
        _insert_root_event(
            wiki_db,
            state_event_id="started-event",
            occurrence_version_id=started_occurrence.occurrence_version_id,
            extraction_run_id=started_occurrence.extraction_run_id,
            source_lineage_id=prepared.lineage_id,
        )

    committed = commit_stage_run(wiki_db, stage_run_id=started.stage_run_id)
    assert committed.status == "committed"
    _insert_root_event(
        wiki_db,
        state_event_id="committed-event",
        occurrence_version_id=started_occurrence.occurrence_version_id,
        extraction_run_id=started_occurrence.extraction_run_id,
        source_lineage_id=prepared.lineage_id,
    )
    assert wiki_db.execute(
        """
        SELECT status
        FROM occurrence_current_states
        WHERE occurrence_version_id = ?
        """,
        (started_occurrence.occurrence_version_id,),
    ).fetchone()["status"] == "active"

    failed_run = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="event-failed",
    )
    failed_occurrence = insert_occurrence_fixture(
        wiki_db,
        prepared=prepared,
        stage_run_id=failed_run.stage_run_id,
        suffix="failed",
    )
    fail_stage_run(
        wiki_db,
        stage_run_id=failed_run.stage_run_id,
        error_text="failed worker",
    )
    with pytest.raises(sqlite3.IntegrityError, match="committed extraction run"):
        _insert_root_event(
            wiki_db,
            state_event_id="failed-event",
            occurrence_version_id=failed_occurrence.occurrence_version_id,
            extraction_run_id=failed_occurrence.extraction_run_id,
            source_lineage_id=prepared.lineage_id,
        )


def test_cross_lineage_stage_and_extraction_cards_are_rejected(wiki_db) -> None:
    prepared_a = prepare_stage(wiki_db, external_key="telegram:lineage:A")
    lineage_b = ensure_source_lineage(
        wiki_db,
        source_kind="telegram",
        external_key="telegram:lineage:B",
    )
    card_b = record_card_revision(
        wiki_db,
        source_lineage_id=lineage_b.source_lineage_id,
        card_payload={"summary": "B"},
        input_payloads={"claim_inputs": {"key_points": ["B"]}},
    )

    with pytest.raises(sqlite3.IntegrityError):
        start_stage_run(
            wiki_db,
            stage_version_id=prepared_a.stage.stage_version_id,
            idempotency_key="cross-card-start",
            artifact_source_card_revision_id=card_b.card_revision_id,
        )

    valid_run = start_stage_run(
        wiki_db,
        stage_version_id=prepared_a.stage.stage_version_id,
        idempotency_key="lineage-a-run",
        artifact_source_card_revision_id=prepared_a.card_revision_id,
    )
    wiki_db.execute(
        """
        INSERT INTO extraction_artifacts (
            extraction_artifact_id,
            extraction_artifact_key,
            processor_contract_version_id,
            claim_inputs_hash,
            artifact_hash,
            artifact_json,
            created_at
        ) VALUES ('cross-artifact', 'cross-artifact-key', ?, ?, 'artifact', '{}', 'audit')
        """,
        (
            prepared_a.stage.processor_contract_version_id,
            prepared_a.stage.input_bindings[0].input_hash,
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        wiki_db.execute(
            """
            INSERT INTO extraction_runs (
                extraction_run_id,
                stage_run_id,
                extraction_artifact_id,
                source_lineage_id,
                stage_kind,
                processor_contract_version_id,
                claim_inputs_hash,
                artifact_source_card_revision_id,
                applied_against_card_revision_id,
                created_at
            ) VALUES (
                'cross-extraction',
                ?,
                'cross-artifact',
                ?,
                'claim_extraction',
                ?,
                ?,
                ?,
                ?,
                'audit'
            )
            """,
            (
                valid_run.stage_run_id,
                lineage_b.source_lineage_id,
                prepared_a.stage.processor_contract_version_id,
                prepared_a.stage.input_bindings[0].input_hash,
                card_b.card_revision_id,
                card_b.card_revision_id,
            ),
        )


def test_identity_alias_revision_must_belong_to_same_concept(wiki_db) -> None:
    revision_a = insert_approved_concept(wiki_db, concept_id="concept-a")
    revision_b = insert_approved_concept(wiki_db, concept_id="concept-b")

    wiki_db.execute(
        """
        INSERT INTO identity_aliases (
            identity_alias_id,
            concept_id,
            concept_revision_id,
            normalized_surface,
            display_surface,
            alias_kind,
            approved_at
        ) VALUES ('valid-alias', 'concept-a', ?, 'a', 'A', 'canonical', 'audit')
        """,
        (revision_a,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        wiki_db.execute(
            """
            INSERT INTO identity_aliases (
                identity_alias_id,
                concept_id,
                concept_revision_id,
                normalized_surface,
                display_surface,
                alias_kind,
                approved_at
            ) VALUES ('cross-alias', 'concept-a', ?, 'wrong', 'Wrong', 'spelling', 'audit')
            """,
            (revision_b,),
        )


def test_hub_projection_requires_matching_effective_approved_concept(wiki_db) -> None:
    hub_prepared = prepare_stage(
        wiki_db,
        stage_kind="hub_projection",
        external_key="telegram:hub-projection:1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_projection_artifact(
            wiki_db,
            prepared=hub_prepared,
            artifact_id="missing-hub",
            projection_kind="hub",
            scope_key="missing-concept",
            concept_id="missing-concept",
        )

    wiki_db.execute(
        """
        INSERT INTO concepts (
            concept_id, concept_kind, approval_status, canonical_key, created_at
        ) VALUES ('headless-concept', 'topic', 'approved', 'headless', 'audit')
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_projection_artifact(
            wiki_db,
            prepared=hub_prepared,
            artifact_id="headless-hub",
            projection_kind="hub",
            scope_key="headless-concept",
            concept_id="headless-concept",
        )

    insert_approved_concept(wiki_db, concept_id="approved-hub")
    _insert_projection_artifact(
        wiki_db,
        prepared=hub_prepared,
        artifact_id="approved-hub-artifact",
        projection_kind="hub",
        scope_key="approved-hub",
        concept_id="approved-hub",
    )
    wiki_db.execute(
        """
        INSERT INTO projection_heads (
            projection_kind,
            projection_scope_key,
            card_revision_id,
            concept_id,
            claim_group_id,
            current_projection_artifact_id,
            current_projection_generation,
            current_projection_inputs_hash,
            current_projection_output_hash,
            current_fts_document_hash,
            updated_at
        ) VALUES (
            'hub',
            'approved-hub',
            NULL,
            'approved-hub',
            NULL,
            'approved-hub-artifact',
            1,
            'inputs',
            'output',
            'fts',
            'audit'
        )
        """
    )

    card_prepared = prepare_stage(
        wiki_db,
        stage_kind="card_projection",
        external_key="telegram:card-projection:1",
    )
    _insert_projection_artifact(
        wiki_db,
        prepared=card_prepared,
        artifact_id="card-artifact",
        projection_kind="card",
        scope_key=card_prepared.card_revision_id,
        card_revision_id=card_prepared.card_revision_id,
        concept_id=None,
    )
    wiki_db.execute(
        """
        INSERT INTO projection_heads (
            projection_kind,
            projection_scope_key,
            card_revision_id,
            concept_id,
            claim_group_id,
            current_projection_artifact_id,
            current_projection_generation,
            current_projection_inputs_hash,
            current_projection_output_hash,
            current_fts_document_hash,
            updated_at
        ) VALUES (
            'card', ?, ?, NULL, NULL, 'card-artifact', 1,
            'inputs', 'output', 'fts', 'audit'
        )
        """,
        (card_prepared.card_revision_id, card_prepared.card_revision_id),
    )

    assert wiki_db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert wiki_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_claim_projection_identity_and_rebuild_generation(wiki_db) -> None:
    prepared = prepare_stage(
        wiki_db,
        stage_kind="claim_projection",
        external_key="telegram:claim-projection:1",
    )
    for group_id in ("claim-group-a", "claim-group-b"):
        wiki_db.execute(
            """
            INSERT INTO claim_groups (
                claim_group_id,
                canonical_claim_hash,
                canonical_claim_json,
                created_by_stage_version_id,
                created_at
            ) VALUES (?, ?, '{}', ?, 'audit')
            """,
            (
                group_id,
                f"hash-{group_id}",
                prepared.stage.stage_version_id,
            ),
        )

    with pytest.raises(sqlite3.IntegrityError):
        _insert_projection_artifact(
            wiki_db,
            prepared=prepared,
            artifact_id="missing-claim-artifact",
            projection_kind="claim",
            scope_key="missing-claim-group",
            concept_id=None,
            claim_group_id="missing-claim-group",
        )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_projection_artifact(
            wiki_db,
            prepared=prepared,
            artifact_id="mismatched-claim-artifact",
            projection_kind="claim",
            scope_key="claim-group-b",
            concept_id=None,
            claim_group_id="claim-group-a",
        )

    _insert_projection_artifact(
        wiki_db,
        prepared=prepared,
        artifact_id="claim-artifact-1",
        projection_kind="claim",
        scope_key="claim-group-a",
        concept_id=None,
        claim_group_id="claim-group-a",
    )
    with pytest.raises(sqlite3.IntegrityError):
        wiki_db.execute(
            """
            INSERT INTO projection_heads (
                projection_kind,
                projection_scope_key,
                concept_id,
                claim_group_id,
                current_projection_artifact_id,
                current_projection_generation,
                current_projection_inputs_hash,
                current_projection_output_hash,
                current_fts_document_hash,
                updated_at
            ) VALUES (
                'claim',
                'claim-group-b',
                NULL,
                'claim-group-b',
                'claim-artifact-1',
                1,
                'inputs',
                'output',
                'fts',
                'audit'
            )
            """
        )

    wiki_db.execute(
        """
        INSERT INTO projection_heads (
            projection_kind,
            projection_scope_key,
            concept_id,
            claim_group_id,
            current_projection_artifact_id,
            current_projection_generation,
            current_projection_inputs_hash,
            current_projection_output_hash,
            current_fts_document_hash,
            updated_at
        ) VALUES (
            'claim',
            'claim-group-a',
            NULL,
            'claim-group-a',
            'claim-artifact-1',
            1,
            'inputs',
            'output',
            'fts',
            'audit'
        )
        """
    )

    _insert_projection_artifact(
        wiki_db,
        prepared=prepared,
        artifact_id="claim-artifact-2",
        projection_kind="claim",
        scope_key="claim-group-a",
        concept_id=None,
        claim_group_id="claim-group-a",
        generation=2,
        inputs_hash="inputs-2",
        output_hash="output-2",
        fts_hash="fts-2",
    )
    wiki_db.execute(
        """
        UPDATE projection_heads
        SET
            current_projection_artifact_id = 'claim-artifact-2',
            current_projection_generation = 2,
            current_projection_inputs_hash = 'inputs-2',
            current_projection_output_hash = 'output-2',
            current_fts_document_hash = 'fts-2',
            updated_at = 'audit-2'
        WHERE projection_kind = 'claim'
          AND projection_scope_key = 'claim-group-a'
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="generation must increase"):
        wiki_db.execute(
            """
            UPDATE projection_heads
            SET
                current_projection_artifact_id = 'claim-artifact-1',
                current_projection_generation = 1,
                current_projection_inputs_hash = 'inputs',
                current_projection_output_hash = 'output',
                current_fts_document_hash = 'fts',
                updated_at = 'audit-rollback'
            WHERE projection_kind = 'claim'
              AND projection_scope_key = 'claim-group-a'
            """
        )

    head = wiki_db.execute(
        """
        SELECT current_projection_generation, current_projection_output_hash
        FROM projection_heads
        WHERE projection_kind = 'claim'
          AND projection_scope_key = 'claim-group-a'
        """
    ).fetchone()
    assert tuple(head) == (2, "output-2")

    dependency = publish_dependency(
        wiki_db,
        dependency_kind=DependencyKind.CLAIM_PROJECTION_SNAPSHOT,
        dependency_scope_key="claim-group-a",
        payload={"projection_output_hash": "output-2"},
        expected_version_id=None,
        producer_kind="stage",
        produced_by_stage_version_id=prepared.stage.stage_version_id,
    )
    assert dependency.dependency_kind is DependencyKind.CLAIM_PROJECTION_SNAPSHOT
    assert wiki_db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert wiki_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_outbox_deduplicates_canonical_equivalents_and_rejects_real_conflict(
    wiki_db,
) -> None:
    prepared = prepare_stage(wiki_db, external_key="telegram:canonical-outbox:1")
    canonical_run = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="canonical-outbox",
    )
    decomposed = OutboxEventSpec(
        event_key="event-cafe\u0301",
        event_kind="de\u0301pendency",
        aggregate_kind="source",
        aggregate_key="cafe\u0301",
        payload={"label": "cafe\u0301"},
    )
    composed = OutboxEventSpec(
        event_key="event-café",
        event_kind="dépendency",
        aggregate_kind="source",
        aggregate_key="café",
        payload={"label": "café"},
    )
    committed = commit_stage_run(
        wiki_db,
        stage_run_id=canonical_run.stage_run_id,
        outbox_events=[decomposed, composed],
    )
    assert committed.status == "committed"
    stored = wiki_db.execute(
        "SELECT event_key, event_kind, aggregate_key, payload_json FROM outbox_events"
    ).fetchall()
    assert [tuple(row) for row in stored] == [
        ("event-café", "dépendency", "café", '{"label":"café"}')
    ]

    conflicting_run = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="conflicting-outbox",
    )
    with pytest.raises(IdempotencyConflictError, match="different content"):
        commit_stage_run(
            wiki_db,
            stage_run_id=conflicting_run.stage_run_id,
            outbox_events=[
                OutboxEventSpec("same-key", "kind", "aggregate", "key", {"value": 1}),
                OutboxEventSpec("same-key", "kind", "aggregate", "key", {"value": 2}),
            ],
        )
    assert wiki_db.execute(
        "SELECT status FROM stage_runs WHERE stage_run_id = ?",
        (conflicting_run.stage_run_id,),
    ).fetchone()["status"] == "started"
