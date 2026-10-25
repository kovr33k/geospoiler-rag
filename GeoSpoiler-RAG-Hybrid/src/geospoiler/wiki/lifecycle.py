"""Atomic application of extraction artifacts to immutable claim occurrences."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from geospoiler.wiki.extraction import (
    OCCURRENCE_SCHEMA_VERSION,
    ExtractionArtifact,
    OccurrenceBlueprint,
    load_extraction_artifact,
)
from geospoiler.wiki.hashing import canonical_json, content_hash
from geospoiler.wiki.schema import CLAIM_EXTRACTION_STAGE_KIND
from geospoiler.wiki.state import (
    DependencyKind,
    StageRun,
    StateConflictError,
    _immediate_transaction,
    _load_stage_run,
    _load_stage_version,
    _new_id,
    _stage_snapshot_is_current,
    _utc_now,
)

LifecycleStatus = Literal["committed", "failed", "stale", "no_op", "started"]


@dataclass(frozen=True)
class PreparedOccurrence:
    occurrence_version_id: str
    blueprint: OccurrenceBlueprint


@dataclass(frozen=True)
class PreparedLifecycleApply:
    stage_run_id: str
    stage_version_id: str
    source_lineage_id: str
    processor_contract_version_id: str
    claim_inputs_hash: str
    artifact_source_card_revision_id: str
    extraction_artifact: ExtractionArtifact
    extraction_run_id: str
    occurrences: tuple[PreparedOccurrence, ...]


@dataclass(frozen=True)
class LifecycleCounts:
    active: int = 0
    retired: int = 0
    superseded: int = 0
    reactivated: int = 0


@dataclass(frozen=True)
class LifecycleApplyResult:
    status: LifecycleStatus
    stage_run: StageRun
    extraction_run_id: str | None
    applied_against_card_revision_id: str | None
    counts: LifecycleCounts


def occurrence_version_id(
    *,
    source_lineage_id: str,
    blueprint: OccurrenceBlueprint,
) -> str:
    """Create occurrence identity without the card-wide claim input hash."""
    identity = {
        "source_lineage_id": source_lineage_id,
        "field_kind": blueprint.field_kind,
        "stable_locator": blueprint.locator,
        "exact_payload_hash": blueprint.exact_payload_hash,
        "occurrence_schema_version": OCCURRENCE_SCHEMA_VERSION,
    }
    digest = content_hash(
        identity,
        namespace="wiki-occurrence-version:v1",
    ).removeprefix("sha256:")
    return f"occurrence:v1:sha256:{digest}"


def prepare_lifecycle_apply(
    connection: sqlite3.Connection,
    *,
    stage_run_id: str,
    extraction_artifact_id: str,
) -> PreparedLifecycleApply:
    """Prepare occurrence identities outside the final write transaction."""
    run = _load_stage_run(connection, stage_run_id)
    if run.stage_kind != CLAIM_EXTRACTION_STAGE_KIND:
        raise StateConflictError("Occurrence lifecycle requires a claim_extraction stage run")
    if run.artifact_source_card_revision_id is None:
        raise StateConflictError("Extraction stage run requires artifact_source_card_revision_id")
    artifact = load_extraction_artifact(
        connection,
        extraction_artifact_id=extraction_artifact_id,
    )
    stage = _load_stage_version(connection, run.stage_version_id, changed=False)
    claim_bindings = [
        binding for binding in stage.input_bindings if binding.input_kind == "claim_inputs"
    ]
    if len(claim_bindings) != 1:
        raise StateConflictError("claim_extraction stage must bind exactly one claim_inputs")
    claim_inputs_hash = claim_bindings[0].input_hash
    if (
        artifact.processor_contract_version_id != run.processor_contract_version_id
        or artifact.processor_contract_hash != stage.processor_contract_hash
        or artifact.claim_inputs_hash != claim_inputs_hash
    ):
        raise StateConflictError("Extraction artifact does not match stage contract/claim inputs")

    extraction_digest = content_hash(
        {
            "stage_run_id": stage_run_id,
            "extraction_artifact_id": extraction_artifact_id,
            "source_lineage_id": run.source_lineage_id,
        },
        namespace="wiki-extraction-run:v1",
    ).removeprefix("sha256:")
    occurrences = tuple(
        PreparedOccurrence(
            occurrence_version_id=occurrence_version_id(
                source_lineage_id=run.source_lineage_id,
                blueprint=item,
            ),
            blueprint=item,
        )
        for item in artifact.items
    )
    if len({item.occurrence_version_id for item in occurrences}) != len(occurrences):
        raise StateConflictError("Artifact produced duplicate occurrence identities")
    return PreparedLifecycleApply(
        stage_run_id=stage_run_id,
        stage_version_id=run.stage_version_id,
        source_lineage_id=run.source_lineage_id,
        processor_contract_version_id=run.processor_contract_version_id,
        claim_inputs_hash=claim_inputs_hash,
        artifact_source_card_revision_id=run.artifact_source_card_revision_id,
        extraction_artifact=artifact,
        extraction_run_id=f"extraction-run:v1:sha256:{extraction_digest}",
        occurrences=occurrences,
    )


def apply_lifecycle(
    connection: sqlite3.Connection,
    prepared: PreparedLifecycleApply,
    *,
    before_event_insert: Callable[[], None] | None = None,
    after_state_event_insert: Callable[[], None] | None = None,
) -> LifecycleApplyResult:
    """CAS-check and atomically apply one prepared extraction manifest."""
    with _immediate_transaction(connection):
        run = _load_stage_run(connection, prepared.stage_run_id)
        _verify_prepared_run(run, prepared)
        if run.status != "started":
            return LifecycleApplyResult(
                status=run.status,
                stage_run=run,
                extraction_run_id=(
                    prepared.extraction_run_id if run.status == "committed" else None
                ),
                applied_against_card_revision_id=run.applied_against_card_revision_id,
                counts=LifecycleCounts(),
            )

        duplicate = connection.execute(
            """
            SELECT stage_run_id
            FROM stage_runs
            WHERE idempotency_key = ?
              AND status = 'committed'
              AND stage_run_id <> ?
            """,
            (run.idempotency_key, run.stage_run_id),
        ).fetchone()
        if duplicate is not None:
            connection.execute(
                """
                UPDATE stage_runs
                SET status = 'no_op', duplicate_of_stage_run_id = ?, finished_at = ?
                WHERE stage_run_id = ? AND status = 'started'
                """,
                (duplicate["stage_run_id"], _utc_now(), run.stage_run_id),
            )
            completed = _load_stage_run(connection, run.stage_run_id)
            return LifecycleApplyResult(
                status="no_op",
                stage_run=completed,
                extraction_run_id=None,
                applied_against_card_revision_id=None,
                counts=LifecycleCounts(),
            )

        stage = _load_stage_version(connection, run.stage_version_id, changed=False)
        if not _stage_snapshot_is_current(connection, stage):
            connection.execute(
                """
                UPDATE stage_runs
                SET status = 'stale', finished_at = ?
                WHERE stage_run_id = ? AND status = 'started'
                """,
                (_utc_now(), run.stage_run_id),
            )
            completed = _load_stage_run(connection, run.stage_run_id)
            return LifecycleApplyResult(
                status="stale",
                stage_run=completed,
                extraction_run_id=None,
                applied_against_card_revision_id=None,
                counts=LifecycleCounts(),
            )

        current_card = connection.execute(
            """
            SELECT current_card_revision_id
            FROM source_lineage_heads
            WHERE source_lineage_id = ?
            """,
            (run.source_lineage_id,),
        ).fetchone()
        if current_card is None:
            raise StateConflictError("Extraction lineage has no current card revision")
        applied_card_revision_id = current_card["current_card_revision_id"]
        current_claim_binding = connection.execute(
            """
            SELECT input_hash
            FROM card_revision_input_bindings
            WHERE card_revision_id = ?
              AND source_lineage_id = ?
              AND input_kind = 'claim_inputs'
            """,
            (applied_card_revision_id, run.source_lineage_id),
        ).fetchone()
        if (
            current_claim_binding is None
            or current_claim_binding["input_hash"] != prepared.claim_inputs_hash
        ):
            _mark_stale_in_transaction(connection, run.stage_run_id)
            completed = _load_stage_run(connection, run.stage_run_id)
            return LifecycleApplyResult(
                status="stale",
                stage_run=completed,
                extraction_run_id=None,
                applied_against_card_revision_id=None,
                counts=LifecycleCounts(),
            )

        existing_states = _current_states_for_lineage(connection, run.source_lineage_id)
        existing_occurrences = _occurrence_rows_for_lineage(
            connection,
            run.source_lineage_id,
        )
        commit_seq = int(
            connection.execute(
                "SELECT COALESCE(MAX(commit_seq), 0) + 1 AS next_seq FROM stage_runs"
            ).fetchone()["next_seq"]
        )
        now = _utc_now()
        updated = connection.execute(
            """
            UPDATE stage_runs
            SET
                status = 'committed',
                applied_against_card_revision_id = ?,
                finished_at = ?,
                commit_seq = ?
            WHERE stage_run_id = ? AND status = 'started'
            """,
            (applied_card_revision_id, now, commit_seq, run.stage_run_id),
        )
        if updated.rowcount != 1:
            raise StateConflictError("Stage run commit CAS failed")

        connection.execute(
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prepared.extraction_run_id,
                run.stage_run_id,
                prepared.extraction_artifact.extraction_artifact_id,
                run.source_lineage_id,
                CLAIM_EXTRACTION_STAGE_KIND,
                run.processor_contract_version_id,
                prepared.claim_inputs_hash,
                prepared.artifact_source_card_revision_id,
                applied_card_revision_id,
                now,
            ),
        )
        _insert_occurrences_and_manifest(
            connection,
            prepared=prepared,
            card_revision_id=prepared.artifact_source_card_revision_id,
            created_at=now,
        )
        transitions, counts = _compute_transitions(
            prepared=prepared,
            existing_states=existing_states,
            existing_occurrences=existing_occurrences,
        )
        if before_event_insert is not None:
            before_event_insert()
        _insert_state_events(
            connection,
            prepared=prepared,
            transitions=transitions,
            existing_states=existing_states,
            created_at=now,
        )
        if after_state_event_insert is not None:
            after_state_event_insert()
        dependency = _publish_occurrence_snapshot_in_transaction(
            connection,
            prepared=prepared,
            stage_version_id=run.stage_version_id,
            created_at=now,
        )
        _insert_outbox_event(
            connection,
            prepared=prepared,
            commit_seq=commit_seq,
            dependency=dependency,
            counts=counts,
            created_at=now,
        )
        completed = _load_stage_run(connection, run.stage_run_id)
        return LifecycleApplyResult(
            status="committed",
            stage_run=completed,
            extraction_run_id=prepared.extraction_run_id,
            applied_against_card_revision_id=applied_card_revision_id,
            counts=counts,
        )


def _verify_prepared_run(run: StageRun, prepared: PreparedLifecycleApply) -> None:
    expected = (
        prepared.stage_version_id,
        prepared.source_lineage_id,
        prepared.processor_contract_version_id,
        prepared.artifact_source_card_revision_id,
    )
    actual = (
        run.stage_version_id,
        run.source_lineage_id,
        run.processor_contract_version_id,
        run.artifact_source_card_revision_id,
    )
    if actual != expected:
        raise StateConflictError("Prepared lifecycle apply no longer matches its stage run")


def _mark_stale_in_transaction(connection: sqlite3.Connection, stage_run_id: str) -> None:
    connection.execute(
        """
        UPDATE stage_runs
        SET status = 'stale', finished_at = ?
        WHERE stage_run_id = ? AND status = 'started'
        """,
        (_utc_now(), stage_run_id),
    )


def _current_states_for_lineage(
    connection: sqlite3.Connection,
    source_lineage_id: str,
) -> dict[str, sqlite3.Row]:
    return {
        row["occurrence_version_id"]: row
        for row in connection.execute(
            """
            SELECT *
            FROM occurrence_current_states
            WHERE source_lineage_id = ?
            """,
            (source_lineage_id,),
        ).fetchall()
    }


def _occurrence_rows_for_lineage(
    connection: sqlite3.Connection,
    source_lineage_id: str,
) -> dict[str, sqlite3.Row]:
    return {
        row["occurrence_version_id"]: row
        for row in connection.execute(
            """
            SELECT occurrence_version_id, stable_locator_json
            FROM claim_occurrences
            WHERE source_lineage_id = ?
            """,
            (source_lineage_id,),
        ).fetchall()
    }


def _insert_occurrences_and_manifest(
    connection: sqlite3.Connection,
    *,
    prepared: PreparedLifecycleApply,
    card_revision_id: str,
    created_at: str,
) -> None:
    for manifest_ordinal, occurrence in enumerate(prepared.occurrences):
        item = occurrence.blueprint
        existing = connection.execute(
            """
            SELECT
                source_lineage_id,
                field_kind,
                stable_locator_json,
                exact_occurrence_payload_json,
                exact_payload_hash,
                occurrence_fingerprint,
                occurrence_schema_version
            FROM claim_occurrences
            WHERE occurrence_version_id = ?
            """,
            (occurrence.occurrence_version_id,),
        ).fetchone()
        expected = (
            prepared.source_lineage_id,
            item.field_kind,
            item.locator_json,
            item.exact_payload_json,
            item.exact_payload_hash,
            item.occurrence_fingerprint,
            OCCURRENCE_SCHEMA_VERSION,
        )
        if existing is None:
            connection.execute(
                """
                INSERT INTO claim_occurrences (
                    occurrence_version_id,
                    source_lineage_id,
                    card_revision_id,
                    extraction_run_id,
                    field_kind,
                    stable_locator_json,
                    exact_occurrence_payload_json,
                    exact_payload_hash,
                    occurrence_fingerprint,
                    occurrence_schema_version,
                    extracted_from_claim_inputs_hash,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    occurrence.occurrence_version_id,
                    prepared.source_lineage_id,
                    card_revision_id,
                    prepared.extraction_run_id,
                    item.field_kind,
                    item.locator_json,
                    item.exact_payload_json,
                    item.exact_payload_hash,
                    item.occurrence_fingerprint,
                    OCCURRENCE_SCHEMA_VERSION,
                    prepared.claim_inputs_hash,
                    created_at,
                ),
            )
        elif tuple(existing) != expected:
            raise StateConflictError(
                f"Occurrence identity {occurrence.occurrence_version_id} has different content"
            )
        connection.execute(
            """
            INSERT INTO extraction_run_occurrences (
                extraction_run_id,
                occurrence_version_id,
                source_lineage_id,
                manifest_ordinal
            ) VALUES (?, ?, ?, ?)
            """,
            (
                prepared.extraction_run_id,
                occurrence.occurrence_version_id,
                prepared.source_lineage_id,
                manifest_ordinal,
            ),
        )


