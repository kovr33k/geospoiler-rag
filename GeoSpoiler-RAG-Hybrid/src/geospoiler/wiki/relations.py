"""Role-aware concept linking and computed card relations."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from geospoiler.wiki.cards import STRUCTURED_RELATION_INPUT_KIND
from geospoiler.wiki.hashing import canonical_json, content_hash, normalize_text, sha256_hex
from geospoiler.wiki.registry import EXCLUDED_ENTITY_CATEGORIES, normalize_surface, publish_registry_dependencies
from geospoiler.wiki.schema import CONCEPT_LINKING_STAGE_KIND
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

RelationRole = Literal["subject", "actor", "object", "context", "mentioned", "unknown"]
RelationKind = Literal["direct", "context", "mentioned"]

DEFAULT_RELATION_CONTRACT = ProcessorContractSpec(
    algorithm_version="structured-surface-role-linker-v3",
    schema_version="wiki-relation-v1",
    canonicalizer_version="wiki-canonical-json-nfc-v1",
    policy_version="exclude-media-sources-and-selective-luna-v3",
    prompt_template_version=None,
    model_profile_version=None,
)
LUNA_RELATION_PROMPT_VERSION = "wiki-relation-role-resolver-v1"

_ROLE_STRENGTH: dict[str, int] = {
    "subject": 6,
    "actor": 5,
    "object": 4,
    "context": 3,
    "unknown": 2,
    "mentioned": 1,
}


class RelationError(RuntimeError):
    """Raised when relation processing cannot be applied safely."""


@dataclass(frozen=True)
class CandidateSnapshot:
    source_lineage_id: str
    surfaces: tuple[dict[str, Any], ...]
    concept_ids: tuple[str, ...]
    changed: bool


@dataclass(frozen=True)
class RelationLineageResult:
    source_lineage_id: str
    stage_version_id: str
    status: Literal["committed", "no_op", "stale"]
    active_links: int
    absent_links: int
    ambiguity_candidates: int
    active_card_relations: int
    absent_card_relations: int
    dependency_changed: bool
    resolver_error: str | None = None


@dataclass(frozen=True)
class RelationStats:
    lineages_seen: int
    lineages_committed: int
    lineages_no_op: int
    lineages_stale: int
    active_links: int
    absent_links: int
    ambiguity_candidates: int
    active_card_relations: int
    absent_card_relations: int
    dependencies_changed: int
    resolver_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class _StructuredSurface:
    normalized_surface: str
    display_surface: str
    field: Literal["entity", "topic"]
    source_category: str
    salience: str
    source_role: str
    locator: dict[str, Any]
    candidate_concept_ids: tuple[str, ...]


@dataclass(frozen=True)
class _DesiredLink:
    occurrence_version_id: str
    concept_id: str
    relation_role: RelationRole
    source_locator: dict[str, Any]
    rule_id: str
    explanation: str
    confidence: float
    rule_inputs: dict[str, Any]
    resolver_version: str = "deterministic-structured-resolver-v1"


@dataclass(frozen=True)
class _RelationOutputCounts:
    active_links: int = 0
    absent_links: int = 0
    ambiguity_candidates: int = 0
    active_card_relations: int = 0
    absent_card_relations: int = 0


@dataclass(frozen=True)
class _LunaResolutionBundle:
    role_overrides: dict[
        tuple[str, str],
        tuple[RelationRole, str, str],
    ]
    metonym_links: dict[tuple[str, str], _DesiredLink]
    metonym_ambiguities: tuple[
        tuple[str, _StructuredSurface, str, str],
        ...,
    ]
    error: str | None = None


def link_all_concepts(
    connection: sqlite3.Connection,
    *,
    contract: ProcessorContractSpec = DEFAULT_RELATION_CONTRACT,
    use_luna: bool = False,
) -> RelationStats:
    """Resolve every current lineage against approved registry concepts."""
    publish_registry_dependencies(connection)
    lineage_ids = [
        row["source_lineage_id"]
        for row in connection.execute(
            """
            SELECT head.source_lineage_id
            FROM source_lineage_heads AS head
            JOIN lineage_input_heads AS relation_input
              ON relation_input.source_lineage_id = head.source_lineage_id
             AND relation_input.input_kind = 'structured_relation_inputs'
            JOIN dependency_heads AS occurrence
              ON occurrence.dependency_kind = 'occurrence_snapshot'
             AND occurrence.dependency_scope_key = head.source_lineage_id
            ORDER BY head.source_lineage_id
            """
        ).fetchall()
    ]
    results = [
        link_lineage_concepts(
            connection,
            lineage_id,
            contract=contract,
            use_luna=use_luna,
        )
        for lineage_id in lineage_ids
    ]
    return RelationStats(
        lineages_seen=len(results),
        lineages_committed=sum(result.status == "committed" for result in results),
        lineages_no_op=sum(result.status == "no_op" for result in results),
        lineages_stale=sum(result.status == "stale" for result in results),
        active_links=sum(result.active_links for result in results),
        absent_links=sum(result.absent_links for result in results),
        ambiguity_candidates=sum(result.ambiguity_candidates for result in results),
        active_card_relations=sum(
            result.active_card_relations for result in results
        ),
        absent_card_relations=sum(
            result.absent_card_relations for result in results
        ),
        dependencies_changed=sum(result.dependency_changed for result in results),
        resolver_errors=tuple(
            result.resolver_error
            for result in results
            if result.resolver_error is not None
        ),
    )


def refresh_candidate_snapshot(
    connection: sqlite3.Connection,
    source_lineage_id: str,
    *,
    include_metonym_candidates: bool = False,
) -> CandidateSnapshot:
    """Materialize the exact surface/candidate set used by relation CAS."""
    structured = _load_structured_input(connection, source_lineage_id)
    surfaces = _surface_blueprints(connection, structured)
    payload_surfaces = [
        {
            "normalized_surface": surface.normalized_surface,
            "display_surface": surface.display_surface,
            "field": surface.field,
            "source_category": surface.source_category,
            "salience": surface.salience,
            "source_role": surface.source_role,
            "candidate_concept_ids": list(surface.candidate_concept_ids),
            "source_locator": surface.locator,
        }
        for surface in surfaces
    ]
    metonym_candidate_ids = (
        tuple(
            row["concept_id"]
            for row in connection.execute(
                """
                SELECT concept_id
                FROM approved_concepts
                ORDER BY concept_id
                LIMIT 300
                """
            ).fetchall()
        )
        if include_metonym_candidates
        else ()
    )
    current = get_dependency_head(
        connection,
        dependency_kind="candidate_snapshot",
        dependency_scope_key=source_lineage_id,
    )
    published = publish_dependency(
        connection,
        dependency_kind="candidate_snapshot",
        dependency_scope_key=source_lineage_id,
        payload={
            "source_lineage_id": source_lineage_id,
            "surfaces": payload_surfaces,
            "metonym_candidate_concept_ids": list(metonym_candidate_ids),
        },
        expected_version_id=(
            None if current is None else current.dependency_version_id
        ),
        producer_kind="registry",
        unordered_collection_paths=(
            ("surfaces",),
            ("metonym_candidate_concept_ids",),
        ),
    )
    return CandidateSnapshot(
        source_lineage_id=source_lineage_id,
        surfaces=tuple(payload_surfaces),
        concept_ids=tuple(
            sorted(
                {
                    concept_id
                    for surface in surfaces
                    for concept_id in surface.candidate_concept_ids
                }
                | set(metonym_candidate_ids)
            )
        ),
        changed=published.changed,
    )


def link_lineage_concepts(
    connection: sqlite3.Connection,
    source_lineage_id: str,
    *,
    contract: ProcessorContractSpec = DEFAULT_RELATION_CONTRACT,
    use_luna: bool = False,
) -> RelationLineageResult:
    """Link occurrences and materialize card relations for one lineage."""
    publish_registry_dependencies(connection)
    effective_contract = _relation_contract(contract, use_luna=use_luna)
    candidate_snapshot = refresh_candidate_snapshot(
        connection,
        source_lineage_id,
        include_metonym_candidates=use_luna,
    )
    activate_processor_contract(
        connection,
        stage_kind=CONCEPT_LINKING_STAGE_KIND,
        contract=effective_contract,
    )
    dependency_keys: list[DependencyKey] = [
        DependencyKey("candidate_snapshot", source_lineage_id),
        DependencyKey("occurrence_snapshot", source_lineage_id),
    ]
    for concept_id in candidate_snapshot.concept_ids:
        if get_dependency_head(
            connection,
            dependency_kind="registry_snapshot",
            dependency_scope_key=concept_id,
        ) is not None:
            dependency_keys.append(DependencyKey("registry_snapshot", concept_id))
        if get_dependency_head(
            connection,
            dependency_kind="approved_identity_alias_snapshot",
            dependency_scope_key=concept_id,
        ) is not None:
            dependency_keys.append(
                DependencyKey("approved_identity_alias_snapshot", concept_id)
            )
    stage = schedule_stage(
        connection,
        source_lineage_id=source_lineage_id,
        stage_kind=CONCEPT_LINKING_STAGE_KIND,
        input_kinds=[STRUCTURED_RELATION_INPUT_KIND],
        dependencies=dependency_keys,
    )
    idempotency_key = f"wiki-concept-linking:{source_lineage_id}:{stage.stage_version_id}"
    committed = connection.execute(
        """
        SELECT stage_run_id
        FROM stage_runs
        WHERE idempotency_key = ? AND status = 'committed'
        """,
        (idempotency_key,),
    ).fetchone()
    if committed is not None:
        with _immediate_transaction(connection):
            if not _stage_is_current(
                connection,
                source_lineage_id,
                stage.stage_version_id,
            ):
                return RelationLineageResult(
                    source_lineage_id=source_lineage_id,
                    stage_version_id=stage.stage_version_id,
                    status="stale",
                    active_links=0,
                    absent_links=0,
                    ambiguity_candidates=0,
                    active_card_relations=0,
                    absent_card_relations=0,
                    dependency_changed=False,
                    resolver_error=None,
                )
            active_card, absent_card = _write_card_relations(
                connection,
                source_lineage_id=source_lineage_id,
                stage_version_id=stage.stage_version_id,
            )
            dependency_changed = _publish_relation_dependency(
                connection,
                source_lineage_id=source_lineage_id,
                stage_version_id=stage.stage_version_id,
            )
        return RelationLineageResult(
            source_lineage_id=source_lineage_id,
            stage_version_id=stage.stage_version_id,
            status="no_op",
            active_links=0,
            absent_links=0,
            ambiguity_candidates=0,
            active_card_relations=active_card,
            absent_card_relations=absent_card,
            dependency_changed=dependency_changed,
            resolver_error=None,
        )

    run = start_stage_run(
        connection,
        stage_version_id=stage.stage_version_id,
        idempotency_key=idempotency_key,
    )
    luna_bundle = _LunaResolutionBundle(
        role_overrides={},
        metonym_links={},
        metonym_ambiguities=(),
    )
    if use_luna:
        luna_bundle = _luna_resolutions(
            connection,
            source_lineage_id=source_lineage_id,
            candidate_snapshot=candidate_snapshot,
        )
    with _immediate_transaction(connection):
        run = commit_stage_run(
            connection,
            stage_run_id=run.stage_run_id,
            outbox_events=(
                OutboxEventSpec(
                    event_key=f"concept-linking:{stage.stage_version_id}",
                    event_kind="concept_linking_ready",
                    aggregate_kind="source_lineage",
                    aggregate_key=source_lineage_id,
                    payload={
                        "source_lineage_id": source_lineage_id,
                        "stage_version_id": stage.stage_version_id,
                    },
                ),
            ),
        )
        if run.status == "committed":
            counts = _apply_relation_output(
                connection,
                source_lineage_id=source_lineage_id,
                stage_version_id=stage.stage_version_id,
                candidate_snapshot=candidate_snapshot,
                role_overrides=luna_bundle.role_overrides,
                metonym_links=luna_bundle.metonym_links,
                metonym_ambiguities=luna_bundle.metonym_ambiguities,
            )
            dependency_changed = _publish_relation_dependency(
                connection,
                source_lineage_id=source_lineage_id,
                stage_version_id=stage.stage_version_id,
            )
        elif run.status == "no_op":
            active_card, absent_card = _write_card_relations(
                connection,
                source_lineage_id=source_lineage_id,
                stage_version_id=stage.stage_version_id,
            )
            counts = _RelationOutputCounts(
                active_links=0,
                absent_links=0,
                ambiguity_candidates=0,
                active_card_relations=active_card,
                absent_card_relations=absent_card,
            )
            dependency_changed = _publish_relation_dependency(
                connection,
                source_lineage_id=source_lineage_id,
                stage_version_id=stage.stage_version_id,
            )
        else:
            counts = _RelationOutputCounts(
                active_links=0,
                absent_links=0,
                ambiguity_candidates=0,
                active_card_relations=0,
                absent_card_relations=0,
            )
            dependency_changed = False
    run_status: Literal["committed", "no_op", "stale"] = run.status
    return RelationLineageResult(
        source_lineage_id=source_lineage_id,
        stage_version_id=stage.stage_version_id,
        status=run_status,
        active_links=counts.active_links,
        absent_links=counts.absent_links,
        ambiguity_candidates=counts.ambiguity_candidates,
        active_card_relations=counts.active_card_relations,
        absent_card_relations=counts.absent_card_relations,
        dependency_changed=dependency_changed,
        resolver_error=luna_bundle.error,
    )


def set_concept_link_override(
    connection: sqlite3.Connection,
    *,
    occurrence_version_id: str,
    concept_id: str,
    relation_role: RelationRole | None,
    rationale: str,
) -> str:
    """Authoritatively include/exclude one occurrence→concept link."""
    occurrence = connection.execute(
        """
        SELECT occurrence_fingerprint
        FROM claim_occurrences
        WHERE occurrence_version_id = ?
        """,
        (occurrence_version_id,),
    ).fetchone()
    if occurrence is None:
        raise RelationError(f"Unknown occurrence {occurrence_version_id}")
    if connection.execute(
        "SELECT 1 FROM approved_concepts WHERE concept_id = ?",
        (concept_id,),
    ).fetchone() is None:
        raise RelationError(f"Unknown approved concept {concept_id}")
    if relation_role is not None and relation_role not in _ROLE_STRENGTH:
        raise ValueError(f"Unsupported relation role: {relation_role}")
    with _immediate_transaction(connection):
        generation = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(decision_generation), 0) + 1
                FROM occurrence_concept_link_overrides
                WHERE occurrence_version_id = ? AND concept_id = ?
                """,
                (occurrence_version_id, concept_id),
            ).fetchone()[0]
        )
        override_id = _stable_id(
            "concept-link-override",
            {
                "occurrence_version_id": occurrence_version_id,
                "concept_id": concept_id,
                "generation": generation,
                "relation_role": relation_role,
            },
        )
        connection.execute(
            """
            INSERT INTO occurrence_concept_link_overrides (
                concept_link_override_id,
                occurrence_version_id,
                concept_id,
                decision_generation,
                action,
                relation_role,
                occurrence_fingerprint,
                override_status,
                rationale,
                decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                override_id,
                occurrence_version_id,
                concept_id,
                generation,
                "exclude" if relation_role is None else "include",
                relation_role,
                occurrence["occurrence_fingerprint"],
                normalize_text(rationale) or None,
                _utc_now(),
            ),
        )
    _refresh_manual_relation_effects(connection, occurrence_version_id)
    return override_id


def resolve_ambiguity(
    connection: sqlite3.Connection,
    *,
    occurrence_version_id: str,
    normalized_surface: str,
    selected_concept_id: str | None,
    relation_role: RelationRole = "context",
    rationale: str,
) -> str:
    """Pin a claim-specific resolution or leave it explicitly unresolved."""
    occurrence = connection.execute(
        """
        SELECT occurrence_fingerprint
        FROM claim_occurrences
        WHERE occurrence_version_id = ?
        """,
        (occurrence_version_id,),
    ).fetchone()
    if occurrence is None:
        raise RelationError(f"Unknown occurrence {occurrence_version_id}")
    normalized = normalize_surface(normalized_surface)
    if not normalized:
        raise ValueError("normalized_surface must not be empty")
    candidate_concept_ids = tuple(
        row["candidate_concept_id"]
        for row in connection.execute(
            """
            SELECT DISTINCT candidate_concept_id
            FROM metonym_candidates
            WHERE occurrence_version_id = ?
              AND normalized_surface = ?
            ORDER BY candidate_concept_id
            """,
            (occurrence_version_id, normalized),
        ).fetchall()
    )
    if selected_concept_id is not None:
        if selected_concept_id not in candidate_concept_ids:
            raise RelationError("Selected concept is not a current recorded candidate")
    with _immediate_transaction(connection):
        generation = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(decision_generation), 0) + 1
                FROM metonym_overrides
                WHERE occurrence_version_id = ? AND normalized_surface = ?
                """,
                (occurrence_version_id, normalized),
            ).fetchone()[0]
        )
        override_id = _stable_id(
            "claim-resolution-override",
            {
                "occurrence_version_id": occurrence_version_id,
                "normalized_surface": normalized,
                "generation": generation,
                "selected_concept_id": selected_concept_id,
            },
        )
        connection.execute(
            """
            INSERT INTO metonym_overrides (
                metonym_override_id,
                occurrence_version_id,
                normalized_surface,
                decision_generation,
                decision,
                selected_concept_id,
                occurrence_fingerprint,
                override_status,
                rationale,
                decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                override_id,
                occurrence_version_id,
                normalized,
                generation,
                "unresolved" if selected_concept_id is None else "pin",
                selected_concept_id,
                occurrence["occurrence_fingerprint"],
                normalize_text(rationale) or None,
                _utc_now(),
            ),
        )
        for candidate_concept_id in candidate_concept_ids:
            _insert_link_override_in_transaction(
                connection,
                occurrence_version_id=occurrence_version_id,
                concept_id=candidate_concept_id,
                relation_role=(
                    relation_role
                    if candidate_concept_id == selected_concept_id
                    else None
                ),
                occurrence_fingerprint=occurrence["occurrence_fingerprint"],
                rationale=rationale,
            )
    _refresh_manual_relation_effects(connection, occurrence_version_id)
    return override_id


def _refresh_manual_relation_effects(
    connection: sqlite3.Connection,
    occurrence_version_id: str,
) -> None:
    stage_row = connection.execute(
        """
        SELECT
            stage_head.source_lineage_id,
            stage_head.current_stage_version_id
        FROM lineage_stage_heads AS stage_head
        JOIN claim_occurrences AS occurrence
          ON occurrence.source_lineage_id = stage_head.source_lineage_id
        WHERE occurrence.occurrence_version_id = ?
          AND stage_head.stage_kind = ?
        """,
        (occurrence_version_id, CONCEPT_LINKING_STAGE_KIND),
    ).fetchone()
    if stage_row is not None:
        with _immediate_transaction(connection):
            _write_card_relations(
                connection,
                source_lineage_id=stage_row["source_lineage_id"],
                stage_version_id=stage_row["current_stage_version_id"],
            )
        _publish_relation_dependency(
            connection,
            source_lineage_id=stage_row["source_lineage_id"],
            stage_version_id=stage_row["current_stage_version_id"],
        )


def list_pending_ambiguities(
    connection: sqlite3.Connection,
) -> tuple[dict[str, Any], ...]:
    """Return unresolved claim-specific candidate sets for the reviewer."""
    rows = connection.execute(
        """
        SELECT
            candidate.occurrence_version_id,
            candidate.normalized_surface,
            candidate.candidate_concept_id,
            candidate.reason,
            candidate.confidence,
            occurrence.exact_occurrence_payload_json,
            concept_revision.canonical_payload_json AS concept_payload_json
        FROM metonym_candidates AS candidate
        JOIN effective_active_occurrences AS occurrence
          ON occurrence.occurrence_version_id = candidate.occurrence_version_id
        JOIN approved_concepts AS concept
          ON concept.concept_id = candidate.candidate_concept_id
        JOIN concept_revisions AS concept_revision
          ON concept_revision.concept_revision_id =
             concept.current_concept_revision_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM metonym_overrides AS decision
            WHERE decision.occurrence_version_id =
                  candidate.occurrence_version_id
              AND decision.normalized_surface = candidate.normalized_surface
              AND decision.override_status = 'active'
        )
        ORDER BY
            candidate.occurrence_version_id,
            candidate.normalized_surface,
            candidate.candidate_concept_id
        """
    ).fetchall()
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["occurrence_version_id"], row["normalized_surface"])
        item = grouped.setdefault(
            key,
            {
                "occurrence_version_id": row["occurrence_version_id"],
                "normalized_surface": row["normalized_surface"],
                "occurrence_payload": json.loads(
                    row["exact_occurrence_payload_json"]
                ),
                "reason": row["reason"],
                "candidates": [],
            },
        )
        concept_payload = json.loads(row["concept_payload_json"])
        item["candidates"].append(
            {
                "concept_id": row["candidate_concept_id"],
                "canonical_label": concept_payload.get("canonical_label"),
                "source_category": concept_payload.get("source_category"),
                "confidence": row["confidence"],
            }
        )
    return tuple(grouped[key] for key in sorted(grouped))


def _apply_relation_output(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    stage_version_id: str,
    candidate_snapshot: CandidateSnapshot,
    role_overrides: Mapping[
        tuple[str, str],
        tuple[RelationRole, str, str],
    ] | None = None,
    metonym_links: Mapping[tuple[str, str], _DesiredLink] | None = None,
    metonym_ambiguities: Sequence[
        tuple[str, _StructuredSurface, str, str]
    ] = (),
) -> _RelationOutputCounts:
    with _immediate_transaction(connection):
        if not _stage_is_current(connection, source_lineage_id, stage_version_id):
            raise RelationError("Concept-linking stage became stale before output apply")
        active_occurrences = connection.execute(
            """
            SELECT
                occurrence_version_id,
                exact_occurrence_payload_json,
                occurrence_fingerprint
            FROM lifecycle_active_occurrences
            WHERE source_lineage_id = ?
            ORDER BY occurrence_version_id
            """,
            (source_lineage_id,),
        ).fetchall()
        occurrence_payloads = {
            row["occurrence_version_id"]: json.loads(
                row["exact_occurrence_payload_json"]
            )
            for row in active_occurrences
        }
        surfaces = tuple(
            _surface_from_snapshot(value)
            for value in candidate_snapshot.surfaces
        )
        desired, ambiguities = _resolve_desired_links(
            connection,
            occurrences=active_occurrences,
            occurrence_payloads=occurrence_payloads,
            surfaces=surfaces,
        )
        for key, (role, explanation, resolver_version) in (
            role_overrides or {}
        ).items():
            current = desired.get(key)
            if current is None:
                continue
            desired[key] = replace(
                current,
                relation_role=role,
                rule_id="luna_relation_role",
                explanation=explanation,
                confidence=0.85,
                rule_inputs={
                    **current.rule_inputs,
                    "luna_relation_role": role,
                    "luna_prompt_template_version": LUNA_RELATION_PROMPT_VERSION,
                },
                resolver_version=resolver_version,
            )
        for key, link in (metonym_links or {}).items():
            current = desired.get(key)
            if current is None or _ROLE_STRENGTH[link.relation_role] > _ROLE_STRENGTH[
                current.relation_role
            ]:
                desired[key] = link
        ambiguities.extend(metonym_ambiguities)
        active_links, absent_links = _write_link_versions(
            connection,
            source_lineage_id=source_lineage_id,
            stage_version_id=stage_version_id,
            desired=desired,
            active_occurrence_ids=set(occurrence_payloads),
        )
        ambiguity_count = _write_ambiguity_candidates(
            connection,
            stage_version_id=stage_version_id,
            ambiguities=ambiguities,
        )
        active_card, absent_card = _write_card_relations(
            connection,
            source_lineage_id=source_lineage_id,
            stage_version_id=stage_version_id,
        )
    return _RelationOutputCounts(
        active_links=active_links,
        absent_links=absent_links,
        ambiguity_candidates=ambiguity_count,
        active_card_relations=active_card,
        absent_card_relations=absent_card,
    )


def _resolve_desired_links(
    connection: sqlite3.Connection,
    *,
    occurrences: Sequence[sqlite3.Row],
    occurrence_payloads: Mapping[str, dict[str, Any]],
    surfaces: Sequence[_StructuredSurface],
) -> tuple[
    dict[tuple[str, str], _DesiredLink],
    list[tuple[str, _StructuredSurface, str, str]],
]:
    desired: dict[tuple[str, str], _DesiredLink] = {}
    ambiguities: list[tuple[str, _StructuredSurface, str, str]] = []
    first_occurrence_id = (
        None if not occurrences else occurrences[0]["occurrence_version_id"]
    )
    for surface in surfaces:
        candidates = _category_filtered_candidates(
            connection,
            candidate_ids=surface.candidate_concept_ids,
            field=surface.field,
            source_category=surface.source_category,
        )
        if not candidates or first_occurrence_id is None:
            continue
        if len(candidates) > 1:
            matching_occurrences = [
                occurrence_id
                for occurrence_id, payload in occurrence_payloads.items()
                if _payload_mentions(payload, (surface.display_surface,))
            ] or [first_occurrence_id]
            for occurrence_id in matching_occurrences:
                for concept_id in candidates:
                    ambiguities.append(
                        (
                            occurrence_id,
                            surface,
                            concept_id,
                            "ambiguous approved identity surface; no automatic guess",
                        )
                    )
            continue

        concept_id = candidates[0]
        aliases = tuple(
            row["display_surface"]
            for row in connection.execute(
                """
                SELECT display_surface
                FROM identity_aliases
                WHERE concept_id = ?
                ORDER BY normalized_surface, display_surface
                """,
                (concept_id,),
            ).fetchall()
        ) or (surface.display_surface,)
        matching_occurrences = [
            occurrence_id
            for occurrence_id, payload in occurrence_payloads.items()
            if _payload_mentions(payload, aliases)
        ]
        if surface.field == "topic" and surface.salience == "primary":
            target_occurrences = list(occurrence_payloads)
            role: RelationRole = "subject"
            rule_id = "primary_topic_direct"
            explanation = (
                "Approved primary topic applies to the card's active claim set"
            )
            confidence = 0.95
        elif matching_occurrences:
            target_occurrences = matching_occurrences
            role = _role_for_surface(
                surface,
                [occurrence_payloads[value] for value in matching_occurrences],
                aliases,
            )
            rule_id = "structured_surface_exact_mention"
            explanation = (
                "Approved identity alias appears in the occurrence and the "
                "structured card field supplies salience/role"
            )
            confidence = 1.0 if role in {"subject", "actor", "object"} else 0.85
        else:
            target_occurrences = [first_occurrence_id]
            role = (
                "mentioned"
                if surface.salience == "mentioned"
                else "context"
            )
            rule_id = "structured_surface_safe_fallback"
            explanation = (
                "Structured card surface has no exact occurrence mention; "
                "primary entity without a proven role safely falls back to context"
            )
            confidence = 0.65

        for occurrence_id in target_occurrences:
            link = _DesiredLink(
                occurrence_version_id=occurrence_id,
                concept_id=concept_id,
                relation_role=role,
                source_locator=surface.locator,
                rule_id=rule_id,
                explanation=explanation,
                confidence=confidence,
                rule_inputs={
                    "surface": surface.normalized_surface,
                    "candidate_concept_ids": list(candidates),
                    "salience": surface.salience,
                    "source_role": surface.source_role,
                    "field": surface.field,
                    "source_category": surface.source_category,
                    "occurrence_version_id": occurrence_id,
                },
            )
            key = (occurrence_id, concept_id)
            current = desired.get(key)
            if current is None or _ROLE_STRENGTH[role] > _ROLE_STRENGTH[
                current.relation_role
            ]:
                desired[key] = link
    return desired, ambiguities


def _write_link_versions(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    stage_version_id: str,
    desired: Mapping[tuple[str, str], _DesiredLink],
    active_occurrence_ids: set[str],
) -> tuple[int, int]:
    prior_keys = {
        (row["occurrence_version_id"], row["concept_id"])
        for row in connection.execute(
            """
            SELECT DISTINCT link.occurrence_version_id, link.concept_id
            FROM occurrence_concept_automatic_links AS link
            JOIN claim_occurrences AS occurrence
              ON occurrence.occurrence_version_id = link.occurrence_version_id
            WHERE occurrence.source_lineage_id = ?
              AND link.occurrence_version_id IN (
                  SELECT occurrence_version_id
                  FROM lifecycle_active_occurrences
                  WHERE source_lineage_id = ?
              )
            """,
            (source_lineage_id, source_lineage_id),
        ).fetchall()
    }
    active_written = 0
    absent_written = 0
    for key in sorted(set(desired) | prior_keys):
        occurrence_id, concept_id = key
        if occurrence_id not in active_occurrence_ids:
            continue
        status = "active" if key in desired else "absent"
        link = desired.get(key)
        role: RelationRole = "unknown" if link is None else link.relation_role
        rule_inputs = (
            {
                "occurrence_version_id": occurrence_id,
                "concept_id": concept_id,
                "reason": "not_present_in_current_relation_output",
            }
            if link is None
            else link.rule_inputs
        )
        rule_inputs_hash = content_hash(
            rule_inputs,
            namespace="wiki-v2-concept-link-rule-inputs",
            unordered_collection_paths=(("candidate_concept_ids",),),
        )
        latest = connection.execute(
            """
            SELECT
                automatic_generation,
                link_status,
                relation_role,
                rule_inputs_hash,
                resolver_version
            FROM occurrence_concept_automatic_links
            WHERE occurrence_version_id = ? AND concept_id = ?
            ORDER BY automatic_generation DESC
            LIMIT 1
            """,
            (occurrence_id, concept_id),
        ).fetchone()
        resolver_version = (
            "deterministic-structured-resolver-v1"
            if link is None
            else link.resolver_version
        )
        if (
            latest is not None
            and latest["link_status"] == status
            and latest["relation_role"] == role
            and latest["rule_inputs_hash"] == rule_inputs_hash
            and latest["resolver_version"] == resolver_version
        ):
            continue
        generation = 1 if latest is None else int(latest["automatic_generation"]) + 1
        automatic_id = _stable_id(
            "automatic-concept-link",
            {
                "occurrence_version_id": occurrence_id,
                "concept_id": concept_id,
                "generation": generation,
                "link_status": status,
            },
        )
        connection.execute(
            """
            INSERT INTO occurrence_concept_automatic_links (
                automatic_link_id,
                occurrence_version_id,
                concept_id,
                automatic_generation,
                link_status,
                relation_role,
                source_locator_json,
                resolver_version,
                rule_id,
                rule_version,
                rule_inputs_json,
                rule_inputs_hash,
                explanation,
                confidence,
                produced_by_stage_version_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                automatic_id,
                occurrence_id,
                concept_id,
                generation,
                status,
                role,
                canonical_json({} if link is None else link.source_locator),
                resolver_version,
                "relation_removed" if link is None else link.rule_id,
                "v1",
                canonical_json(rule_inputs),
                rule_inputs_hash,
                (
                    "Link absent from current deterministic output"
                    if link is None
                    else link.explanation
                ),
                None if link is None else link.confidence,
                stage_version_id,
                _utc_now(),
            ),
        )
        if status == "active":
            active_written += 1
        else:
            absent_written += 1
    return active_written, absent_written


