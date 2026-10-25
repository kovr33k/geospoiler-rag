from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from models import EnrichedCardV2
from retrieval.wiki import (
    CLAIM_EXTRACTION_STAGE_KIND,
    CLAIM_INPUT_KIND,
    DEFAULT_CLAIM_EXTRACTION_CONTRACT,
    DependencyKind,
    activate_processor_contract,
    apply_lifecycle,
    apply_prepared_extraction,
    build_extraction_artifact,
    ensure_source_lineage,
    fail_stage_run,
    ingest_card,
    ingest_path,
    prepare_card_extraction,
    prepare_lifecycle_apply,
    record_card_revision,
    record_ingested_card,
    schedule_stage,
    start_stage_run,
    store_extraction_artifact,
)
from tests.wiki_v2.test_phase3_cards import enriched_data


@dataclass(frozen=True)
class CustomAttempt:
    lineage_id: str
    card_revision_id: str
    stage_run_id: str
    extraction_run_id: str
    prepared: object


def _custom_attempt(
    connection: sqlite3.Connection,
    *,
    external_key: str,
    claims: list[dict],
    idempotency_key: str,
) -> CustomAttempt:
    lineage = ensure_source_lineage(
        connection,
        source_kind="test",
        external_key=external_key,
    )
    claim_payload = {
        "card_schema": "test_claims_v1",
        "content_type": "test",
        "language": "ru",
        "key_points": claims,
        "theses": [],
        "quotes": [],
        "events": [],
    }
    card = record_card_revision(
        connection,
        source_lineage_id=lineage.source_lineage_id,
        card_payload={"source": external_key, **claim_payload},
        input_payloads={CLAIM_INPUT_KIND: claim_payload},
        card_unordered_collection_paths=(("key_points",),),
        input_unordered_collection_paths={CLAIM_INPUT_KIND: (("key_points",),)},
    )
    activation = activate_processor_contract(
        connection,
        stage_kind=CLAIM_EXTRACTION_STAGE_KIND,
        contract=DEFAULT_CLAIM_EXTRACTION_CONTRACT,
    )
    stage = schedule_stage(
        connection,
        source_lineage_id=lineage.source_lineage_id,
        stage_kind=CLAIM_EXTRACTION_STAGE_KIND,
        input_kinds=[CLAIM_INPUT_KIND],
    )
    claim_head = next(
        head for head in card.input_heads if head.input_kind == CLAIM_INPUT_KIND
    )
    artifact = build_extraction_artifact(
        claim_inputs=claim_payload,
        claim_inputs_hash=claim_head.input_hash,
        processor_contract_version_id=activation.processor_contract_version_id,
        processor_contract_hash=activation.contract_hash,
    )
    store_extraction_artifact(connection, artifact)
    run = start_stage_run(
        connection,
        stage_version_id=stage.stage_version_id,
        idempotency_key=idempotency_key,
        artifact_source_card_revision_id=card.card_revision_id,
    )
    prepared = prepare_lifecycle_apply(
        connection,
        stage_run_id=run.stage_run_id,
        extraction_artifact_id=artifact.extraction_artifact_id,
    )
    return CustomAttempt(
        lineage_id=lineage.source_lineage_id,
        card_revision_id=card.card_revision_id,
        stage_run_id=run.stage_run_id,
        extraction_run_id=prepared.extraction_run_id,
        prepared=prepared,
    )