@dataclass(frozen=True)
class _Transition:
    occurrence_version_id: str
    to_status: Literal["active", "retired", "superseded"]
    superseded_by_occurrence_id: str | None = None


def _compute_transitions(
    *,
    prepared: PreparedLifecycleApply,
    existing_states: dict[str, sqlite3.Row],
    existing_occurrences: dict[str, sqlite3.Row],
) -> tuple[tuple[_Transition, ...], LifecycleCounts]:
    manifest_ids = {item.occurrence_version_id for item in prepared.occurrences}
    old_active_ids = {
        occurrence_id
        for occurrence_id, state in existing_states.items()
        if state["status"] == "active"
    }
    removed_ids = old_active_ids - manifest_ids
    newly_effective_ids = {
        occurrence_id
        for occurrence_id in manifest_ids
        if occurrence_id not in old_active_ids
    }

    old_external_all = {
        occurrence_id: _external_locator_key(
            json.loads(existing_occurrences[occurrence_id]["stable_locator_json"])
        )
        for occurrence_id in old_active_ids
    }
    new_by_id = {
        occurrence.occurrence_version_id: occurrence
        for occurrence in prepared.occurrences
    }
    new_external_all = {
        occurrence_id: occurrence.blueprint.external_locator_key
        for occurrence_id, occurrence in new_by_id.items()
    }
    old_counts = Counter(key for key in old_external_all.values() if key is not None)
    new_counts = Counter(key for key in new_external_all.values() if key is not None)
    unique_new_for_key = {
        key: occurrence_id
        for occurrence_id in newly_effective_ids
        if (key := new_external_all[occurrence_id]) is not None
        and new_counts[key] == 1
    }

    transitions: list[_Transition] = []
    retired = 0
    superseded = 0
    for occurrence_id in sorted(removed_ids):
        key = old_external_all[occurrence_id]
        successor = (
            unique_new_for_key.get(key)
            if key is not None and old_counts[key] == 1
            else None
        )
        if successor is None:
            transitions.append(_Transition(occurrence_id, "retired"))
            retired += 1
        else:
            transitions.append(
                _Transition(
                    occurrence_id,
                    "superseded",
                    superseded_by_occurrence_id=successor,
                )
            )
            superseded += 1

    active = 0
    reactivated = 0
    for occurrence_id in sorted(newly_effective_ids):
        previous = existing_states.get(occurrence_id)
        transitions.append(_Transition(occurrence_id, "active"))
        if previous is None:
            active += 1
        else:
            reactivated += 1
    return (
        tuple(transitions),
        LifecycleCounts(
            active=active,
            retired=retired,
            superseded=superseded,
            reactivated=reactivated,
        ),
    )