def _write_ambiguity_candidates(
    connection: sqlite3.Connection,
    *,
    stage_version_id: str,
    ambiguities: Sequence[tuple[str, _StructuredSurface, str, str]],
) -> int:
    written = 0
    for occurrence_id, surface, concept_id, reason in sorted(
        ambiguities,
        key=lambda item: (
            item[0],
            item[1].normalized_surface,
            item[2],
        ),
    ):
        existing = connection.execute(
            """
            SELECT 1
            FROM metonym_candidates
            WHERE occurrence_version_id = ?
              AND normalized_surface = ?
              AND candidate_concept_id = ?
              AND produced_by_stage_version_id = ?
            """,
            (
                occurrence_id,
                surface.normalized_surface,
                concept_id,
                stage_version_id,
            ),
        ).fetchone()
        if existing is not None:
            continue
        generation = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(candidate_generation), 0) + 1
                FROM metonym_candidates
                WHERE occurrence_version_id = ?
                  AND normalized_surface = ?
                  AND candidate_concept_id = ?
                """,
                (occurrence_id, surface.normalized_surface, concept_id),
            ).fetchone()[0]
        )
        candidate_id = _stable_id(
            "claim-resolution-candidate",
            {
                "occurrence_version_id": occurrence_id,
                "normalized_surface": surface.normalized_surface,
                "concept_id": concept_id,
                "generation": generation,
            },
        )
        connection.execute(
            """
            INSERT INTO metonym_candidates (
                metonym_candidate_id,
                occurrence_version_id,
                normalized_surface,
                candidate_concept_id,
                candidate_generation,
                reason,
                confidence,
                produced_by_stage_version_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                occurrence_id,
                surface.normalized_surface,
                concept_id,
                generation,
                reason,
                0.5,
                stage_version_id,
                _utc_now(),
            ),
        )
        written += 1
    return written