def test_a_b_a_reactivates_same_occurrence_without_reactivated_status(wiki_db) -> None:
    results = [
        ingest_card(
            wiki_db,
            EnrichedCardV2.model_validate(enriched_data(key_points=[{"text": text}])),
        )
        for text in ("A", "B", "A")
    ]
    first_occurrence = wiki_db.execute(
        """
        SELECT occurrence_version_id
        FROM extraction_run_occurrences
        WHERE extraction_run_id = ?
        """,
        (results[0].lifecycle_result.extraction_run_id,),
    ).fetchone()["occurrence_version_id"]
    third_occurrence = wiki_db.execute(
        """
        SELECT occurrence_version_id
        FROM extraction_run_occurrences
        WHERE extraction_run_id = ?
        """,
        (results[2].lifecycle_result.extraction_run_id,),
    ).fetchone()["occurrence_version_id"]

    assert first_occurrence == third_occurrence
    assert results[2].lifecycle_result.counts.reactivated == 1
    statuses = {
        row["to_status"]
        for row in wiki_db.execute("SELECT to_status FROM occurrence_state_events")
    }
    assert statuses <= {"active", "retired", "superseded"}
    assert (
        wiki_db.execute(
            """
            SELECT status
            FROM occurrence_current_states
            WHERE occurrence_version_id = ?
            """,
            (first_occurrence,),
        ).fetchone()["status"]
        == "active"
    )


def test_unique_exact_external_locator_supersedes(wiki_db) -> None:
    old = _custom_attempt(
        wiki_db,
        external_key="strict-locator",
        claims=[{"text": "Old", "external_locator": "message:1:span:2"}],
        idempotency_key="strict-old",
    )
    assert apply_lifecycle(wiki_db, old.prepared).status == "committed"
    new = _custom_attempt(
        wiki_db,
        external_key="strict-locator",
        claims=[{"text": "New", "external_locator": "message:1:span:2"}],
        idempotency_key="strict-new",
    )
    result = apply_lifecycle(wiki_db, new.prepared)
    assert result.counts.superseded == 1
    assert result.counts.retired == 0
    row = wiki_db.execute(
        """
        SELECT to_status, superseded_by_occurrence_id
        FROM occurrence_state_events
        WHERE extraction_run_id = ? AND to_status = 'superseded'
        """,
        (new.extraction_run_id,),
    ).fetchone()
    assert row["to_status"] == "superseded"
    assert row["superseded_by_occurrence_id"] is not None


def test_duplicate_exact_locator_falls_back_to_retired_and_active(wiki_db) -> None:
    old = _custom_attempt(
        wiki_db,
        external_key="duplicate-locator",
        claims=[
            {"text": "Old A", "external_locator": "same"},
            {"text": "Old B", "external_locator": "same"},
        ],
        idempotency_key="duplicate-old",
    )
    apply_lifecycle(wiki_db, old.prepared)
    new = _custom_attempt(
        wiki_db,
        external_key="duplicate-locator",
        claims=[
            {"text": "New A", "external_locator": "same"},
            {"text": "New B", "external_locator": "same"},
        ],
        idempotency_key="duplicate-new",
    )
    result = apply_lifecycle(wiki_db, new.prepared)
    assert result.counts == type(result.counts)(
        active=2,
        retired=2,
        superseded=0,
        reactivated=0,
    )


def test_unchanged_duplicate_locator_prevents_false_supersede(wiki_db) -> None:
    old = _custom_attempt(
        wiki_db,
        external_key="partly-unchanged-duplicate",
        claims=[
            {"text": "Stable", "external_locator": "same"},
            {"text": "Old", "external_locator": "same"},
        ],
        idempotency_key="partly-unchanged-old",
    )
    apply_lifecycle(wiki_db, old.prepared)
    new = _custom_attempt(
        wiki_db,
        external_key="partly-unchanged-duplicate",
        claims=[
            {"text": "Stable", "external_locator": "same"},
            {"text": "New", "external_locator": "same"},
        ],
        idempotency_key="partly-unchanged-new",
    )
    result = apply_lifecycle(wiki_db, new.prepared)
    assert result.counts.retired == 1
    assert result.counts.active == 1
    assert result.counts.superseded == 0