def _insert_state_events(
    connection: sqlite3.Connection,
    *,
    prepared: PreparedLifecycleApply,
    transitions: tuple[_Transition, ...],
    existing_states: dict[str, sqlite3.Row],
    created_at: str,
) -> None:
    for transition in transitions:
        previous = existing_states.get(transition.occurrence_version_id)
        event_digest = content_hash(
            {
                "extraction_run_id": prepared.extraction_run_id,
                "occurrence_version_id": transition.occurrence_version_id,
                "to_status": transition.to_status,
                "superseded_by": transition.superseded_by_occurrence_id,
            },
            namespace="wiki-occurrence-state-event:v1",
        ).removeprefix("sha256:")
        connection.execute(
            """
            INSERT INTO occurrence_state_events (
                state_event_id,
                occurrence_version_id,
                extraction_run_id,
                source_lineage_id,
                previous_state_event_id,
                to_status,
                superseded_by_occurrence_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"state-event:v1:sha256:{event_digest}",
                transition.occurrence_version_id,
                prepared.extraction_run_id,
                prepared.source_lineage_id,
                None if previous is None else previous["state_event_id"],
                transition.to_status,
                transition.superseded_by_occurrence_id,
                created_at,
            ),
        )


@dataclass(frozen=True)
class _PublishedDependency:
    dependency_version_id: str
    generation: int
    dependency_hash: str


def _publish_occurrence_snapshot_in_transaction(
    connection: sqlite3.Connection,
    *,
    prepared: PreparedLifecycleApply,
    stage_version_id: str,
    created_at: str,
) -> _PublishedDependency:
    payload = {
        "source_lineage_id": prepared.source_lineage_id,
        "active_occurrence_ids": sorted(
            occurrence.occurrence_version_id for occurrence in prepared.occurrences
        ),
    }
    payload_json = canonical_json(payload)
    dependency_hash = content_hash(
        payload,
        namespace=(
            f"wiki-v2-dependency:{DependencyKind.OCCURRENCE_SNAPSHOT}:"
            f"{prepared.source_lineage_id}"
        ),
    )
    current = connection.execute(
        """
        SELECT *
        FROM dependency_heads
        WHERE dependency_kind = ? AND dependency_scope_key = ?
        """,
        (DependencyKind.OCCURRENCE_SNAPSHOT, prepared.source_lineage_id),
    ).fetchone()
    if current is not None and current["current_hash"] == dependency_hash:
        return _PublishedDependency(
            dependency_version_id=current["current_dependency_version_id"],
            generation=int(current["current_generation"]),
            dependency_hash=current["current_hash"],
        )

    generation = 1 if current is None else int(current["current_generation"]) + 1
    dependency_version_id = _new_id()
    connection.execute(
        """
        INSERT INTO dependency_versions (
            dependency_version_id,
            dependency_kind,
            dependency_scope_key,
            dependency_generation,
            dependency_hash,
            canonical_payload_json,
            producer_kind,
            produced_by_stage_version_id,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'stage', ?, ?)
        """,
        (
            dependency_version_id,
            DependencyKind.OCCURRENCE_SNAPSHOT,
            prepared.source_lineage_id,
            generation,
            dependency_hash,
            payload_json,
            stage_version_id,
            created_at,
        ),
    )
    if current is None:
        connection.execute(
            """
            INSERT INTO dependency_heads (
                dependency_kind,
                dependency_scope_key,
                current_dependency_version_id,
                current_generation,
                current_hash,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                DependencyKind.OCCURRENCE_SNAPSHOT,
                prepared.source_lineage_id,
                dependency_version_id,
                generation,
                dependency_hash,
                created_at,
            ),
        )
    else:
        updated = connection.execute(
            """
            UPDATE dependency_heads
            SET
                current_dependency_version_id = ?,
                current_generation = ?,
                current_hash = ?,
                updated_at = ?
            WHERE dependency_kind = ?
              AND dependency_scope_key = ?
              AND current_dependency_version_id = ?
            """,
            (
                dependency_version_id,
                generation,
                dependency_hash,
                created_at,
                DependencyKind.OCCURRENCE_SNAPSHOT,
                prepared.source_lineage_id,
                current["current_dependency_version_id"],
            ),
        )
        if updated.rowcount != 1:
            raise StateConflictError("Occurrence dependency head CAS failed")
    return _PublishedDependency(
        dependency_version_id=dependency_version_id,
        generation=generation,
        dependency_hash=dependency_hash,
    )