def _write_card_relations(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    stage_version_id: str,
) -> tuple[int, int]:
    card_row = connection.execute(
        """
        SELECT current_card_revision_id
        FROM source_lineage_heads
        WHERE source_lineage_id = ?
        """,
        (source_lineage_id,),
    ).fetchone()
    if card_row is None:
        raise RelationError(f"Lineage {source_lineage_id} has no card head")
    card_revision_id = card_row["current_card_revision_id"]
    link_rows = connection.execute(
        """
        SELECT
            link.occurrence_version_id,
            link.concept_id,
            link.relation_role
        FROM effective_occurrence_concept_links AS link
        JOIN lifecycle_active_occurrences AS occurrence
          ON occurrence.occurrence_version_id = link.occurrence_version_id
        WHERE occurrence.source_lineage_id = ?
        ORDER BY
            link.concept_id,
            link.occurrence_version_id,
            link.relation_role
        """,
        (source_lineage_id,),
    ).fetchall()
    by_concept: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in link_rows:
        by_concept[row["concept_id"]].append(row)
    prior_concepts = {
        row["concept_id"]
        for row in connection.execute(
            """
            SELECT DISTINCT concept_id
            FROM card_relations
            WHERE card_revision_id = ?
            """,
            (card_revision_id,),
        ).fetchall()
    }
    active_written = 0
    absent_written = 0
    for concept_id in sorted(set(by_concept) | prior_concepts):
        rows = by_concept.get(concept_id, [])
        status = "active" if rows else "absent"
        strongest: RelationRole = (
            "unknown"
            if not rows
            else max(
                (row["relation_role"] for row in rows),
                key=lambda role: _ROLE_STRENGTH[role],
            )
        )
        relation_kind: RelationKind
        if strongest in {"subject", "actor", "object"}:
            relation_kind = "direct"
        elif strongest in {"context", "unknown"}:
            relation_kind = "context"
        else:
            relation_kind = "mentioned"
        inputs = {
            "card_revision_id": card_revision_id,
            "concept_id": concept_id,
            "contributors": [
                {
                    "occurrence_version_id": row["occurrence_version_id"],
                    "relation_role": row["relation_role"],
                }
                for row in rows
            ],
        }
        inputs_hash = content_hash(
            inputs,
            namespace="wiki-v2-card-relation-inputs",
            unordered_collection_paths=(("contributors",),),
        )
        latest = connection.execute(
            """
            SELECT
                relation_generation,
                relation_status,
                relation_inputs_hash
            FROM card_relations
            WHERE card_revision_id = ? AND concept_id = ?
            ORDER BY relation_generation DESC
            LIMIT 1
            """,
            (card_revision_id, concept_id),
        ).fetchone()
        if (
            latest is not None
            and latest["relation_status"] == status
            and latest["relation_inputs_hash"] == inputs_hash
        ):
            continue
        generation = 1 if latest is None else int(latest["relation_generation"]) + 1
        relation_id = _stable_id(
            "card-relation",
            {
                "card_revision_id": card_revision_id,
                "concept_id": concept_id,
                "generation": generation,
                "status": status,
            },
        )
        connection.execute(
            """
            INSERT INTO card_relations (
                card_relation_id,
                card_revision_id,
                concept_id,
                relation_generation,
                relation_status,
                relation_kind,
                strongest_relation_role,
                relation_inputs_hash,
                produced_by_stage_version_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relation_id,
                card_revision_id,
                concept_id,
                generation,
                status,
                relation_kind,
                strongest,
                inputs_hash,
                stage_version_id,
                _utc_now(),
            ),
        )
        for row in rows:
            connection.execute(
                """
                INSERT INTO card_relation_contributors (
                    card_relation_id,
                    occurrence_version_id,
                    contribution_role
                ) VALUES (?, ?, ?)
                """,
                (
                    relation_id,
                    row["occurrence_version_id"],
                    row["relation_role"],
                ),
            )
        if status == "active":
            active_written += 1
        else:
            absent_written += 1
    return active_written, absent_written


def _publish_relation_dependency(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    stage_version_id: str,
) -> bool:
    links = connection.execute(
        """
        SELECT
            link.occurrence_version_id,
            link.concept_id,
            link.relation_role,
            link.link_source
        FROM effective_occurrence_concept_links AS link
        JOIN lifecycle_active_occurrences AS occurrence
          ON occurrence.occurrence_version_id = link.occurrence_version_id
        WHERE occurrence.source_lineage_id = ?
        ORDER BY link.occurrence_version_id, link.concept_id
        """,
        (source_lineage_id,),
    ).fetchall()
    relations = connection.execute(
        """
        SELECT
            relation.card_revision_id,
            relation.concept_id,
            relation.relation_kind,
            relation.strongest_relation_role
        FROM effective_card_relations AS relation
        JOIN source_lineage_heads AS card_head
          ON card_head.current_card_revision_id = relation.card_revision_id
        WHERE card_head.source_lineage_id = ?
        ORDER BY relation.concept_id
        """,
        (source_lineage_id,),
    ).fetchall()
    current = get_dependency_head(
        connection,
        dependency_kind="effective_concept_links",
        dependency_scope_key=source_lineage_id,
    )
    published = publish_dependency(
        connection,
        dependency_kind="effective_concept_links",
        dependency_scope_key=source_lineage_id,
        payload={
            "source_lineage_id": source_lineage_id,
            "occurrence_links": [dict(row) for row in links],
            "card_relations": [dict(row) for row in relations],
        },
        expected_version_id=(
            None if current is None else current.dependency_version_id
        ),
        producer_kind="stage",
        produced_by_stage_version_id=stage_version_id,
        unordered_collection_paths=(
            ("occurrence_links",),
            ("card_relations",),
        ),
    )
    return published.changed


def _load_structured_input(
    connection: sqlite3.Connection,
    source_lineage_id: str,
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT input.canonical_payload_json
        FROM lineage_input_heads AS head
        JOIN lineage_input_versions AS input
          ON input.input_version_id = head.current_input_version_id
        WHERE head.source_lineage_id = ?
          AND head.input_kind = 'structured_relation_inputs'
        """,
        (source_lineage_id,),
    ).fetchone()
    if row is None:
        raise RelationError(
            f"Lineage {source_lineage_id} has no structured relation input"
        )
    return json.loads(row["canonical_payload_json"])