def test_failed_stale_and_no_op_attempts_create_no_domain_rows(wiki_db) -> None:
    failed = _custom_attempt(
        wiki_db,
        external_key="failed-attempt",
        claims=[{"text": "A"}],
        idempotency_key="failed-key",
    )
    fail_stage_run(
        wiki_db,
        stage_run_id=failed.stage_run_id,
        error_text="expected failure",
    )
    assert apply_lifecycle(wiki_db, failed.prepared).status == "failed"
    assert (
        wiki_db.execute(
            "SELECT COUNT(*) FROM extraction_runs WHERE stage_run_id = ?",
            (failed.stage_run_id,),
        ).fetchone()[0]
        == 0
    )

    stale = _custom_attempt(
        wiki_db,
        external_key="stale-attempt",
        claims=[{"text": "A"}],
        idempotency_key="stale-A",
    )
    newer = _custom_attempt(
        wiki_db,
        external_key="stale-attempt",
        claims=[{"text": "B"}],
        idempotency_key="stale-B",
    )
    assert apply_lifecycle(wiki_db, stale.prepared).status == "stale"
    assert (
        wiki_db.execute(
            "SELECT COUNT(*) FROM extraction_runs WHERE stage_run_id = ?",
            (stale.stage_run_id,),
        ).fetchone()[0]
        == 0
    )

    first = apply_lifecycle(wiki_db, newer.prepared)
    assert first.status == "committed"
    duplicate = _custom_attempt(
        wiki_db,
        external_key="stale-attempt",
        claims=[{"text": "B"}],
        idempotency_key="stale-B",
    )
    assert apply_lifecycle(wiki_db, duplicate.prepared).status == "no_op"
    assert (
        wiki_db.execute(
            "SELECT COUNT(*) FROM extraction_runs WHERE stage_run_id = ?",
            (duplicate.stage_run_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        wiki_db.execute(
            """
            SELECT COUNT(*)
            FROM occurrence_state_events
            WHERE extraction_run_id IN (?, ?, ?)
            """,
            (
                failed.extraction_run_id,
                stale.extraction_run_id,
                duplicate.extraction_run_id,
            ),
        ).fetchone()[0]
        == 0
    )


def test_summary_change_during_run_applies_to_new_card_with_same_claim_input(wiki_db) -> None:
    initial = record_ingested_card(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data(summary="Old summary")),
    )
    prepared = prepare_card_extraction(wiki_db, initial)
    newer = record_ingested_card(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data(summary="New summary")),
    )
    assert (
        next(
            head
            for head in initial.card_revision.input_heads
            if head.input_kind == CLAIM_INPUT_KIND
        ).generation
        == next(
            head
            for head in newer.card_revision.input_heads
            if head.input_kind == CLAIM_INPUT_KIND
        ).generation
    )

    result = apply_prepared_extraction(wiki_db, prepared)
    assert result.status == "committed"
    assert (
        result.applied_against_card_revision_id
        == newer.card_revision.card_revision_id
    )
    audit = wiki_db.execute(
        """
        SELECT
            artifact_source_card_revision_id,
            applied_against_card_revision_id
        FROM extraction_runs
        WHERE extraction_run_id = ?
        """,
        (result.extraction_run_id,),
    ).fetchone()
    assert audit["artifact_source_card_revision_id"] == initial.card_revision.card_revision_id
    assert audit["applied_against_card_revision_id"] == newer.card_revision.card_revision_id
    assert (
        wiki_db.execute(
            """
            SELECT card_revision_id
            FROM claim_occurrences
            WHERE extraction_run_id = ?
            """,
            (result.extraction_run_id,),
        ).fetchone()["card_revision_id"]
        == initial.card_revision.card_revision_id
    )


def test_claim_input_change_during_run_makes_attempt_stale(wiki_db) -> None:
    initial = record_ingested_card(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data(key_points=[{"text": "A"}])),
    )
    prepared = prepare_card_extraction(wiki_db, initial)
    changed = record_ingested_card(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data(key_points=[{"text": "B"}])),
    )
    prepare_card_extraction(wiki_db, changed, idempotency_key="new-generation-started")
    result = apply_prepared_extraction(wiki_db, prepared)
    assert result.status == "stale"
    assert (
        wiki_db.execute(
            "SELECT COUNT(*) FROM extraction_runs WHERE stage_run_id = ?",
            (prepared.stage_run.stage_run_id,),
        ).fetchone()[0]
        == 0
    )