def _insert_outbox_event(
    connection: sqlite3.Connection,
    *,
    prepared: PreparedLifecycleApply,
    commit_seq: int,
    dependency: _PublishedDependency,
    counts: LifecycleCounts,
    created_at: str,
) -> None:
    payload = {
        "dependency_kind": DependencyKind.OCCURRENCE_SNAPSHOT,
        "dependency_scope_key": prepared.source_lineage_id,
        "dependency_version_id": dependency.dependency_version_id,
        "dependency_generation": dependency.generation,
        "dependency_hash": dependency.dependency_hash,
        "lifecycle_counts": {
            "active": counts.active,
            "retired": counts.retired,
            "superseded": counts.superseded,
            "reactivated": counts.reactivated,
        },
    }
    event_key = f"occurrence-snapshot:{prepared.stage_run_id}"
    connection.execute(
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
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"outbox:{prepared.stage_run_id}:occurrence-snapshot",
            event_key,
            prepared.stage_run_id,
            commit_seq,
            "dependency.updated",
            DependencyKind.OCCURRENCE_SNAPSHOT,
            prepared.source_lineage_id,
            canonical_json(payload),
            created_at,
        ),
    )


def _external_locator_key(locator: dict[str, object]) -> str | None:
    if not locator.get("exact_external"):
        return None
    return canonical_json(
        {
            "locator_type": locator.get("locator_type"),
            "value": locator.get("value"),
        }
    )