def _surface_blueprints(
    connection: sqlite3.Connection,
    structured: Mapping[str, Any],
) -> tuple[_StructuredSurface, ...]:
    surfaces: list[_StructuredSurface] = []
    entities = structured.get("entities") or {}
    if isinstance(entities, Mapping):
        for category in sorted(entities):
            if category in EXCLUDED_ENTITY_CATEGORIES:
                continue
            values = entities.get(category)
            if not isinstance(values, list):
                continue
            for ordinal, raw in enumerate(values):
                if not isinstance(raw, Mapping):
                    continue
                display = normalize_text(str(raw.get("text") or ""))
                normalized = normalize_surface(display)
                if not normalized:
                    continue
                surfaces.append(
                    _StructuredSurface(
                        normalized_surface=normalized,
                        display_surface=display,
                        field="entity",
                        source_category=str(category),
                        salience=str(raw.get("salience") or "mentioned"),
                        source_role=str(raw.get("role") or ""),
                        locator={
                            "field": "entities",
                            "category": str(category),
                            "content_fingerprint": content_hash(
                                dict(raw),
                                namespace="wiki-v2-structured-surface",
                            ),
                            "diagnostic_index": ordinal,
                        },
                        candidate_concept_ids=_surface_candidates(
                            connection, normalized
                        ),
                    )
                )
    topics = structured.get("topics") or []
    if isinstance(topics, list):
        for ordinal, raw in enumerate(topics):
            if not isinstance(raw, Mapping):
                continue
            display = normalize_text(str(raw.get("label") or ""))
            normalized = normalize_surface(display)
            if not normalized:
                continue
            surfaces.append(
                _StructuredSurface(
                    normalized_surface=normalized,
                    display_surface=display,
                    field="topic",
                    source_category=str(raw.get("type") or "other"),
                    salience=str(raw.get("salience") or "mentioned"),
                    source_role="topic",
                    locator={
                        "field": "topics",
                        "topic_type": str(raw.get("type") or "other"),
                        "content_fingerprint": content_hash(
                            dict(raw),
                            namespace="wiki-v2-structured-surface",
                        ),
                        "diagnostic_index": ordinal,
                    },
                    candidate_concept_ids=_surface_candidates(
                        connection, normalized
                    ),
                )
            )
    return tuple(
        sorted(
            surfaces,
            key=lambda value: (
                value.field,
                value.normalized_surface,
                value.source_category,
                canonical_json(value.locator),
            ),
        )
    )


