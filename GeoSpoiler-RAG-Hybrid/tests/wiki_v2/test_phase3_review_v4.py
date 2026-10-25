from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import replace

import pytest

from models import EnrichedCardV2
from retrieval.wiki import (
    DEFAULT_ELIGIBILITY_CONTRACT,
    DependencyKind,
    adapt_card,
    apply_lifecycle,
    apply_prepared_eligibility,
    build_occurrence_blueprints,
    connect_database,
    evaluate_eligibility,
    fail_stage_run,
    ingest_card,
    ingest_path,
    prepare_eligibility_evaluation,
    record_card_revision,
)
from tests.wiki_v2.test_phase3_cards import (
    enriched_data,
    youtube_segment_data,
)
from tests.wiki_v2.test_phase3_lifecycle import _custom_attempt


def _record_adapted_without_eligibility(
    connection: sqlite3.Connection,
    card: EnrichedCardV2,
):
    adapted = adapt_card(card)
    return record_card_revision(
        connection,
        source_lineage_id=adapted.source_lineage_id,
        card_payload=adapted.card_payload,
        input_payloads=adapted.input_payloads,
        card_revision_id=adapted.card_revision_id,
        card_unordered_collection_paths=adapted.card_unordered_collection_paths,
        card_exact_quote_paths=adapted.card_exact_quote_paths,
        input_unordered_collection_paths=adapted.input_unordered_collection_paths,
        input_exact_quote_paths=adapted.input_exact_quote_paths,
    )


def test_two_connection_stale_eligibility_cannot_replace_current_b(tmp_path) -> None:
    path = tmp_path / "eligibility-race.sqlite"
    first_connection = connect_database(path)
    second_connection = connect_database(path)
    try:
        initial = ingest_card(
            first_connection,
            EnrichedCardV2.model_validate(
                enriched_data(summary="A", quality_flags=[])
            ),
        )
        stale_a = prepare_eligibility_evaluation(
            first_connection,
            source_lineage_id=initial.recorded_card.lineage.source_lineage_id,
        )

        card_b = EnrichedCardV2.model_validate(
            enriched_data(
                summary="B",
                quality_flags=["extraction_unstable"],
            )
        )
        revision_b = _record_adapted_without_eligibility(
            second_connection,
            card_b,
        )
        assert (
            second_connection.execute(
                "SELECT COUNT(*) FROM effective_active_occurrences"
            ).fetchone()[0]
            == 0
        )
        evaluation_b = evaluate_eligibility(
            second_connection,
            source_lineage_id=initial.recorded_card.lineage.source_lineage_id,
            card_revision_id=revision_b.card_revision_id,
        )
        assert evaluation_b.status == "published"
        assert not evaluation_b.eligible
        head_before = tuple(
            second_connection.execute(
                """
                SELECT
                    current_eligibility_evaluation_id,
                    current_eligibility_generation,
                    evaluated_card_revision_id
                FROM eligibility_heads
                """
            ).fetchone()
        )

        stale_result = apply_prepared_eligibility(first_connection, stale_a)
        assert stale_result.status == "stale"
        assert not stale_result.changed
        head_after = tuple(
            first_connection.execute(
                """
                SELECT
                    current_eligibility_evaluation_id,
                    current_eligibility_generation,
                    evaluated_card_revision_id
                FROM eligibility_heads
                """
            ).fetchone()
        )
        assert head_after == head_before
        assert head_after[2] == revision_b.card_revision_id
        assert (
            first_connection.execute(
                "SELECT COUNT(*) FROM effective_active_occurrences"
            ).fetchone()[0]
            == 0
        )
    finally:
        first_connection.close()
        second_connection.close()


