from __future__ import annotations

import sqlite3

import pytest

from retrieval.wiki.state import (
    OutboxEventSpec,
    activate_processor_contract,
    advance_input_head,
    commit_stage_run,
    fail_stage_run,
    list_pending_outbox,
    mark_outbox_processed,
    publish_dependency,
    start_stage_run,
)
from tests.wiki_v2.helpers import contract, prepare_stage


def event(event_key: str = "wiki:claim:1") -> OutboxEventSpec:
    return OutboxEventSpec(
        event_key=event_key,
        event_kind="dependency_published",
        aggregate_kind="source_lineage",
        aggregate_key="telegram:test:1",
        payload={"dirty_stage": "grouping"},
    )


def test_stage_run_commit_assigns_sequence_and_emits_outbox(wiki_db) -> None:
    prepared = prepare_stage(wiki_db)
    started = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="claim-extraction:1",
        artifact_source_card_revision_id=prepared.card_revision_id,
    )

    committed = commit_stage_run(
        wiki_db,
        stage_run_id=started.stage_run_id,
        outbox_events=[event()],
    )

    assert committed.status == "committed"
    assert committed.commit_seq == 1
    assert committed.applied_against_card_revision_id == prepared.card_revision_id
    pending = list_pending_outbox(wiki_db)
    assert len(pending) == 1
    assert pending[0].stage_run_id == committed.stage_run_id
    assert pending[0].commit_seq == committed.commit_seq


@pytest.mark.parametrize("changed_kind", ["input", "dependency", "contract"])
def test_stage_run_becomes_stale_when_any_bound_head_changes(
    wiki_db,
    changed_kind: str,
) -> None:
    prepared = prepare_stage(wiki_db)
    started = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key=f"stale:{changed_kind}",
    )

    if changed_kind == "input":
        advance_input_head(
            wiki_db,
            source_lineage_id=prepared.lineage_id,
            input_kind="claim_inputs",
            payload={"key_points": ["changed"]},
            observed_card_revision_id=prepared.card_revision_id,
        )
    elif changed_kind == "dependency":
        publish_dependency(
            wiki_db,
            dependency_kind=prepared.dependency.dependency_kind,
            dependency_scope_key=prepared.dependency.dependency_scope_key,
            payload={"candidate_concept_ids": ["concept-b"]},
            expected_version_id=prepared.dependency.dependency_version_id,
            producer_kind="registry",
        )
    else:
        activate_processor_contract(
            wiki_db,
            stage_kind=prepared.stage.stage_kind,
            contract=contract("B"),
        )

    result = commit_stage_run(
        wiki_db,
        stage_run_id=started.stage_run_id,
        outbox_events=[event(f"stale:{changed_kind}")],
    )
    assert result.status == "stale"
    assert result.commit_seq is None
    assert list_pending_outbox(wiki_db) == ()


def test_a_to_b_to_a_hash_reuse_still_stales_old_run(wiki_db) -> None:
    prepared = prepare_stage(wiki_db)
    started = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="a-b-a",
    )
    first_input = prepared.stage.input_bindings[0]
    changed = advance_input_head(
        wiki_db,
        source_lineage_id=prepared.lineage_id,
        input_kind="claim_inputs",
        payload={"key_points": ["claim B"]},
        observed_card_revision_id=prepared.card_revision_id,
    )
    returned = advance_input_head(
        wiki_db,
        source_lineage_id=prepared.lineage_id,
        input_kind="claim_inputs",
        payload={"key_points": ["claim A"]},
        observed_card_revision_id=prepared.card_revision_id,
    )

    assert changed.generation == 2
    assert returned.generation == 3
    assert returned.input_hash == first_input.input_hash
    assert returned.input_version_id != first_input.input_version_id
    assert commit_stage_run(
        wiki_db,
        stage_run_id=started.stage_run_id,
    ).status == "stale"


def test_duplicate_idempotency_attempt_becomes_no_op(wiki_db) -> None:
    prepared = prepare_stage(wiki_db)
    first = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="same-job",
    )
    second = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="same-job",
    )

    committed = commit_stage_run(wiki_db, stage_run_id=first.stage_run_id)
    duplicate = commit_stage_run(wiki_db, stage_run_id=second.stage_run_id)

    assert committed.status == "committed"
    assert duplicate.status == "no_op"
    assert duplicate.commit_seq is None
    assert duplicate.duplicate_of_stage_run_id == committed.stage_run_id
    committed_count = wiki_db.execute(
        """
        SELECT COUNT(*)
        FROM stage_runs
        WHERE idempotency_key = 'same-job' AND status = 'committed'
        """
    ).fetchone()[0]
    assert committed_count == 1