def _surface_from_snapshot(value: Mapping[str, Any]) -> _StructuredSurface:
    return _StructuredSurface(
        normalized_surface=str(value["normalized_surface"]),
        display_surface=str(value["display_surface"]),
        field=value["field"],
        source_category=str(value["source_category"]),
        salience=str(value["salience"]),
        source_role=str(value["source_role"]),
        locator=dict(value["source_locator"]),
        candidate_concept_ids=tuple(
            str(item) for item in value["candidate_concept_ids"]
        ),
    )


def _surface_candidates(
    connection: sqlite3.Connection,
    normalized_surface: str,
) -> tuple[str, ...]:
    row = connection.execute(
        """
        SELECT revision.candidate_concept_ids_json
        FROM surface_heads AS head
        JOIN surface_revisions AS revision
          ON revision.surface_revision_id = head.current_surface_revision_id
        WHERE head.normalized_surface = ?
        """,
        (normalized_surface,),
    ).fetchone()
    if row is None:
        return ()
    return tuple(sorted(str(value) for value in json.loads(row[0])))


def _category_filtered_candidates(
    connection: sqlite3.Connection,
    *,
    candidate_ids: Sequence[str],
    field: str,
    source_category: str,
) -> tuple[str, ...]:
    if len(candidate_ids) <= 1:
        return tuple(candidate_ids)
    rows = connection.execute(
        f"""
        SELECT
            concept.concept_id,
            concept.concept_kind,
            revision.canonical_payload_json
        FROM approved_concepts AS concept
        JOIN concept_revisions AS revision
          ON revision.concept_revision_id = concept.current_concept_revision_id
        WHERE concept.concept_id IN ({",".join("?" for _ in candidate_ids)})
        ORDER BY concept.concept_id
        """,
        tuple(candidate_ids),
    ).fetchall()
    expected_kind = "topic" if field == "topic" else "entity"
    exact = [
        row["concept_id"]
        for row in rows
        if row["concept_kind"] == expected_kind
        and str(json.loads(row["canonical_payload_json"]).get("source_category"))
        == source_category
    ]
    return tuple(exact if exact else candidate_ids)