def test_direct_sql_cannot_move_eligibility_head_to_stale_card(wiki_db) -> None:
    initial = ingest_card(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data(summary="A")),
    )
    evaluation = initial.recorded_card.eligibility
    lineage_id = initial.recorded_card.lineage.source_lineage_id
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
            'stale-manual-evaluation',
            source_lineage_id,
            evaluated_card_revision_id,
            2,
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
            'stale-manual-evaluation-hash',
            'audit'
        FROM eligibility_evaluation_versions
        WHERE eligibility_evaluation_id = ?
        """,
        (evaluation.eligibility_evaluation_id,),
    )

    revision_b = _record_adapted_without_eligibility(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data(summary="B")),
    )
    assert revision_b.card_revision_id != evaluation.evaluated_card_revision_id
    assert (
        wiki_db.execute(
            "SELECT COUNT(*) FROM effective_active_occurrences"
        ).fetchone()[0]
        == 0
    )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="current card, input, and contract snapshot",
    ):
        wiki_db.execute(
            """
            UPDATE eligibility_heads
            SET current_eligibility_evaluation_id = 'stale-manual-evaluation',
                current_eligibility_generation = 2,
                updated_at = 'manual'
            WHERE source_lineage_id = ?
            """,
            (lineage_id,),
        )
    head = wiki_db.execute(
        """
        SELECT
            current_eligibility_evaluation_id,
            current_eligibility_generation,
            evaluated_card_revision_id
        FROM eligibility_heads
        WHERE source_lineage_id = ?
        """,
        (lineage_id,),
    ).fetchone()
    assert tuple(head) == (
        evaluation.eligibility_evaluation_id,
        1,
        evaluation.evaluated_card_revision_id,
    )


def test_policy_only_eligibility_upgrade_republishes_without_card_or_extraction(
    wiki_db,
) -> None:
    initial = ingest_card(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data()),
    )
    lineage_id = initial.recorded_card.lineage.source_lineage_id
    card_head_before = tuple(
        wiki_db.execute(
            """
            SELECT current_card_revision_id, card_head_generation
            FROM source_lineage_heads
            WHERE source_lineage_id = ?
            """,
            (lineage_id,),
        ).fetchone()
    )
    input_before = tuple(
        wiki_db.execute(
            """
            SELECT
                current_input_version_id,
                current_input_generation,
                current_input_hash,
                version.canonical_payload_json
            FROM lineage_input_heads AS head
            JOIN lineage_input_versions AS version
              ON version.input_version_id = head.current_input_version_id
            WHERE head.source_lineage_id = ?
              AND head.input_kind = 'eligibility_inputs'
            """,
            (lineage_id,),
        ).fetchone()
    )
    assert "policy" not in json.loads(input_before[3])
    domain_counts_before = tuple(
        wiki_db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM stage_runs),
                (SELECT COUNT(*) FROM extraction_runs),
                (SELECT COUNT(*) FROM occurrence_state_events),
                (SELECT COUNT(*) FROM card_revisions)
            """
        ).fetchone()
    )

    upgraded_contract = replace(
        DEFAULT_ELIGIBILITY_CONTRACT,
        policy_version="wiki-eligibility-policy-v2",
    )
    upgraded = evaluate_eligibility(
        wiki_db,
        source_lineage_id=lineage_id,
        card_revision_id=card_head_before[0],
        contract=upgraded_contract,
    )

    assert upgraded.status == "published"
    assert upgraded.changed
    assert upgraded.generation == initial.recorded_card.eligibility.generation + 1
    assert (
        upgraded.dependency_generation
        == initial.recorded_card.eligibility.dependency_generation + 1
    )
    assert (
        upgraded.processor_contract_version_id
        != initial.recorded_card.eligibility.processor_contract_version_id
    )
    assert tuple(
        wiki_db.execute(
            """
            SELECT current_card_revision_id, card_head_generation
            FROM source_lineage_heads
            WHERE source_lineage_id = ?
            """,
            (lineage_id,),
        ).fetchone()
    ) == card_head_before
    input_after = tuple(
        wiki_db.execute(
            """
            SELECT current_input_version_id, current_input_generation, current_input_hash
            FROM lineage_input_heads
            WHERE source_lineage_id = ? AND input_kind = 'eligibility_inputs'
            """,
            (lineage_id,),
        ).fetchone()
    )
    assert input_after == input_before[:3]
    assert tuple(
        wiki_db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM stage_runs),
                (SELECT COUNT(*) FROM extraction_runs),
                (SELECT COUNT(*) FROM occurrence_state_events),
                (SELECT COUNT(*) FROM card_revisions)
            """
        ).fetchone()
    ) == domain_counts_before
    assert (
        wiki_db.execute(
            "SELECT COUNT(*) FROM effective_active_occurrences"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    "locator_hint",
    [
        {"source_span": [10, 5]},
        {"source_span": {"start": 1, "end": 2, "approximate": True}},
        {"source_span": {"start": 1, "end": 2, "exact": False}},
        {"timestamp": True},
        {"timestamp": -1},
        {"timestamp": ""},
        {"timestamp": "not-a-timestamp"},
        {"timestamp": {"seconds": 10}},
    ],
)
def test_invalid_exact_locator_candidates_retire_never_supersede(
    wiki_db,
    locator_hint,
) -> None:
    old = _custom_attempt(
        wiki_db,
        external_key="invalid-locator",
        claims=[{"text": "Old", **locator_hint}],
        idempotency_key="invalid-old",
    )
    apply_lifecycle(wiki_db, old.prepared)
    new = _custom_attempt(
        wiki_db,
        external_key="invalid-locator",
        claims=[{"text": "New", **locator_hint}],
        idempotency_key="invalid-new",
    )
    result = apply_lifecycle(wiki_db, new.prepared)
    assert result.counts.retired == 1
    assert result.counts.active == 1
    assert result.counts.superseded == 0
    assert all(
        item.blueprint.locator["locator_kind"] == "content_fingerprint"
        for item in new.prepared.occurrences
    )


@pytest.mark.parametrize(
    "locator_hint",
    [
        {"source_span": [1, 2]},
        {"source_span": {"start": 1.5, "end": 2.5, "exact": True}},
    ],
)
def test_valid_exact_span_supersedes(wiki_db, locator_hint) -> None:
    suffix = json.dumps(locator_hint, sort_keys=True)
    old = _custom_attempt(
        wiki_db,
        external_key=f"valid-span-{suffix}",
        claims=[{"text": "Old", **locator_hint}],
        idempotency_key=f"valid-span-old-{suffix}",
    )
    apply_lifecycle(wiki_db, old.prepared)
    new = _custom_attempt(
        wiki_db,
        external_key=f"valid-span-{suffix}",
        claims=[{"text": "New", **locator_hint}],
        idempotency_key=f"valid-span-new-{suffix}",
    )
    result = apply_lifecycle(wiki_db, new.prepared)
    assert result.counts.superseded == 1
    assert result.counts.retired == 0


@pytest.mark.parametrize(
    ("value", "is_exact"),
    [
        (0, True),
        (1.5, True),
        ("01:02", True),
        ("01:02:03.5", True),
        ("2026-07-30T10:20:30Z", True),
        (False, False),
        (math.nan, False),
        (math.inf, False),
        ("1:2", False),
        ("2026-99-99T10:20:30Z", False),
    ],
)
def test_timestamp_validation_is_strict(value, is_exact) -> None:
    item = build_occurrence_blueprints(
        {
            "key_points": [{"text": "Claim", "timestamp": value}],
            "theses": [],
            "quotes": [],
            "events": [],
        }
    )[0]
    assert item.has_exact_external_locator is is_exact


def test_direct_sql_rejects_second_root_and_branched_successor(wiki_db) -> None:
    initial = _custom_attempt(
        wiki_db,
        external_key="event-chain-guards",
        claims=[{"text": "Stable"}],
        idempotency_key="event-chain-initial",
    )
    apply_lifecycle(wiki_db, initial.prepared)
    occurrence_id = initial.prepared.occurrences[0].occurrence_version_id
    root_event_id = wiki_db.execute(
        """
        SELECT state_event_id
        FROM occurrence_state_events
        WHERE occurrence_version_id = ?
        """,
        (occurrence_id,),
    ).fetchone()["state_event_id"]

    second = _custom_attempt(
        wiki_db,
        external_key="event-chain-guards",
        claims=[{"text": "Stable"}],
        idempotency_key="event-chain-second",
    )
    apply_lifecycle(wiki_db, second.prepared)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        wiki_db.execute(
            """
            INSERT INTO occurrence_state_events (
                state_event_id,
                occurrence_version_id,
                extraction_run_id,
                source_lineage_id,
                previous_state_event_id,
                to_status,
                created_at
            ) VALUES ('second-root', ?, ?, ?, NULL, 'active', 'audit')
            """,
            (occurrence_id, second.extraction_run_id, second.lineage_id),
        )

    branch = _custom_attempt(
        wiki_db,
        external_key="event-chain-guards",
        claims=[{"text": "Stable"}],
        idempotency_key="event-chain-branch",
    )
    apply_lifecycle(wiki_db, branch.prepared)
    wiki_db.execute(
        """
        INSERT INTO occurrence_state_events (
            state_event_id,
            occurrence_version_id,
            extraction_run_id,
            source_lineage_id,
            previous_state_event_id,
            to_status,
            created_at
        ) VALUES ('branch-one', ?, ?, ?, ?, 'active', 'audit')
        """,
        (
            occurrence_id,
            branch.extraction_run_id,
            branch.lineage_id,
            root_event_id,
        ),
    )

    competing = _custom_attempt(
        wiki_db,
        external_key="event-chain-guards",
        claims=[{"text": "Stable"}],
        idempotency_key="event-chain-competing",
    )
    apply_lifecycle(wiki_db, competing.prepared)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        wiki_db.execute(
            """
            INSERT INTO occurrence_state_events (
                state_event_id,
                occurrence_version_id,
                extraction_run_id,
                source_lineage_id,
                previous_state_event_id,
                to_status,
                created_at
            ) VALUES ('branch-two', ?, ?, ?, ?, 'active', 'audit')
            """,
            (
                occurrence_id,
                competing.extraction_run_id,
                competing.lineage_id,
                root_event_id,
            ),
        )


def test_failure_after_state_events_before_outbox_rolls_back_everything(wiki_db) -> None:
    attempt = _custom_attempt(
        wiki_db,
        external_key="post-event-rollback",
        claims=[{"text": "A"}],
        idempotency_key="post-event-rollback",
    )

    def fail_after_events() -> None:
        assert (
            wiki_db.execute(
                """
                SELECT COUNT(*)
                FROM occurrence_state_events
                WHERE extraction_run_id = ?
                """,
                (attempt.extraction_run_id,),
            ).fetchone()[0]
            == 1
        )
        raise RuntimeError("after-state-event")

    with pytest.raises(RuntimeError, match="after-state-event"):
        apply_lifecycle(
            wiki_db,
            attempt.prepared,
            after_state_event_insert=fail_after_events,
        )
    assert tuple(
        wiki_db.execute(
            "SELECT status, commit_seq FROM stage_runs WHERE stage_run_id = ?",
            (attempt.stage_run_id,),
        ).fetchone()
    ) == ("started", None)
    for table, column, value in (
        ("extraction_runs", "extraction_run_id", attempt.extraction_run_id),
        ("claim_occurrences", "source_lineage_id", attempt.lineage_id),
        ("occurrence_state_events", "extraction_run_id", attempt.extraction_run_id),
        ("outbox_events", "stage_run_id", attempt.stage_run_id),
    ):
        assert (
            wiki_db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
                (value,),
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


def test_failed_attempt_retry_same_idempotency_commits_exactly_once(wiki_db) -> None:
    first = _custom_attempt(
        wiki_db,
        external_key="failed-retry",
        claims=[{"text": "A"}],
        idempotency_key="retry-key",
    )
    fail_stage_run(
        wiki_db,
        stage_run_id=first.stage_run_id,
        error_text="injected worker failure",
    )
    second = _custom_attempt(
        wiki_db,
        external_key="failed-retry",
        claims=[{"text": "A"}],
        idempotency_key="retry-key",
    )
    assert apply_lifecycle(wiki_db, second.prepared).status == "committed"
    third = _custom_attempt(
        wiki_db,
        external_key="failed-retry",
        claims=[{"text": "A"}],
        idempotency_key="retry-key",
    )
    assert apply_lifecycle(wiki_db, third.prepared).status == "no_op"
    statuses = [
        row["status"]
        for row in wiki_db.execute(
            """
            SELECT status
            FROM stage_runs
            WHERE idempotency_key = ?
            ORDER BY started_at
            """,
            ("retry-key",),
        )
    ]
    assert sorted(statuses) == ["committed", "failed", "no_op"]
    assert (
        wiki_db.execute(
            """
            SELECT COUNT(*)
            FROM extraction_runs AS extraction
            JOIN stage_runs AS run ON run.stage_run_id = extraction.stage_run_id
            WHERE run.idempotency_key = ?
            """,
            ("retry-key",),
        ).fetchone()[0]
        == 1
    )


def test_duplicate_card_in_array_and_across_files_is_deterministic_no_op(
    wiki_db,
    tmp_path,
) -> None:
    card = enriched_data(source_id="telegram:duplicate-file-card")
    (tmp_path / "a.json").write_text(
        json.dumps([card, card]),
        encoding="utf-8",
    )
    (tmp_path / "b.json").write_text(json.dumps(card), encoding="utf-8")
    stats = ingest_path(wiki_db, tmp_path)
    assert stats.extraction_runs_committed == 1
    assert stats.extraction_runs_no_op == 2
    assert stats.errors == ()
    for table, expected in (
        ("source_lineages", 1),
        ("card_revisions", 1),
        ("stage_runs", 1),
        ("extraction_runs", 1),
        ("claim_occurrences", 1),
        ("occurrence_state_events", 1),
    ):
        assert wiki_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected


def test_mixed_enriched_and_youtube_segment_array(wiki_db, tmp_path) -> None:
    payload = [
        youtube_segment_data(segment_id="youtube:mixed:segment:0"),
        enriched_data(source_id="telegram:mixed:1"),
    ]
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    stats = ingest_path(wiki_db, path)
    assert stats.files_seen == 1
    assert stats.files_valid == 1
    assert stats.files_invalid == 0
    assert stats.extraction_runs_committed == 2
    assert {
        row["source_kind"]
        for row in wiki_db.execute("SELECT source_kind FROM source_lineages")
    } == {"telegram", "youtube_segment"}


def test_conflicting_revisions_follow_sorted_file_order_without_corruption(
    tmp_path,
) -> None:
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    source_id = "telegram:conflicting-revisions"
    card_a = enriched_data(source_id=source_id, summary="A")
    card_b = enriched_data(source_id=source_id, summary="B")
    (cards_dir / "01-a.json").write_text(json.dumps(card_a), encoding="utf-8")
    (cards_dir / "02-b.json").write_text(json.dumps(card_b), encoding="utf-8")
    expected_b = adapt_card(EnrichedCardV2.model_validate(card_b)).card_revision_id

    final_heads: list[str] = []
    for ordinal in range(2):
        connection = connect_database(tmp_path / f"conflict-{ordinal}.sqlite")
        try:
            stats = ingest_path(connection, cards_dir)
            assert stats.errors == ()
            final_heads.append(
                connection.execute(
                    "SELECT current_card_revision_id FROM source_lineage_heads"
                ).fetchone()[0]
            )
            assert connection.execute(
                "SELECT COUNT(*) FROM source_lineages"
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM card_revisions"
            ).fetchone()[0] == 2
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            connection.close()
    assert final_heads == [expected_b, expected_b]


def test_extraction_artifacts_contain_no_source_or_lineage_metadata(wiki_db) -> None:
    telegram_source = "telegram:artifact-secret"
    segment_id = "youtube:artifact-secret:segment:7"
    ingest_card(
        wiki_db,
        EnrichedCardV2.model_validate(enriched_data(source_id=telegram_source)),
    )
    from models import YouTubeSegmentCardV2

    ingest_card(
        wiki_db,
        YouTubeSegmentCardV2.model_validate(
            youtube_segment_data(segment_id=segment_id)
        ),
    )
    serialized = "\n".join(
        value
        for row in wiki_db.execute(
            """
            SELECT artifact_json, '' AS exact_payload_json, '' AS locator_json,
                   '' AS evidence_metadata_json
            FROM extraction_artifacts
            UNION ALL
            SELECT '', exact_payload_json, locator_json, evidence_metadata_json
            FROM extraction_artifact_items
            """
        )
        for value in tuple(row)
    )
    for forbidden in (
        telegram_source,
        segment_id,
        "youtube:video-1",
        "source_lineage_id",
        "card_revision_id",
        "normalized_path",
        "segment_id",
        "parent_source_id",
        "start_seconds",
        "transcript_text",
    ):
        assert forbidden not in serialized