def test_failed_attempt_can_retry_and_commit_same_idempotency_key(wiki_db) -> None:
    prepared = prepare_stage(wiki_db)
    first = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="retryable-job",
    )
    failed = fail_stage_run(
        wiki_db,
        stage_run_id=first.stage_run_id,
        error_text="worker failed",
    )
    retry = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="retryable-job",
    )
    committed = commit_stage_run(wiki_db, stage_run_id=retry.stage_run_id)

    assert failed.status == "failed"
    assert committed.status == "committed"
    assert committed.commit_seq == 1


def test_commit_sequences_are_unique_across_connections(tmp_path) -> None:
    from retrieval.wiki.schema import connect_database

    path = tmp_path / "commit-seq.sqlite"
    first_connection = connect_database(path)
    prepared = prepare_stage(first_connection)
    second_connection = connect_database(path)
    try:
        first_run = start_stage_run(
            first_connection,
            stage_version_id=prepared.stage.stage_version_id,
            idempotency_key="commit-1",
        )
        second_run = start_stage_run(
            second_connection,
            stage_version_id=prepared.stage.stage_version_id,
            idempotency_key="commit-2",
        )
        first_commit = commit_stage_run(
            first_connection,
            stage_run_id=first_run.stage_run_id,
        )
        second_commit = commit_stage_run(
            second_connection,
            stage_run_id=second_run.stage_run_id,
        )
        assert {first_commit.commit_seq, second_commit.commit_seq} == {1, 2}
        assert first_connection.execute(
            """
            SELECT COUNT(DISTINCT commit_seq) = COUNT(*)
            FROM stage_runs
            WHERE status = 'committed'
            """
        ).fetchone()[0] == 1
    finally:
        first_connection.close()
        second_connection.close()


def test_outbox_replay_and_processed_mark_are_idempotent(wiki_db) -> None:
    prepared = prepare_stage(wiki_db)
    first_run = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="outbox-first",
    )
    commit_stage_run(
        wiki_db,
        stage_run_id=first_run.stage_run_id,
        outbox_events=[event(), event()],
    )
    second_run = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="outbox-second",
    )
    second_commit = commit_stage_run(
        wiki_db,
        stage_run_id=second_run.stage_run_id,
        outbox_events=[event()],
    )

    assert second_commit.status == "committed"
    assert wiki_db.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 1
    pending = list_pending_outbox(wiki_db)
    first_mark = mark_outbox_processed(
        wiki_db,
        outbox_event_id=pending[0].outbox_event_id,
    )
    second_mark = mark_outbox_processed(
        wiki_db,
        outbox_event_id=pending[0].outbox_event_id,
    )
    assert first_mark.processed_at is not None
    assert second_mark.processed_at == first_mark.processed_at
    assert list_pending_outbox(wiki_db) == ()


def test_outbox_rejects_non_committed_run(wiki_db) -> None:
    prepared = prepare_stage(wiki_db)
    started = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="not-committed",
    )
    with pytest.raises(sqlite3.IntegrityError, match="committed"):
        wiki_db.execute(
            """
            INSERT INTO outbox_events (
                outbox_event_id,
                event_key,
                stage_run_id,
                commit_seq,
                event_kind,
                aggregate_kind,
                aggregate_key,
                payload_json,
                created_at
            ) VALUES ('event-id', 'event-key', ?, 1, 'kind', 'aggregate', 'key', '{}', 'audit')
            """,
            (started.stage_run_id,),
        )


def test_run_does_not_advance_scheduler_owned_stage_head(wiki_db) -> None:
    prepared = prepare_stage(wiki_db)
    before = wiki_db.execute(
        """
        SELECT current_stage_version_id, current_stage_generation
        FROM lineage_stage_heads
        WHERE source_lineage_id = ? AND stage_kind = ?
        """,
        (prepared.lineage_id, prepared.stage.stage_kind),
    ).fetchone()
    run = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="head-ownership",
    )
    commit_stage_run(wiki_db, stage_run_id=run.stage_run_id)
    after = wiki_db.execute(
        """
        SELECT current_stage_version_id, current_stage_generation
        FROM lineage_stage_heads
        WHERE source_lineage_id = ? AND stage_kind = ?
        """,
        (prepared.lineage_id, prepared.stage.stage_kind),
    ).fetchone()
    assert dict(after) == dict(before)