def test_injected_event_failure_rolls_back_commit_and_all_domain_rows(wiki_db) -> None:
    attempt = _custom_attempt(
        wiki_db,
        external_key="rollback",
        claims=[{"text": "A"}],
        idempotency_key="rollback-attempt",
    )

    def inject_failure() -> None:
        raise RuntimeError("injected event failure")

    with pytest.raises(RuntimeError, match="injected event failure"):
        apply_lifecycle(
            wiki_db,
            attempt.prepared,
            before_event_insert=inject_failure,
        )
    run = wiki_db.execute(
        "SELECT status, commit_seq FROM stage_runs WHERE stage_run_id = ?",
        (attempt.stage_run_id,),
    ).fetchone()
    assert tuple(run) == ("started", None)
    assert (
        wiki_db.execute(
            "SELECT COUNT(*) FROM extraction_runs WHERE extraction_run_id = ?",
            (attempt.extraction_run_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        wiki_db.execute(
            "SELECT COUNT(*) FROM claim_occurrences WHERE source_lineage_id = ?",
            (attempt.lineage_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        wiki_db.execute(
            """
            SELECT COUNT(*)
            FROM dependency_heads
            WHERE dependency_kind = ? AND dependency_scope_key = ?
            """,
            (DependencyKind.OCCURRENCE_SNAPSHOT, attempt.lineage_id),
        ).fetchone()[0]
        == 0
    )
    assert apply_lifecycle(wiki_db, attempt.prepared).status == "committed"


def test_eligibility_deactivates_and_reactivates_without_extraction_rerun(wiki_db) -> None:
    eligible = ingest_card(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data()),
    )
    lineage_id = eligible.recorded_card.lineage.source_lineage_id
    stage_run_count = wiki_db.execute("SELECT COUNT(*) FROM stage_runs").fetchone()[0]
    event_count = wiki_db.execute(
        "SELECT COUNT(*) FROM occurrence_state_events"
    ).fetchone()[0]
    occurrence_ids = tuple(
        row["occurrence_version_id"]
        for row in wiki_db.execute(
            "SELECT occurrence_version_id FROM effective_active_occurrences"
        )
    )
    assert occurrence_ids

    ineligible = ingest_card(
        wiki_db,
        EnrichedCardV2.model_validate(
            enriched_data(quality_flags=["extraction_unstable"])
        ),
    )
    assert ineligible.extraction_status == "no_op"
    assert (
        wiki_db.execute(
            """
            SELECT COUNT(*)
            FROM effective_active_occurrences
            WHERE source_lineage_id = ?
            """,
            (lineage_id,),
        ).fetchone()[0]
        == 0
    )
    restored = ingest_card(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data()),
    )
    assert restored.extraction_status == "no_op"
    restored_ids = tuple(
        row["occurrence_version_id"]
        for row in wiki_db.execute(
            """
            SELECT occurrence_version_id
            FROM effective_active_occurrences
            WHERE source_lineage_id = ?
            """,
            (lineage_id,),
        )
    )
    assert restored_ids == occurrence_ids
    assert wiki_db.execute("SELECT COUNT(*) FROM stage_runs").fetchone()[0] == stage_run_count
    assert (
        wiki_db.execute("SELECT COUNT(*) FROM occurrence_state_events").fetchone()[0]
        == event_count
    )
    assert (
        wiki_db.execute(
            """
            SELECT current_eligibility_generation
            FROM eligibility_heads
            WHERE source_lineage_id = ?
            """,
            (lineage_id,),
        ).fetchone()[0]
        == 3
    )


def test_eligibility_history_is_immutable_and_requires_exact_card_binding(wiki_db) -> None:
    result = ingest_card(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data()),
    )
    evaluation = result.recorded_card.eligibility
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        wiki_db.execute(
            """
            UPDATE eligibility_evaluation_versions
            SET reasons_json = '[]'
            WHERE eligibility_evaluation_id = ?
            """,
            (evaluation.eligibility_evaluation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="current CAS snapshot"):
        wiki_db.execute(
            """
            INSERT INTO eligibility_evaluation_versions (
                eligibility_evaluation_id,
                source_lineage_id,
                evaluated_card_revision_id,
                eligibility_generation,
                eligibility_input_kind,
                eligibility_input_version_id,
                eligibility_input_generation,
                eligibility_inputs_hash,
                stage_kind,
                processor_contract_activation_generation,
                processor_contract_version_id,
                processor_contract_hash,
                eligible,
                reasons_json,
                evaluation_hash,
                created_at
            )
            SELECT
                'bad-evaluation',
                source_lineage_id,
                evaluated_card_revision_id,
                eligibility_generation + 1,
                eligibility_input_kind,
                eligibility_input_version_id,
                eligibility_input_generation,
                'wrong-hash',
                stage_kind,
                processor_contract_activation_generation,
                processor_contract_version_id,
                processor_contract_hash,
                eligible,
                reasons_json,
                'bad-hash',
                'audit'
            FROM eligibility_evaluation_versions
            WHERE eligibility_evaluation_id = ?
            """,
            (evaluation.eligibility_evaluation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="generation must increase"):
        wiki_db.execute(
            """
            UPDATE eligibility_heads
            SET current_eligibility_generation = current_eligibility_generation
            WHERE source_lineage_id = ?
            """,
            (result.recorded_card.lineage.source_lineage_id,),
        )


def test_artifact_is_reused_across_lineages_with_same_claim_inputs(wiki_db) -> None:
    for source_id in ("telegram:reuse:1", "telegram:reuse:2"):
        result = ingest_card(
            wiki_db,
            EnrichedCardV2.model_validate(enriched_data(source_id=source_id)),
        )
        assert result.extraction_status == "committed"
    assert wiki_db.execute("SELECT COUNT(*) FROM extraction_artifacts").fetchone()[0] == 1
    assert wiki_db.execute("SELECT COUNT(*) FROM source_lineages").fetchone()[0] == 2
    assert wiki_db.execute("SELECT COUNT(*) FROM claim_occurrences").fetchone()[0] == 2


def test_directory_ingest_continues_after_bad_file_and_bad_array_is_atomic(
    wiki_db,
    tmp_path,
) -> None:
    (tmp_path / "valid.json").write_text(
        EnrichedCardV2.model_validate(
            enriched_data(source_id="telegram:directory:valid")
        ).model_dump_json(),
        encoding="utf-8",
    )
    (tmp_path / "invalid.json").write_text("{broken", encoding="utf-8")
    bad_array = [
        enriched_data(source_id="telegram:directory:must-not-ingest"),
        {"schema_version": "not-supported"},
    ]
    (tmp_path / "invalid-array.json").write_text(
        __import__("json").dumps(bad_array),
        encoding="utf-8",
    )

    stats = ingest_path(wiki_db, tmp_path)
    assert stats.files_seen == 3
    assert stats.files_valid == 1
    assert stats.files_invalid == 2
    assert stats.extraction_runs_committed == 1
    assert len(stats.errors) == 2
    source_keys = {
        row["external_key"]
        for row in wiki_db.execute("SELECT external_key FROM source_lineages")
    }
    assert source_keys == {"telegram:directory:valid"}


def test_populated_phase3_database_integrity(wiki_db) -> None:
    ingest_card(
        wiki_db,
        EnrichedCardV2.model_validate(
            enriched_data(
                key_points=[{"text": "A"}, {"text": "B"}],
                quality_flags=[],
            )
        ),
    )
    assert wiki_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert wiki_db.execute("PRAGMA foreign_key_check").fetchall() == []