def _role_for_surface(
    surface: _StructuredSurface,
    payloads: Sequence[Mapping[str, Any]],
    aliases: Sequence[str],
) -> RelationRole:
    explicit = _normalize_structured_role(surface.source_role)
    if explicit is not None:
        return explicit
    if any(_speaker_or_actor_matches(payload, aliases) for payload in payloads):
        return "actor"
    if surface.salience == "mentioned":
        return "mentioned"
    # Frozen safety rule: a primary entity without a proven role is context,
    # never direct merely because an LLM marked it primary.
    return "context"


def _normalize_structured_role(value: str) -> RelationRole | None:
    normalized = normalize_surface(value)
    mapping: dict[str, RelationRole] = {
        "subject": "subject",
        "субъект": "subject",
        "инициатор": "subject",
        "actor": "actor",
        "speaker": "actor",
        "участник": "actor",
        "заявитель": "actor",
        "object": "object",
        "target": "object",
        "цель": "object",
        "объект": "object",
        "context": "context",
        "контекст": "context",
        "mentioned": "mentioned",
        "упоминание": "mentioned",
    }
    return mapping.get(normalized)


def _speaker_or_actor_matches(
    payload: Mapping[str, Any],
    aliases: Sequence[str],
) -> bool:
    values: list[str] = []
    speaker = payload.get("speaker")
    if isinstance(speaker, str):
        values.append(speaker)
    actors = payload.get("actors")
    if isinstance(actors, list):
        values.extend(str(value) for value in actors)
    normalized_aliases = {normalize_surface(alias) for alias in aliases}
    return any(normalize_surface(value) in normalized_aliases for value in values)


