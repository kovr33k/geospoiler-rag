"""Deterministic, reversible claim grouping for Wiki occurrences."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from geospoiler.wiki.hashing import canonical_json, content_hash, sha256_hex
from geospoiler.wiki.schema import CLAIM_GROUPING_STAGE_KIND
from geospoiler.wiki.state import (
    DependencyKey,
    OutboxEventSpec,
    ProcessorContractSpec,
    _immediate_transaction,
    activate_processor_contract,
    commit_stage_run,
    get_dependency_head,
    publish_dependency,
    schedule_stage,
    start_stage_run,
)

DEFAULT_GROUPING_CONTRACT = ProcessorContractSpec(
    algorithm_version="almost-exact-occurrence-grouping-v1",
    schema_version="claim-group-v1",
    canonicalizer_version="wiki-canonical-json-nfc-v1",
    policy_version="no-semantic-auto-merge-v1",
)


class GroupingError(RuntimeError):
    """Raised when claim grouping cannot be applied safely."""


@dataclass(frozen=True)
class GroupingLineageResult:
    source_lineage_id: str
    stage_version_id: str
    status: Literal["committed", "no_op", "stale"]
    occurrences_seen: int
    groups_created: int
    memberships_written: int
    dependency_changed: bool


@dataclass(frozen=True)
class GroupingStats:
    lineages_seen: int
    lineages_committed: int
    lineages_no_op: int
    lineages_stale: int
    occurrences_seen: int
    groups_created: int
    memberships_written: int
    dependencies_changed: int


def group_all_claims(
    connection: sqlite3.Connection,
    *,
    contract: ProcessorContractSpec = DEFAULT_GROUPING_CONTRACT,
) -> GroupingStats:
    """Group lifecycle-active occurrences before eligibility is overlaid."""
    lineage_ids = [
        row["source_lineage_id"]
        for row in connection.execute(
            """
            SELECT head.source_lineage_id
            FROM source_lineage_heads AS head
            JOIN dependency_heads AS occurrence
              ON occurrence.dependency_kind = 'occurrence_snapshot'
             AND occurrence.dependency_scope_key = head.source_lineage_id
            ORDER BY head.source_lineage_id
            """
        ).fetchall()
    ]
    results = [
        group_lineage_claims(connection, lineage_id, contract=contract)
        for lineage_id in lineage_ids
    ]
    return GroupingStats(
        lineages_seen=len(results),
        lineages_committed=sum(result.status == "committed" for result in results),
        lineages_no_op=sum(result.status == "no_op" for result in results),
        lineages_stale=sum(result.status == "stale" for result in results),
        occurrences_seen=sum(result.occurrences_seen for result in results),
        groups_created=sum(result.groups_created for result in results),
        memberships_written=sum(result.memberships_written for result in results),
        dependencies_changed=sum(result.dependency_changed for result in results),
    )


def group_lineage_claims(
    connection: sqlite3.Connection,
    source_lineage_id: str,
    *,
    contract: ProcessorContractSpec = DEFAULT_GROUPING_CONTRACT,
) -> GroupingLineageResult:
    """Run exact deterministic grouping for one lineage stage snapshot."""
    activate_processor_contract(
        connection,
        stage_kind=CLAIM_GROUPING_STAGE_KIND,
        contract=contract,
    )
    dependencies = (DependencyKey("occurrence_snapshot", source_lineage_id),)
    stage = schedule_stage(
        connection,
        source_lineage_id=source_lineage_id,
        stage_kind=CLAIM_GROUPING_STAGE_KIND,
        input_kinds=[],
        dependencies=dependencies,
    )
    idempotency_key = f"wiki-claim-grouping:{source_lineage_id}:{stage.stage_version_id}"
    committed = connection.execute(
        """
        SELECT stage_run_id
        FROM stage_runs
        WHERE idempotency_key = ? AND status = 'committed'
        """,
        (idempotency_key,),
    ).fetchone()
    run_status: Literal["committed", "no_op", "stale"]
    if committed is None:
        run = start_stage_run(
            connection,
            stage_version_id=stage.stage_version_id,
            idempotency_key=idempotency_key,
        )
        with _immediate_transaction(connection):
            run = commit_stage_run(
                connection,
                stage_run_id=run.stage_run_id,
                outbox_events=(
                    OutboxEventSpec(
                        event_key=f"claim-grouping:{stage.stage_version_id}",
                        event_kind="claim_grouping_ready",
                        aggregate_kind="source_lineage",
                        aggregate_key=source_lineage_id,
                        payload={
                            "source_lineage_id": source_lineage_id,
                            "stage_version_id": stage.stage_version_id,
                        },
                    ),
                ),
            )
            if run.status == "stale":
                return GroupingLineageResult(
                    source_lineage_id=source_lineage_id,
                    stage_version_id=stage.stage_version_id,
                    status="stale",
                    occurrences_seen=0,
                    groups_created=0,
                    memberships_written=0,
                    dependency_changed=False,
                )
            occurrences_seen, groups_created, memberships_written = (
                _apply_grouping_output(
                    connection,
                    source_lineage_id,
                    stage.stage_version_id,
                )
            )
            dependency_changed = _publish_grouping_dependency(
                connection,
                source_lineage_id=source_lineage_id,
                stage_version_id=stage.stage_version_id,
            )
        run_status = "committed" if run.status == "committed" else "no_op"
    else:
        with _immediate_transaction(connection):
            occurrences_seen, groups_created, memberships_written = (
                _apply_grouping_output(
                    connection,
                    source_lineage_id,
                    stage.stage_version_id,
                )
            )
            dependency_changed = _publish_grouping_dependency(
                connection,
                source_lineage_id=source_lineage_id,
                stage_version_id=stage.stage_version_id,
            )
        run_status = "no_op"
    return GroupingLineageResult(
        source_lineage_id=source_lineage_id,
        stage_version_id=stage.stage_version_id,
        status=run_status,
        occurrences_seen=occurrences_seen,
        groups_created=groups_created,
        memberships_written=memberships_written,
        dependency_changed=dependency_changed,
    )


def set_group_override(
    connection: sqlite3.Connection,
    *,
    occurrence_version_id: str,
    claim_group_id: str | None,
    rationale: str,
) -> str:
    """Authoritatively assign/clear one occurrence without deleting auto state."""
    row = connection.execute(
        """
        SELECT occurrence_fingerprint
        FROM claim_occurrences
        WHERE occurrence_version_id = ?
        """,
        (occurrence_version_id,),
    ).fetchone()
    if row is None:
        raise GroupingError(f"Unknown occurrence {occurrence_version_id}")
    if claim_group_id is not None:
        exists = connection.execute(
            "SELECT 1 FROM claim_groups WHERE claim_group_id = ?",
            (claim_group_id,),
        ).fetchone()
        if exists is None:
            raise GroupingError(f"Unknown claim group {claim_group_id}")
    with _immediate_transaction(connection):
        generation = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(decision_generation), 0) + 1
                FROM claim_group_overrides
                WHERE occurrence_version_id = ?
                """,
                (occurrence_version_id,),
            ).fetchone()[0]
        )
        override_id = _stable_id(
            "claim-group-override",
            {
                "occurrence_version_id": occurrence_version_id,
                "generation": generation,
                "claim_group_id": claim_group_id,
            },
        )
        connection.execute(
            """
            INSERT INTO claim_group_overrides (
                claim_group_override_id,
                occurrence_version_id,
                decision_generation,
                action,
                claim_group_id,
                occurrence_fingerprint,
                override_status,
                rationale,
                decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                override_id,
                occurrence_version_id,
                generation,
                "clear" if claim_group_id is None else "assign",
                claim_group_id,
                row["occurrence_fingerprint"],
                rationale or None,
                _utc_now(),
            ),
        )
    return override_id


def _apply_grouping_output(
    connection: sqlite3.Connection,
    source_lineage_id: str,
    stage_version_id: str,
) -> tuple[int, int, int]:
    with _immediate_transaction(connection):
        if not _stage_is_current(connection, source_lineage_id, stage_version_id):
            raise GroupingError("Grouping stage became stale before output apply")
        occurrences = connection.execute(
            """
            SELECT
                occurrence_version_id,
                field_kind,
                exact_occurrence_payload_json
            FROM lifecycle_active_occurrences
            WHERE source_lineage_id = ?
            ORDER BY occurrence_version_id
            """,
            (source_lineage_id,),
        ).fetchall()
        groups_created = 0
        memberships_written = 0
        for occurrence in occurrences:
            canonical_claim = {
                "field_kind": occurrence["field_kind"],
                "payload": json.loads(
                    occurrence["exact_occurrence_payload_json"]
                ),
            }
            canonical_hash = content_hash(
                canonical_claim,
                namespace="wiki-v2-almost-exact-claim-group",
                exact_quote_paths=(
                    ("payload", "text"),
                )
                if occurrence["field_kind"] == "quote"
                else (),
                unordered_collection_paths=(
                    ("payload", "actors"),
                )
                if occurrence["field_kind"] == "event"
                else (),
            )
            group_id = (
                "claim-group:v1:sha256:"
                + canonical_hash.removeprefix("sha256:")
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO claim_groups (
                    claim_group_id,
                    canonical_claim_hash,
                    canonical_claim_json,
                    created_by_stage_version_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    canonical_hash,
                    canonical_json(canonical_claim),
                    stage_version_id,
                    _utc_now(),
                ),
            )
            groups_created += int(cursor.rowcount > 0)
            already = connection.execute(
                """
                SELECT 1
                FROM automatic_group_memberships
                WHERE occurrence_version_id = ?
                  AND produced_by_stage_version_id = ?
                """,
                (occurrence["occurrence_version_id"], stage_version_id),
            ).fetchone()
            if already is not None:
                continue
            generation = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(automatic_generation), 0) + 1
                    FROM automatic_group_memberships
                    WHERE occurrence_version_id = ?
                    """,
                    (occurrence["occurrence_version_id"],),
                ).fetchone()[0]
            )
            inputs_hash = content_hash(
                {
                    "occurrence_version_id": occurrence["occurrence_version_id"],
                    "canonical_claim_hash": canonical_hash,
                    "stage_version_id": stage_version_id,
                },
                namespace="wiki-v2-group-membership-inputs",
            )
            membership_id = _stable_id(
                "automatic-group-membership",
                {
                    "occurrence_version_id": occurrence["occurrence_version_id"],
                    "generation": generation,
                    "claim_group_id": group_id,
                },
            )
            connection.execute(
                """
                INSERT INTO automatic_group_memberships (
                    automatic_group_membership_id,
                    occurrence_version_id,
                    claim_group_id,
                    automatic_generation,
                    produced_by_stage_version_id,
                    rule_inputs_hash,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    membership_id,
                    occurrence["occurrence_version_id"],
                    group_id,
                    generation,
                    stage_version_id,
                    inputs_hash,
                    _utc_now(),
                ),
            )
            memberships_written += 1
    return len(occurrences), groups_created, memberships_written


def _publish_grouping_dependency(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    stage_version_id: str,
) -> bool:
    rows = connection.execute(
        """
        SELECT
            membership.occurrence_version_id,
            membership.claim_group_id,
            membership.membership_source
        FROM effective_claim_group_memberships AS membership
        JOIN lifecycle_active_occurrences AS occurrence
          ON occurrence.occurrence_version_id = membership.occurrence_version_id
        WHERE occurrence.source_lineage_id = ?
        ORDER BY membership.occurrence_version_id
        """,
        (source_lineage_id,),
    ).fetchall()
    current = get_dependency_head(
        connection,
        dependency_kind="effective_claim_groups",
        dependency_scope_key=source_lineage_id,
    )
    published = publish_dependency(
        connection,
        dependency_kind="effective_claim_groups",
        dependency_scope_key=source_lineage_id,
        payload={
            "source_lineage_id": source_lineage_id,
            "memberships": [dict(row) for row in rows],
        },
        expected_version_id=(
            None if current is None else current.dependency_version_id
        ),
        producer_kind="stage",
        produced_by_stage_version_id=stage_version_id,
        unordered_collection_paths=(("memberships",),),
    )
    return published.changed


def _stage_is_current(
    connection: sqlite3.Connection,
    source_lineage_id: str,
    stage_version_id: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM lineage_stage_heads AS stage_head
        JOIN lineage_stage_versions AS stage
          ON stage.stage_version_id = stage_head.current_stage_version_id
        JOIN active_processor_contract_heads AS contract_head
          ON contract_head.stage_kind = stage.stage_kind
         AND contract_head.current_activation_generation =
             stage.processor_contract_activation_generation
        WHERE stage_head.source_lineage_id = ?
          AND stage_head.stage_kind = ?
          AND stage_head.current_stage_version_id = ?
          AND NOT EXISTS (
              SELECT 1
              FROM lineage_stage_input_bindings AS binding
              LEFT JOIN lineage_input_heads AS input_head
                ON input_head.source_lineage_id = binding.source_lineage_id
               AND input_head.input_kind = binding.input_kind
              WHERE binding.stage_version_id = stage.stage_version_id
                AND (
                    input_head.current_input_version_id IS NULL
                    OR input_head.current_input_version_id <> binding.input_version_id
                    OR input_head.current_input_generation <> binding.input_generation
                    OR input_head.current_input_hash <> binding.input_hash
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM stage_dependency_bindings AS binding
              LEFT JOIN dependency_heads AS dependency_head
                ON dependency_head.dependency_kind = binding.dependency_kind
               AND dependency_head.dependency_scope_key =
                   binding.dependency_scope_key
              WHERE binding.stage_version_id = stage.stage_version_id
                AND (
                    dependency_head.current_dependency_version_id IS NULL
                    OR dependency_head.current_dependency_version_id <>
                       binding.dependency_version_id
                    OR dependency_head.current_generation <> binding.dependency_generation
                    OR dependency_head.current_hash <> binding.dependency_hash
                )
          )
        """,
        (source_lineage_id, CLAIM_GROUPING_STAGE_KIND, stage_version_id),
    ).fetchone()
    return row is not None


def _stable_id(prefix: str, payload: object) -> str:
    return f"{prefix}:v1:sha256:{sha256_hex(prefix + chr(10) + canonical_json(payload))}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