def _payload_mentions(
    payload: Mapping[str, Any],
    aliases: Sequence[str],
) -> bool:
    fragments: list[str] = []
    for key in (
        "text",
        "description",
        "speaker",
        "location",
        "context",
        "evidence",
    ):
        value = payload.get(key)
        if isinstance(value, str):
            fragments.append(value)
    actors = payload.get("actors")
    if isinstance(actors, list):
        fragments.extend(str(value) for value in actors)
    haystack = _search_normalize(" ".join(fragments))
    for alias in aliases:
        needle = _search_normalize(alias)
        if needle and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack):
            return True
    return False


def _search_normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _insert_link_override_in_transaction(
    connection: sqlite3.Connection,
    *,
    occurrence_version_id: str,
    concept_id: str,
    relation_role: RelationRole | None,
    occurrence_fingerprint: str,
    rationale: str,
) -> None:
    generation = int(
        connection.execute(
            """
            SELECT COALESCE(MAX(decision_generation), 0) + 1
            FROM occurrence_concept_link_overrides
            WHERE occurrence_version_id = ? AND concept_id = ?
            """,
            (occurrence_version_id, concept_id),
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO occurrence_concept_link_overrides (
            concept_link_override_id,
            occurrence_version_id,
            concept_id,
            decision_generation,
            action,
            relation_role,
            occurrence_fingerprint,
            override_status,
            rationale,
            decided_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        (
            _stable_id(
                "concept-link-override",
                {
                    "occurrence_version_id": occurrence_version_id,
                    "concept_id": concept_id,
                    "generation": generation,
                    "relation_role": relation_role,
                },
            ),
            occurrence_version_id,
            concept_id,
            generation,
            "exclude" if relation_role is None else "include",
            relation_role,
            occurrence_fingerprint,
            normalize_text(rationale) or None,
            _utc_now(),
        ),
    )


def _relation_contract(
    contract: ProcessorContractSpec,
    *,
    use_luna: bool,
) -> ProcessorContractSpec:
    if not use_luna:
        return contract
    from geospoiler.providers import llm_backend

    return replace(
        contract,
        prompt_template_version=LUNA_RELATION_PROMPT_VERSION,
        model_profile_version=llm_backend.active_model_for("default"),
        extra={**dict(contract.extra), "luna_role_resolution": True},
    )


def _luna_resolutions(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    candidate_snapshot: CandidateSnapshot,
) -> _LunaResolutionBundle:
    from geospoiler.providers import llm_backend

    if not llm_backend.is_luna_role("default"):
        return _LunaResolutionBundle(
            role_overrides={},
            metonym_links={},
            metonym_ambiguities=(),
            error="Luna profile is not active; safe deterministic roles were used",
        )
    occurrence_rows = connection.execute(
        """
        SELECT occurrence_version_id, exact_occurrence_payload_json
        FROM lifecycle_active_occurrences
        WHERE source_lineage_id = ?
        ORDER BY occurrence_version_id
        """,
        (source_lineage_id,),
    ).fetchall()
    payloads = {
        row["occurrence_version_id"]: json.loads(
            row["exact_occurrence_payload_json"]
        )
        for row in occurrence_rows
    }
    surfaces = tuple(
        _surface_from_snapshot(value) for value in candidate_snapshot.surfaces
    )
    metonym_surfaces = tuple(
        surface
        for surface in surfaces
        if surface.field == "entity"
        and surface.source_role.casefold() not in {"subject", "actor", "object"}
        and surface.salience.casefold() != "mentioned"
    )
    desired, _ = _resolve_desired_links(
        connection,
        occurrences=occurrence_rows,
        occurrence_payloads=payloads,
        surfaces=surfaces,
    )
    requests: list[dict[str, Any]] = []
    for key, link in sorted(desired.items()):
        if link.relation_role not in {"context", "unknown"}:
            continue
        concept_row = connection.execute(
            """
            SELECT revision.canonical_payload_json
            FROM approved_concepts AS concept
            JOIN concept_revisions AS revision
              ON revision.concept_revision_id =
                 concept.current_concept_revision_id
            WHERE concept.concept_id = ?
            """,
            (link.concept_id,),
        ).fetchone()
        if concept_row is None:
            continue
        concept_payload = json.loads(concept_row["canonical_payload_json"])
        requests.append(
            {
                "occurrence_version_id": key[0],
                "concept_id": key[1],
                "concept_label": concept_payload.get("canonical_label"),
                "occurrence": payloads[key[0]],
                "structured_role": link.rule_inputs.get("source_role"),
                "salience": link.rule_inputs.get("salience"),
                "safe_default_role": link.relation_role,
            }
        )
    if not payloads or (not requests and not metonym_surfaces):
        return _LunaResolutionBundle(
            role_overrides={},
            metonym_links={},
            metonym_ambiguities=(),
        )
    available_concepts: list[dict[str, Any]] = []
    for row in connection.execute(
        """
        SELECT
            concept.concept_id,
            revision.canonical_payload_json
        FROM approved_concepts AS concept
        JOIN concept_revisions AS revision
          ON revision.concept_revision_id = concept.current_concept_revision_id
        ORDER BY concept.concept_id
        LIMIT 300
        """
    ).fetchall():
        concept_payload = json.loads(row["canonical_payload_json"])
        aliases = [
            alias["display_surface"]
            for alias in connection.execute(
                """
                SELECT display_surface
                FROM identity_aliases
                WHERE concept_id = ?
                ORDER BY normalized_surface, display_surface
                """,
                (row["concept_id"],),
            ).fetchall()
        ]
        available_concepts.append(
            {
                "concept_id": row["concept_id"],
                "canonical_label": concept_payload.get("canonical_label"),
                "source_category": concept_payload.get("source_category"),
                "aliases": aliases,
            }
        )
    prompt = f"""
Определи роль уже одобренного Wiki concept в каждом конкретном claim.

Допустимые роли:
- subject: о concept непосредственно утверждается claim;
- actor: concept действует/говорит/участвует;
- object: действие направлено на concept;
- context: concept задаёт контекст, но claim не утверждает о нём напрямую;
- mentioned: только упоминание;
- unknown: доказательств недостаточно.

Не меняй concept_id и occurrence_version_id. Не превращай primary entity в
subject только из-за salience. Если роль не доказана самим claim, оставь
context/mentioned/unknown. Это решение относится только к claim и никогда не
создаёт alias.

Role requests:
{canonical_json(requests)}

Дополнительно проверь только явные контекстные метонимы в claims. Например,
«Москва заявила» может обозначать государственный орган, но «встреча прошла в
Москве» обозначает город. Метоним:
- относится только к конкретному occurrence;
- никогда не становится alias;
- должен выбирать один наиболее точный approved concept;
- если выбор неоднозначен, верни status=ambiguous и все реальные candidates;
- нельзя автоматически добавлять сразу государство, правительство и лидера.

Structured surfaces in this card:
{canonical_json([
    {
        "normalized_surface": surface.normalized_surface,
        "display_surface": surface.display_surface,
        "field": surface.field,
        "source_category": surface.source_category,
        "salience": surface.salience,
        "source_role": surface.source_role,
        "candidate_concept_ids": list(surface.candidate_concept_ids),
        "source_locator": surface.locator,
    }
    for surface in metonym_surfaces
])}

Active occurrences:
{canonical_json([
    {"occurrence_version_id": occurrence_id, "payload": payload}
    for occurrence_id, payload in payloads.items()
])}

Available approved concepts (используй только эти IDs):
{canonical_json(available_concepts)}
""".strip()
    try:
        response = llm_backend.complete_json_sync(
            [
                {
                    "role": "system",
                    "content": (
                        "Resolve relation roles for supplied claim/concept pairs only. "
                        "Return conservative structured JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            role="default",
            schema=_luna_resolution_schema(),
        )
    except Exception as exc:
        return _LunaResolutionBundle(
            role_overrides={},
            metonym_links={},
            metonym_ambiguities=(),
            error=str(exc),
        )
    valid_keys = {
        (item["occurrence_version_id"], item["concept_id"]) for item in requests
    }
    resolver_version = (
        f"{llm_backend.active_model_for('default')}:{LUNA_RELATION_PROMPT_VERSION}"
    )
    overrides: dict[tuple[str, str], tuple[RelationRole, str, str]] = {}
    for raw in response.get("resolutions", []):
        key = (
            str(raw.get("occurrence_version_id") or ""),
            str(raw.get("concept_id") or ""),
        )
        role = str(raw.get("relation_role") or "")
        explanation = str(raw.get("explanation") or "").strip()
        if key not in valid_keys or role not in _ROLE_STRENGTH or not explanation:
            continue
        overrides[key] = (role, explanation, resolver_version)  # type: ignore[assignment]
    concept_ids = {item["concept_id"] for item in available_concepts}
    surface_by_normalized = {
        surface.normalized_surface: surface for surface in metonym_surfaces
    }
    metonym_links: dict[tuple[str, str], _DesiredLink] = {}
    metonym_ambiguities: list[
        tuple[str, _StructuredSurface, str, str]
    ] = []
    for raw in response.get("metonym_resolutions", []):
        occurrence_id = str(raw.get("occurrence_version_id") or "")
        normalized = normalize_surface(str(raw.get("surface") or ""))
        status = str(raw.get("status") or "")
        selected_concept_id = str(raw.get("selected_concept_id") or "")
        candidate_ids = tuple(
            sorted(
                {
                    str(value)
                    for value in raw.get("candidate_concept_ids", [])
                    if str(value) in concept_ids
                }
            )
        )
        role = str(raw.get("relation_role") or "")
        explanation = str(raw.get("explanation") or "").strip()
        surface = surface_by_normalized.get(normalized)
        if (
            occurrence_id not in payloads
            or surface is None
            or role not in _ROLE_STRENGTH
            or not explanation
            or status not in {"resolved", "ambiguous"}
        ):
            continue
        if status == "resolved":
            if (
                selected_concept_id not in concept_ids
                or selected_concept_id not in candidate_ids
                or _surface_is_identity_alias(
                    connection,
                    normalized_surface=normalized,
                    concept_id=selected_concept_id,
                )
            ):
                continue
            metonym_links[(occurrence_id, selected_concept_id)] = _DesiredLink(
                occurrence_version_id=occurrence_id,
                concept_id=selected_concept_id,
                relation_role=role,  # type: ignore[arg-type]
                source_locator={
                    **surface.locator,
                    "contextual_surface": surface.display_surface,
                    "resolution_kind": "claim_metonym",
                },
                rule_id="luna_claim_metonym",
                explanation=explanation,
                confidence=0.8,
                rule_inputs={
                    "occurrence_version_id": occurrence_id,
                    "normalized_surface": normalized,
                    "selected_concept_id": selected_concept_id,
                    "candidate_concept_ids": list(candidate_ids),
                    "prompt_template_version": LUNA_RELATION_PROMPT_VERSION,
                },
                resolver_version=resolver_version,
            )
        elif len(candidate_ids) >= 2:
            for concept_id in candidate_ids:
                metonym_ambiguities.append(
                    (
                        occurrence_id,
                        surface,
                        concept_id,
                        f"context-dependent metonym ambiguity: {explanation}",
                    )
                )
    return _LunaResolutionBundle(
        role_overrides=overrides,
        metonym_links=metonym_links,
        metonym_ambiguities=tuple(metonym_ambiguities),
    )


def _surface_is_identity_alias(
    connection: sqlite3.Connection,
    *,
    normalized_surface: str,
    concept_id: str,
) -> bool:
    return (
        connection.execute(
            """
            SELECT 1
            FROM identity_aliases
            WHERE normalized_surface = ? AND concept_id = ?
            """,
            (normalized_surface, concept_id),
        ).fetchone()
        is not None
    )


def _luna_resolution_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "resolutions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "occurrence_version_id": {"type": "string"},
                        "concept_id": {"type": "string"},
                        "relation_role": {
                            "type": "string",
                            "enum": list(_ROLE_STRENGTH),
                        },
                        "explanation": {"type": "string"},
                    },
                    "required": [
                        "occurrence_version_id",
                        "concept_id",
                        "relation_role",
                        "explanation",
                    ],
                },
            },
            "metonym_resolutions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "occurrence_version_id": {"type": "string"},
                        "surface": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["resolved", "ambiguous"],
                        },
                        "selected_concept_id": {"type": "string"},
                        "candidate_concept_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "relation_role": {
                            "type": "string",
                            "enum": list(_ROLE_STRENGTH),
                        },
                        "explanation": {"type": "string"},
                    },
                    "required": [
                        "occurrence_version_id",
                        "surface",
                        "status",
                        "selected_concept_id",
                        "candidate_concept_ids",
                        "relation_role",
                        "explanation",
                    ],
                },
            },
        },
        "required": ["resolutions", "metonym_resolutions"],
    }


def _stage_is_current(
    connection: sqlite3.Connection,
    source_lineage_id: str,
    stage_version_id: str,
) -> bool:
    return (
        connection.execute(
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
                        OR input_head.current_input_version_id <>
                           binding.input_version_id
                        OR input_head.current_input_generation <>
                           binding.input_generation
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
                        OR dependency_head.current_generation <>
                           binding.dependency_generation
                        OR dependency_head.current_hash <> binding.dependency_hash
                    )
              )
            """,
            (source_lineage_id, CONCEPT_LINKING_STAGE_KIND, stage_version_id),
        ).fetchone()
        is not None
    )


def _stable_id(prefix: str, payload: object) -> str:
    return f"{prefix}:v1:sha256:{sha256_hex(prefix + chr(10) + canonical_json(payload))}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
