"""Transactional revision, DAG, CAS, and outbox engine for Wiki v3."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from geospoiler.wiki.hashing import JsonPath, canonical_json, content_hash, normalize_text
from geospoiler.wiki.schema import ALLOWED_DEPENDENCY_KINDS

ProducerKind = Literal["ingest", "manual", "registry", "stage"]
StageRunStatus = Literal["started", "committed", "failed", "stale", "no_op"]


class DependencyKind(StrEnum):
    """Closed schema-v3 vocabulary for data-only dependency snapshots."""

    OCCURRENCE_SNAPSHOT = "occurrence_snapshot"
    APPROVED_IDENTITY_ALIAS_SNAPSHOT = "approved_identity_alias_snapshot"
    CANDIDATE_SNAPSHOT = "candidate_snapshot"
    REGISTRY_SNAPSHOT = "registry_snapshot"
    SURFACE_RESOLUTION = "surface_resolution"
    EFFECTIVE_CLAIM_GROUPS = "effective_claim_groups"
    EFFECTIVE_CONCEPT_LINKS = "effective_concept_links"
    ELIGIBILITY_STATE = "eligibility_state"
    MANUAL_SIDECAR = "manual_sidecar"
    CONCEPT_DISPLAY_SNAPSHOT = "concept_display_snapshot"
    HIERARCHY_SNAPSHOT = "hierarchy_snapshot"
    CARD_RELATION_SNAPSHOT = "card_relation_snapshot"
    CARD_PROJECTION_SNAPSHOT = "card_projection_snapshot"
    CLAIM_PROJECTION_SNAPSHOT = "claim_projection_snapshot"


if tuple(kind.value for kind in DependencyKind) != ALLOWED_DEPENDENCY_KINDS:
    raise RuntimeError("DependencyKind and SQLite dependency CHECK must stay identical")


class WikiStateError(RuntimeError):
    """Base exception for Wiki state-engine failures."""


class StateNotFoundError(WikiStateError):
    """Raised when a required lineage, head, contract, or stage is absent."""


class StaleHeadError(WikiStateError):
    """Raised when a compare-and-swap expected value is no longer current."""


class StateConflictError(WikiStateError):
    """Raised when immutable state conflicts with a requested write."""


class IdempotencyConflictError(WikiStateError):
    """Raised when one outbox key is reused with different content."""


@dataclass(frozen=True)
class SourceLineage:
    source_lineage_id: str
    source_kind: str
    external_key: str


@dataclass(frozen=True)
class InputHead:
    source_lineage_id: str
    input_kind: str
    input_version_id: str
    generation: int
    input_hash: str
    changed: bool = False


@dataclass(frozen=True)
class CardRevision:
    card_revision_id: str
    source_lineage_id: str
    content_hash: str
    card_head_generation: int
    input_heads: tuple[InputHead, ...]
    changed: bool


@dataclass(frozen=True)
class ProcessorContractSpec:
    """The processing method; policies belong here, never in data dependencies."""

    algorithm_version: str
    schema_version: str
    canonicalizer_version: str
    policy_version: str
    prompt_template_version: str | None = None
    model_profile_version: str | None = None
    builder_version: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "algorithm_version": self.algorithm_version,
            "schema_version": self.schema_version,
            "canonicalizer_version": self.canonicalizer_version,
            "policy_version": self.policy_version,
            "prompt_template_version": self.prompt_template_version,
            "model_profile_version": self.model_profile_version,
            "builder_version": self.builder_version,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True)
class ProcessorActivation:
    stage_kind: str
    activation_generation: int
    processor_contract_version_id: str
    contract_hash: str
    changed: bool = False


@dataclass(frozen=True, order=True)
class DependencyKey:
    """A data dependency address; processing policies are intentionally absent."""

    dependency_kind: DependencyKind | str
    dependency_scope_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "dependency_kind", _coerce_dependency_kind(self.dependency_kind))


@dataclass(frozen=True)
class DependencyHead:
    dependency_kind: DependencyKind
    dependency_scope_key: str
    dependency_version_id: str
    generation: int
    dependency_hash: str
    changed: bool = False


@dataclass(frozen=True)
class StageInputBinding:
    input_kind: str
    input_version_id: str
    generation: int
    input_hash: str


@dataclass(frozen=True)
class StageDependencyBinding:
    dependency_kind: DependencyKind
    dependency_scope_key: str
    dependency_version_id: str
    generation: int
    dependency_hash: str


@dataclass(frozen=True)
class StageVersion:
    stage_version_id: str
    source_lineage_id: str
    stage_kind: str
    generation: int
    stage_inputs_hash: str
    contract_activation_generation: int
    processor_contract_version_id: str
    processor_contract_hash: str
    input_bindings: tuple[StageInputBinding, ...]
    dependency_bindings: tuple[StageDependencyBinding, ...]
    changed: bool = False


@dataclass(frozen=True)
class StageRun:
    stage_run_id: str
    stage_version_id: str
    source_lineage_id: str
    stage_kind: str
    processor_contract_version_id: str
    idempotency_key: str
    status: StageRunStatus
    observed_stage_generation: int
    observed_contract_activation_generation: int
    commit_seq: int | None
    duplicate_of_stage_run_id: str | None
    artifact_source_card_revision_id: str | None
    applied_against_card_revision_id: str | None
    error_text: str | None


@dataclass(frozen=True)
class OutboxEventSpec:
    event_key: str
    event_kind: str
    aggregate_kind: str
    aggregate_key: str
    payload: Any


@dataclass(frozen=True)
class OutboxEvent:
    outbox_event_id: str
    event_key: str
    stage_run_id: str
    commit_seq: int
    event_kind: str
    aggregate_kind: str
    aggregate_key: str
    payload_json: str
    processed_at: str | None


def ensure_source_lineage(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    external_key: str,
    source_lineage_id: str | None = None,
) -> SourceLineage:
    """Create a stable source lineage or return its existing identity."""
    with _immediate_transaction(connection):
        row = connection.execute(
            """
            SELECT source_lineage_id, source_kind, external_key
            FROM source_lineages
            WHERE source_kind = ? AND external_key = ?
            """,
            (source_kind, external_key),
        ).fetchone()
        if row is None:
            lineage_id = source_lineage_id or deterministic_source_lineage_id(
                source_kind=source_kind,
                external_key=external_key,
            )
            connection.execute(
                """
                INSERT INTO source_lineages (
                    source_lineage_id, source_kind, external_key, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (lineage_id, source_kind, external_key, _utc_now()),
            )
            return SourceLineage(lineage_id, source_kind, external_key)
        if source_lineage_id is not None and row["source_lineage_id"] != source_lineage_id:
            raise StateConflictError(
                f"Lineage {source_kind}:{external_key} already has id {row['source_lineage_id']}"
            )
        return SourceLineage(row["source_lineage_id"], row["source_kind"], row["external_key"])


def deterministic_source_lineage_id(*, source_kind: str, external_key: str) -> str:
    """Return the stable default identity for a native source lineage."""
    digest = content_hash(
        {
            "source_kind": normalize_text(source_kind),
            "external_key": normalize_text(external_key),
        },
        namespace="wiki-lineage:v1",
    ).removeprefix("sha256:")
    return f"lineage:v1:sha256:{digest}"


def record_card_revision(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    card_payload: Any,
    input_payloads: Mapping[str, Any],
    producer_kind: ProducerKind = "ingest",
    produced_by_stage_version_id: str | None = None,
    card_revision_id: str | None = None,
    card_unordered_collection_paths: Sequence[JsonPath] = (),
    card_exact_quote_paths: Sequence[JsonPath] = (),
    input_unordered_collection_paths: Mapping[str, Sequence[JsonPath]] | None = None,
    input_exact_quote_paths: Mapping[str, Sequence[JsonPath]] | None = None,
) -> CardRevision:
    """Record an immutable card revision and independently advance each input kind."""
    _validate_producer(producer_kind, produced_by_stage_version_id)
    card_json = canonical_json(
        card_payload,
        unordered_collection_paths=card_unordered_collection_paths,
        exact_quote_paths=card_exact_quote_paths,
    )
    card_hash = content_hash(
        card_payload,
        namespace="wiki-card-revision-payload:v1",
        unordered_collection_paths=card_unordered_collection_paths,
        exact_quote_paths=card_exact_quote_paths,
    )
    unordered_by_kind = input_unordered_collection_paths or {}
    quotes_by_kind = input_exact_quote_paths or {}
    prepared_inputs = {
        input_kind: (
            canonical_json(
                payload,
                unordered_collection_paths=unordered_by_kind.get(input_kind, ()),
                exact_quote_paths=quotes_by_kind.get(input_kind, ()),
            ),
            content_hash(
                payload,
                namespace=f"wiki-input:v1:{input_kind}",
                unordered_collection_paths=unordered_by_kind.get(input_kind, ()),
                exact_quote_paths=quotes_by_kind.get(input_kind, ()),
            ),
        )
        for input_kind, payload in input_payloads.items()
    }

    with _immediate_transaction(connection):
        _require_lineage(connection, source_lineage_id)
        revision_row = connection.execute(
            """
            SELECT card_revision_id
            FROM card_revisions
            WHERE source_lineage_id = ? AND card_content_hash = ?
            """,
            (source_lineage_id, card_hash),
        ).fetchone()
        if revision_row is None:
            resolved_card_revision_id = card_revision_id or _default_card_revision_id(
                source_lineage_id=source_lineage_id,
                card_content_hash=card_hash,
            )
            connection.execute(
                """
                INSERT INTO card_revisions (
                    card_revision_id,
                    source_lineage_id,
                    card_content_hash,
                    canonical_payload_json,
                    producer_kind,
                    produced_by_stage_version_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_card_revision_id,
                    source_lineage_id,
                    card_hash,
                    card_json,
                    producer_kind,
                    produced_by_stage_version_id,
                    _utc_now(),
                ),
            )
        else:
            resolved_card_revision_id = revision_row["card_revision_id"]
            if card_revision_id is not None and card_revision_id != resolved_card_revision_id:
                raise StateConflictError(
                    "Canonical card payload already exists with a different card revision id"
                )

        card_head = connection.execute(
            """
            SELECT current_card_revision_id, card_head_generation
            FROM source_lineage_heads
            WHERE source_lineage_id = ?
            """,
            (source_lineage_id,),
        ).fetchone()
        card_changed = (
            card_head is None
            or card_head["current_card_revision_id"] != resolved_card_revision_id
        )
        if card_head is None:
            card_head_generation = 1
            connection.execute(
                """
                INSERT INTO source_lineage_heads (
                    source_lineage_id,
                    current_card_revision_id,
                    card_head_generation,
                    updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    source_lineage_id,
                    resolved_card_revision_id,
                    card_head_generation,
                    _utc_now(),
                ),
            )
        elif card_changed:
            card_head_generation = int(card_head["card_head_generation"]) + 1
            cursor = connection.execute(
                """
                UPDATE source_lineage_heads
                SET current_card_revision_id = ?, card_head_generation = ?, updated_at = ?
                WHERE source_lineage_id = ?
                  AND current_card_revision_id = ?
                  AND card_head_generation = ?
                """,
                (
                    resolved_card_revision_id,
                    card_head_generation,
                    _utc_now(),
                    source_lineage_id,
                    card_head["current_card_revision_id"],
                    card_head["card_head_generation"],
                ),
            )
            _require_cas_update(cursor, "card revision head")
        else:
            card_head_generation = int(card_head["card_head_generation"])

        input_heads: list[InputHead] = []
        for input_kind in sorted(prepared_inputs):
            input_json, input_hash = prepared_inputs[input_kind]
            input_head = _advance_input_head_in_transaction(
                connection,
                source_lineage_id=source_lineage_id,
                input_kind=input_kind,
                input_hash=input_hash,
                canonical_payload_json=input_json,
                observed_card_revision_id=resolved_card_revision_id,
            )
            existing_binding = connection.execute(
                """
                SELECT input_hash
                FROM card_revision_input_bindings
                WHERE card_revision_id = ? AND input_kind = ?
                """,
                (resolved_card_revision_id, input_kind),
            ).fetchone()
            if existing_binding is None:
                connection.execute(
                    """
                    INSERT INTO card_revision_input_bindings (
                        card_revision_id,
                        input_kind,
                        source_lineage_id,
                        input_version_id,
                        input_generation,
                        input_hash
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_card_revision_id,
                        input_kind,
                        source_lineage_id,
                        input_head.input_version_id,
                        input_head.generation,
                        input_head.input_hash,
                    ),
                )
            elif existing_binding["input_hash"] != input_head.input_hash:
                raise StateConflictError(
                    f"Card revision {resolved_card_revision_id} already binds {input_kind} "
                    "to a different input hash"
                )
            input_heads.append(input_head)

        return CardRevision(
            card_revision_id=resolved_card_revision_id,
            source_lineage_id=source_lineage_id,
            content_hash=card_hash,
            card_head_generation=card_head_generation,
            input_heads=tuple(input_heads),
            changed=card_changed,
        )


def advance_input_head(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    input_kind: str,
    payload: Any,
    observed_card_revision_id: str | None = None,
    unordered_collection_paths: Sequence[JsonPath] = (),
    exact_quote_paths: Sequence[JsonPath] = (),
) -> InputHead:
    """Advance only one named input head when its canonical hash changes."""
    payload_json = canonical_json(
        payload,
        unordered_collection_paths=unordered_collection_paths,
        exact_quote_paths=exact_quote_paths,
    )
    input_hash = content_hash(
        payload,
        namespace=f"wiki-input:v1:{input_kind}",
        unordered_collection_paths=unordered_collection_paths,
        exact_quote_paths=exact_quote_paths,
    )
    with _immediate_transaction(connection):
        _require_lineage(connection, source_lineage_id)
        if observed_card_revision_id is not None:
            _require_card_revision(connection, observed_card_revision_id, source_lineage_id)
        return _advance_input_head_in_transaction(
            connection,
            source_lineage_id=source_lineage_id,
            input_kind=input_kind,
            input_hash=input_hash,
            canonical_payload_json=payload_json,
            observed_card_revision_id=observed_card_revision_id,
        )


def get_input_head(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    input_kind: str,
) -> InputHead | None:
    row = connection.execute(
        """
        SELECT
            source_lineage_id,
            input_kind,
            current_input_version_id,
            current_input_generation,
            current_input_hash
        FROM lineage_input_heads
        WHERE source_lineage_id = ? AND input_kind = ?
        """,
        (source_lineage_id, input_kind),
    ).fetchone()
    return _input_head_from_row(row) if row is not None else None


def activate_processor_contract(
    connection: sqlite3.Connection,
    *,
    stage_kind: str,
    contract: ProcessorContractSpec | Mapping[str, Any],
) -> ProcessorActivation:
    """Register and activate a method contract while preserving activation history."""
    payload = contract.as_payload() if isinstance(contract, ProcessorContractSpec) else dict(contract)
    contract_json = canonical_json(payload)
    contract_hash = content_hash(payload, namespace=f"wiki-v2-contract:{stage_kind}")
    with _immediate_transaction(connection):
        version_row = connection.execute(
            """
            SELECT processor_contract_version_id
            FROM processor_contract_versions
            WHERE stage_kind = ? AND contract_hash = ?
            """,
            (stage_kind, contract_hash),
        ).fetchone()
        if version_row is None:
            contract_version_id = _new_id()
            connection.execute(
                """
                INSERT INTO processor_contract_versions (
                    processor_contract_version_id,
                    stage_kind,
                    contract_hash,
                    contract_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (contract_version_id, stage_kind, contract_hash, contract_json, _utc_now()),
            )
        else:
            contract_version_id = version_row["processor_contract_version_id"]

        current = _active_contract_row(connection, stage_kind)
        if current is not None and current["processor_contract_version_id"] == contract_version_id:
            return ProcessorActivation(
                stage_kind=stage_kind,
                activation_generation=int(current["current_activation_generation"]),
                processor_contract_version_id=contract_version_id,
                contract_hash=contract_hash,
                changed=False,
            )

        activation_generation = (
            1 if current is None else int(current["current_activation_generation"]) + 1
        )
        now = _utc_now()
        connection.execute(
            """
            INSERT INTO processor_contract_activations (
                stage_kind,
                activation_generation,
                processor_contract_version_id,
                activated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (stage_kind, activation_generation, contract_version_id, now),
        )
        if current is None:
            connection.execute(
                """
                INSERT INTO active_processor_contract_heads (
                    stage_kind, current_activation_generation, updated_at
                ) VALUES (?, ?, ?)
                """,
                (stage_kind, activation_generation, now),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE active_processor_contract_heads
                SET current_activation_generation = ?, updated_at = ?
                WHERE stage_kind = ? AND current_activation_generation = ?
                """,
                (
                    activation_generation,
                    now,
                    stage_kind,
                    current["current_activation_generation"],
                ),
            )
            _require_cas_update(cursor, "processor contract head")
        return ProcessorActivation(
            stage_kind=stage_kind,
            activation_generation=activation_generation,
            processor_contract_version_id=contract_version_id,
            contract_hash=contract_hash,
            changed=True,
        )


def get_active_processor_contract(
    connection: sqlite3.Connection,
    *,
    stage_kind: str,
) -> ProcessorActivation | None:
    row = _active_contract_row(connection, stage_kind)
    if row is None:
        return None
    return ProcessorActivation(
        stage_kind=stage_kind,
        activation_generation=int(row["current_activation_generation"]),
        processor_contract_version_id=row["processor_contract_version_id"],
        contract_hash=row["contract_hash"],
        changed=False,
    )


def publish_dependency(
    connection: sqlite3.Connection,
    *,
    dependency_kind: DependencyKind | str,
    dependency_scope_key: str,
    payload: Any,
    expected_version_id: str | None,
    producer_kind: ProducerKind = "ingest",
    produced_by_stage_version_id: str | None = None,
    unordered_collection_paths: Sequence[JsonPath] = (),
) -> DependencyHead:
    """CAS-publish one universal data dependency head."""
    dependency_kind = _coerce_dependency_kind(dependency_kind)
    _validate_producer(producer_kind, produced_by_stage_version_id)
    payload_json = canonical_json(payload, unordered_collection_paths=unordered_collection_paths)
    dependency_hash = content_hash(
        payload,
        namespace=f"wiki-v2-dependency:{dependency_kind}:{dependency_scope_key}",
        unordered_collection_paths=unordered_collection_paths,
    )
    with _immediate_transaction(connection):
        current = _dependency_head_row(connection, dependency_kind, dependency_scope_key)
        current_version_id = None if current is None else current["current_dependency_version_id"]
        if current_version_id != expected_version_id:
            raise StaleHeadError(
                f"Dependency {dependency_kind}:{dependency_scope_key} expected "
                f"{expected_version_id!r}, found {current_version_id!r}"
            )
        if current is not None and current["current_hash"] == dependency_hash:
            return _dependency_head_from_row(current, changed=False)

        generation = 1 if current is None else int(current["current_generation"]) + 1
        dependency_version_id = _new_id()
        now = _utc_now()
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dependency_version_id,
                dependency_kind,
                dependency_scope_key,
                generation,
                dependency_hash,
                payload_json,
                producer_kind,
                produced_by_stage_version_id,
                now,
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
                    dependency_kind,
                    dependency_scope_key,
                    dependency_version_id,
                    generation,
                    dependency_hash,
                    now,
                ),
            )
        else:
            cursor = connection.execute(
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
                    now,
                    dependency_kind,
                    dependency_scope_key,
                    expected_version_id,
                ),
            )
            _require_cas_update(cursor, "dependency head")
        return DependencyHead(
            dependency_kind=dependency_kind,
            dependency_scope_key=dependency_scope_key,
            dependency_version_id=dependency_version_id,
            generation=generation,
            dependency_hash=dependency_hash,
            changed=True,
        )


def get_dependency_head(
    connection: sqlite3.Connection,
    *,
    dependency_kind: DependencyKind | str,
    dependency_scope_key: str,
) -> DependencyHead | None:
    dependency_kind = _coerce_dependency_kind(dependency_kind)
    row = _dependency_head_row(connection, dependency_kind, dependency_scope_key)
    return _dependency_head_from_row(row, changed=False) if row is not None else None


def schedule_stage(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    stage_kind: str,
    input_kinds: Sequence[str],
    dependencies: Sequence[DependencyKey] = (),
) -> StageVersion:
    """Controller-only API: snapshot all data bindings and advance a stage head."""
    normalized_input_kinds = tuple(sorted(set(input_kinds)))
    if len(normalized_input_kinds) != len(input_kinds):
        raise StateConflictError("Stage input kinds must be unique")
    normalized_dependencies = tuple(sorted(set(dependencies)))
    if len(normalized_dependencies) != len(dependencies):
        raise StateConflictError("Stage dependency keys must be unique")

    with _immediate_transaction(connection):
        _require_lineage(connection, source_lineage_id)
        contract = _active_contract_row(connection, stage_kind)
        if contract is None:
            raise StateNotFoundError(f"No active processor contract for stage {stage_kind!r}")

        input_bindings: list[StageInputBinding] = []
        for input_kind in normalized_input_kinds:
            head = get_input_head(
                connection,
                source_lineage_id=source_lineage_id,
                input_kind=input_kind,
            )
            if head is None:
                raise StateNotFoundError(
                    f"No input head for {source_lineage_id}:{input_kind}"
                )
            input_bindings.append(
                StageInputBinding(
                    input_kind=head.input_kind,
                    input_version_id=head.input_version_id,
                    generation=head.generation,
                    input_hash=head.input_hash,
                )
            )

        dependency_bindings: list[StageDependencyBinding] = []
        for dependency in normalized_dependencies:
            head = get_dependency_head(
                connection,
                dependency_kind=dependency.dependency_kind,
                dependency_scope_key=dependency.dependency_scope_key,
            )
            if head is None:
                raise StateNotFoundError(
                    "No dependency head for "
                    f"{dependency.dependency_kind}:{dependency.dependency_scope_key}"
                )
            dependency_bindings.append(
                StageDependencyBinding(
                    dependency_kind=head.dependency_kind,
                    dependency_scope_key=head.dependency_scope_key,
                    dependency_version_id=head.dependency_version_id,
                    generation=head.generation,
                    dependency_hash=head.dependency_hash,
                )
            )

        binding_payload = {
            "inputs": [
                {
                    "input_kind": binding.input_kind,
                    "input_version_id": binding.input_version_id,
                    "input_generation": binding.generation,
                    "input_hash": binding.input_hash,
                }
                for binding in input_bindings
            ],
            "dependencies": [
                {
                    "dependency_kind": binding.dependency_kind,
                    "dependency_scope_key": binding.dependency_scope_key,
                    "dependency_version_id": binding.dependency_version_id,
                    "dependency_generation": binding.generation,
                    "dependency_hash": binding.dependency_hash,
                }
                for binding in dependency_bindings
            ],
        }
        stage_inputs_hash = content_hash(
            binding_payload,
            namespace=f"wiki-v2-stage-inputs:{source_lineage_id}:{stage_kind}",
        )
        current_head = connection.execute(
            """
            SELECT
                head.current_stage_version_id,
                head.current_stage_generation,
                version.stage_inputs_hash,
                version.processor_contract_activation_generation
            FROM lineage_stage_heads AS head
            JOIN lineage_stage_versions AS version
              ON version.stage_version_id = head.current_stage_version_id
            WHERE head.source_lineage_id = ? AND head.stage_kind = ?
            """,
            (source_lineage_id, stage_kind),
        ).fetchone()
        if (
            current_head is not None
            and current_head["stage_inputs_hash"] == stage_inputs_hash
            and int(current_head["processor_contract_activation_generation"])
            == int(contract["current_activation_generation"])
        ):
            return _load_stage_version(
                connection,
                current_head["current_stage_version_id"],
                changed=False,
            )

        generation = (
            1 if current_head is None else int(current_head["current_stage_generation"]) + 1
        )
        stage_version_id = _new_id()
        now = _utc_now()
        connection.execute(
            """
            INSERT INTO lineage_stage_versions (
                stage_version_id,
                source_lineage_id,
                stage_kind,
                stage_generation,
                stage_inputs_hash,
                processor_contract_activation_generation,
                processor_contract_version_id,
                processor_contract_hash,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stage_version_id,
                source_lineage_id,
                stage_kind,
                generation,
                stage_inputs_hash,
                contract["current_activation_generation"],
                contract["processor_contract_version_id"],
                contract["contract_hash"],
                now,
            ),
        )
        connection.executemany(
            """
            INSERT INTO lineage_stage_input_bindings (
                stage_version_id,
                source_lineage_id,
                input_kind,
                input_version_id,
                input_generation,
                input_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    stage_version_id,
                    source_lineage_id,
                    binding.input_kind,
                    binding.input_version_id,
                    binding.generation,
                    binding.input_hash,
                )
                for binding in input_bindings
            ],
        )
        connection.executemany(
            """
            INSERT INTO stage_dependency_bindings (
                stage_version_id,
                dependency_kind,
                dependency_scope_key,
                dependency_version_id,
                dependency_generation,
                dependency_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    stage_version_id,
                    binding.dependency_kind,
                    binding.dependency_scope_key,
                    binding.dependency_version_id,
                    binding.generation,
                    binding.dependency_hash,
                )
                for binding in dependency_bindings
            ],
        )
        if current_head is None:
            connection.execute(
                """
                INSERT INTO lineage_stage_heads (
                    source_lineage_id,
                    stage_kind,
                    current_stage_version_id,
                    current_stage_generation,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (source_lineage_id, stage_kind, stage_version_id, generation, now),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE lineage_stage_heads
                SET
                    current_stage_version_id = ?,
                    current_stage_generation = ?,
                    updated_at = ?
                WHERE source_lineage_id = ?
                  AND stage_kind = ?
                  AND current_stage_version_id = ?
                  AND current_stage_generation = ?
                """,
                (
                    stage_version_id,
                    generation,
                    now,
                    source_lineage_id,
                    stage_kind,
                    current_head["current_stage_version_id"],
                    current_head["current_stage_generation"],
                ),
            )
            _require_cas_update(cursor, "stage head")
        return _load_stage_version(connection, stage_version_id, changed=True)


def get_stage_version(
    connection: sqlite3.Connection,
    *,
    stage_version_id: str,
) -> StageVersion:
    return _load_stage_version(connection, stage_version_id, changed=False)


def start_stage_run(
    connection: sqlite3.Connection,
    *,
    stage_version_id: str,
    idempotency_key: str,
    artifact_source_card_revision_id: str | None = None,
) -> StageRun:
    """Create a worker attempt without mutating any authoritative or stage head."""
    with _immediate_transaction(connection):
        version = connection.execute(
            """
            SELECT
                source_lineage_id,
                stage_kind,
                stage_generation,
                processor_contract_activation_generation,
                processor_contract_version_id
            FROM lineage_stage_versions
            WHERE stage_version_id = ?
            """,
            (stage_version_id,),
        ).fetchone()
        if version is None:
            raise StateNotFoundError(f"Unknown stage version {stage_version_id}")
        stage_run_id = _new_id()
        connection.execute(
            """
            INSERT INTO stage_runs (
                stage_run_id,
                stage_version_id,
                source_lineage_id,
                stage_kind,
                processor_contract_version_id,
                idempotency_key,
                status,
                observed_stage_generation,
                observed_contract_activation_generation,
                artifact_source_card_revision_id,
                started_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'started', ?, ?, ?, ?)
            """,
            (
                stage_run_id,
                stage_version_id,
                version["source_lineage_id"],
                version["stage_kind"],
                version["processor_contract_version_id"],
                idempotency_key,
                version["stage_generation"],
                version["processor_contract_activation_generation"],
                artifact_source_card_revision_id,
                _utc_now(),
            ),
        )
        return _load_stage_run(connection, stage_run_id)


def fail_stage_run(
    connection: sqlite3.Connection,
    *,
    stage_run_id: str,
    error_text: str,
) -> StageRun:
    """Preserve a failed attempt so the same idempotency key can be retried."""
    with _immediate_transaction(connection):
        run = _load_stage_run(connection, stage_run_id)
        if run.status != "started":
            return run
        connection.execute(
            """
            UPDATE stage_runs
            SET status = 'failed', error_text = ?, finished_at = ?
            WHERE stage_run_id = ? AND status = 'started'
            """,
            (error_text, _utc_now(), stage_run_id),
        )
        return _load_stage_run(connection, stage_run_id)


def commit_stage_run(
    connection: sqlite3.Connection,
    *,
    stage_run_id: str,
    outbox_events: Sequence[OutboxEventSpec] = (),
) -> StageRun:
    """Atomically CAS-check a stage snapshot, assign commit_seq, and emit outbox."""
    prepared_events = _prepare_outbox_events(outbox_events)
    with _immediate_transaction(connection):
        run = _load_stage_run(connection, stage_run_id)
        if run.status != "started":
            return run

        duplicate = connection.execute(
            """
            SELECT stage_run_id
            FROM stage_runs
            WHERE idempotency_key = ? AND status = 'committed' AND stage_run_id <> ?
            """,
            (run.idempotency_key, stage_run_id),
        ).fetchone()
        if duplicate is not None:
            connection.execute(
                """
                UPDATE stage_runs
                SET
                    status = 'no_op',
                    duplicate_of_stage_run_id = ?,
                    finished_at = ?
                WHERE stage_run_id = ? AND status = 'started'
                """,
                (duplicate["stage_run_id"], _utc_now(), stage_run_id),
            )
            return _load_stage_run(connection, stage_run_id)

        version = _load_stage_version(connection, run.stage_version_id, changed=False)
        if not _stage_snapshot_is_current(connection, version):
            connection.execute(
                """
                UPDATE stage_runs
                SET status = 'stale', finished_at = ?
                WHERE stage_run_id = ? AND status = 'started'
                """,
                (_utc_now(), stage_run_id),
            )
            return _load_stage_run(connection, stage_run_id)

        current_card = connection.execute(
            """
            SELECT current_card_revision_id
            FROM source_lineage_heads
            WHERE source_lineage_id = ?
            """,
            (version.source_lineage_id,),
        ).fetchone()
        applied_against_card_revision_id = (
            None if current_card is None else current_card["current_card_revision_id"]
        )
        commit_seq = int(
            connection.execute(
                "SELECT COALESCE(MAX(commit_seq), 0) + 1 AS next_seq FROM stage_runs"
            ).fetchone()["next_seq"]
        )
        now = _utc_now()
        cursor = connection.execute(
            """
            UPDATE stage_runs
            SET
                status = 'committed',
                applied_against_card_revision_id = ?,
                finished_at = ?,
                commit_seq = ?
            WHERE stage_run_id = ? AND status = 'started'
            """,
            (applied_against_card_revision_id, now, commit_seq, stage_run_id),
        )
        _require_cas_update(cursor, "stage run")

        for event, payload_json in prepared_events:
            existing = connection.execute(
                """
                SELECT
                    event_kind,
                    aggregate_kind,
                    aggregate_key,
                    payload_json
                FROM outbox_events
                WHERE event_key = ?
                """,
                (event.event_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["event_kind"] != event.event_kind
                    or existing["aggregate_kind"] != event.aggregate_kind
                    or existing["aggregate_key"] != event.aggregate_key
                    or existing["payload_json"] != payload_json
                ):
                    raise IdempotencyConflictError(
                        f"Outbox event key {event.event_key!r} has different content"
                    )
                continue
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
                    _new_id(),
                    event.event_key,
                    stage_run_id,
                    commit_seq,
                    event.event_kind,
                    event.aggregate_kind,
                    event.aggregate_key,
                    payload_json,
                    now,
                ),
            )
        return _load_stage_run(connection, stage_run_id)


def list_pending_outbox(
    connection: sqlite3.Connection,
    *,
    limit: int = 100,
) -> tuple[OutboxEvent, ...]:
    rows = connection.execute(
        """
        SELECT *
        FROM outbox_events
        WHERE processed_at IS NULL
        ORDER BY commit_seq, outbox_event_id
        LIMIT ?
        """,
        (max(limit, 0),),
    ).fetchall()
    return tuple(_outbox_event_from_row(row) for row in rows)


def mark_outbox_processed(
    connection: sqlite3.Connection,
    *,
    outbox_event_id: str,
) -> OutboxEvent:
    """Idempotently mark one event delivered under the single-dispatcher contract."""
    with _immediate_transaction(connection):
        row = connection.execute(
            "SELECT * FROM outbox_events WHERE outbox_event_id = ?",
            (outbox_event_id,),
        ).fetchone()
        if row is None:
            raise StateNotFoundError(f"Unknown outbox event {outbox_event_id}")
        if row["processed_at"] is None:
            connection.execute(
                """
                UPDATE outbox_events
                SET processed_at = ?
                WHERE outbox_event_id = ? AND processed_at IS NULL
                """,
                (_utc_now(), outbox_event_id),
            )
        updated = connection.execute(
            "SELECT * FROM outbox_events WHERE outbox_event_id = ?",
            (outbox_event_id,),
        ).fetchone()
        return _outbox_event_from_row(updated)


def _advance_input_head_in_transaction(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    input_kind: str,
    input_hash: str,
    canonical_payload_json: str,
    observed_card_revision_id: str | None,
) -> InputHead:
    current = connection.execute(
        """
        SELECT *
        FROM lineage_input_heads
        WHERE source_lineage_id = ? AND input_kind = ?
        """,
        (source_lineage_id, input_kind),
    ).fetchone()
    if current is not None and current["current_input_hash"] == input_hash:
        return _input_head_from_row(current, changed=False)

    generation = 1 if current is None else int(current["current_input_generation"]) + 1
    input_version_id = _new_id()
    now = _utc_now()
    connection.execute(
        """
        INSERT INTO lineage_input_versions (
            input_version_id,
            source_lineage_id,
            input_kind,
            input_generation,
            input_hash,
            canonical_payload_json,
            observed_card_revision_id,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            input_version_id,
            source_lineage_id,
            input_kind,
            generation,
            input_hash,
            canonical_payload_json,
            observed_card_revision_id,
            now,
        ),
    )
    if current is None:
        connection.execute(
            """
            INSERT INTO lineage_input_heads (
                source_lineage_id,
                input_kind,
                current_input_version_id,
                current_input_generation,
                current_input_hash,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_lineage_id,
                input_kind,
                input_version_id,
                generation,
                input_hash,
                now,
            ),
        )
    else:
        cursor = connection.execute(
            """
            UPDATE lineage_input_heads
            SET
                current_input_version_id = ?,
                current_input_generation = ?,
                current_input_hash = ?,
                updated_at = ?
            WHERE source_lineage_id = ?
              AND input_kind = ?
              AND current_input_version_id = ?
            """,
            (
                input_version_id,
                generation,
                input_hash,
                now,
                source_lineage_id,
                input_kind,
                current["current_input_version_id"],
            ),
        )
        _require_cas_update(cursor, "input head")
    return InputHead(
        source_lineage_id=source_lineage_id,
        input_kind=input_kind,
        input_version_id=input_version_id,
        generation=generation,
        input_hash=input_hash,
        changed=True,
    )


def _active_contract_row(
    connection: sqlite3.Connection,
    stage_kind: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT
            head.current_activation_generation,
            activation.processor_contract_version_id,
            version.contract_hash
        FROM active_processor_contract_heads AS head
        JOIN processor_contract_activations AS activation
          ON activation.stage_kind = head.stage_kind
         AND activation.activation_generation = head.current_activation_generation
        JOIN processor_contract_versions AS version
          ON version.processor_contract_version_id = activation.processor_contract_version_id
        WHERE head.stage_kind = ?
        """,
        (stage_kind,),
    ).fetchone()


def _dependency_head_row(
    connection: sqlite3.Connection,
    dependency_kind: DependencyKind,
    dependency_scope_key: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT *
        FROM dependency_heads
        WHERE dependency_kind = ? AND dependency_scope_key = ?
        """,
        (dependency_kind, dependency_scope_key),
    ).fetchone()


def _load_stage_version(
    connection: sqlite3.Connection,
    stage_version_id: str,
    *,
    changed: bool,
) -> StageVersion:
    row = connection.execute(
        "SELECT * FROM lineage_stage_versions WHERE stage_version_id = ?",
        (stage_version_id,),
    ).fetchone()
    if row is None:
        raise StateNotFoundError(f"Unknown stage version {stage_version_id}")
    input_rows = connection.execute(
        """
        SELECT *
        FROM lineage_stage_input_bindings
        WHERE stage_version_id = ?
        ORDER BY input_kind
        """,
        (stage_version_id,),
    ).fetchall()
    dependency_rows = connection.execute(
        """
        SELECT *
        FROM stage_dependency_bindings
        WHERE stage_version_id = ?
        ORDER BY dependency_kind, dependency_scope_key
        """,
        (stage_version_id,),
    ).fetchall()
    return StageVersion(
        stage_version_id=row["stage_version_id"],
        source_lineage_id=row["source_lineage_id"],
        stage_kind=row["stage_kind"],
        generation=int(row["stage_generation"]),
        stage_inputs_hash=row["stage_inputs_hash"],
        contract_activation_generation=int(row["processor_contract_activation_generation"]),
        processor_contract_version_id=row["processor_contract_version_id"],
        processor_contract_hash=row["processor_contract_hash"],
        input_bindings=tuple(
            StageInputBinding(
                input_kind=input_row["input_kind"],
                input_version_id=input_row["input_version_id"],
                generation=int(input_row["input_generation"]),
                input_hash=input_row["input_hash"],
            )
            for input_row in input_rows
        ),
        dependency_bindings=tuple(
            StageDependencyBinding(
                dependency_kind=DependencyKind(dependency_row["dependency_kind"]),
                dependency_scope_key=dependency_row["dependency_scope_key"],
                dependency_version_id=dependency_row["dependency_version_id"],
                generation=int(dependency_row["dependency_generation"]),
                dependency_hash=dependency_row["dependency_hash"],
            )
            for dependency_row in dependency_rows
        ),
        changed=changed,
    )


def _load_stage_run(connection: sqlite3.Connection, stage_run_id: str) -> StageRun:
    row = connection.execute(
        "SELECT * FROM stage_runs WHERE stage_run_id = ?",
        (stage_run_id,),
    ).fetchone()
    if row is None:
        raise StateNotFoundError(f"Unknown stage run {stage_run_id}")
    return StageRun(
        stage_run_id=row["stage_run_id"],
        stage_version_id=row["stage_version_id"],
        source_lineage_id=row["source_lineage_id"],
        stage_kind=row["stage_kind"],
        processor_contract_version_id=row["processor_contract_version_id"],
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        observed_stage_generation=int(row["observed_stage_generation"]),
        observed_contract_activation_generation=int(
            row["observed_contract_activation_generation"]
        ),
        commit_seq=None if row["commit_seq"] is None else int(row["commit_seq"]),
        duplicate_of_stage_run_id=row["duplicate_of_stage_run_id"],
        artifact_source_card_revision_id=row["artifact_source_card_revision_id"],
        applied_against_card_revision_id=row["applied_against_card_revision_id"],
        error_text=row["error_text"],
    )


def _stage_snapshot_is_current(
    connection: sqlite3.Connection,
    version: StageVersion,
) -> bool:
    stage_head = connection.execute(
        """
        SELECT current_stage_version_id, current_stage_generation
        FROM lineage_stage_heads
        WHERE source_lineage_id = ? AND stage_kind = ?
        """,
        (version.source_lineage_id, version.stage_kind),
    ).fetchone()
    if (
        stage_head is None
        or stage_head["current_stage_version_id"] != version.stage_version_id
        or int(stage_head["current_stage_generation"]) != version.generation
    ):
        return False

    contract = _active_contract_row(connection, version.stage_kind)
    if (
        contract is None
        or int(contract["current_activation_generation"])
        != version.contract_activation_generation
        or contract["processor_contract_version_id"]
        != version.processor_contract_version_id
        or contract["contract_hash"] != version.processor_contract_hash
    ):
        return False

    for binding in version.input_bindings:
        head = get_input_head(
            connection,
            source_lineage_id=version.source_lineage_id,
            input_kind=binding.input_kind,
        )
        if (
            head is None
            or head.input_version_id != binding.input_version_id
            or head.generation != binding.generation
            or head.input_hash != binding.input_hash
        ):
            return False

    for binding in version.dependency_bindings:
        head = get_dependency_head(
            connection,
            dependency_kind=binding.dependency_kind,
            dependency_scope_key=binding.dependency_scope_key,
        )
        if (
            head is None
            or head.dependency_version_id != binding.dependency_version_id
            or head.generation != binding.generation
            or head.dependency_hash != binding.dependency_hash
        ):
            return False
    return True


def _prepare_outbox_events(
    events: Sequence[OutboxEventSpec],
) -> tuple[tuple[OutboxEventSpec, str], ...]:
    prepared: dict[
        str,
        tuple[OutboxEventSpec, str, tuple[str, str, str, str, str]],
    ] = {}
    for event in events:
        payload_json = canonical_json(event.payload)
        canonical_event = OutboxEventSpec(
            event_key=normalize_text(event.event_key),
            event_kind=normalize_text(event.event_kind),
            aggregate_kind=normalize_text(event.aggregate_kind),
            aggregate_key=normalize_text(event.aggregate_key),
            payload=event.payload,
        )
        signature = (
            canonical_event.event_key,
            canonical_event.event_kind,
            canonical_event.aggregate_kind,
            canonical_event.aggregate_key,
            payload_json,
        )
        previous = prepared.get(canonical_event.event_key)
        if previous is not None and previous[2] != signature:
            raise IdempotencyConflictError(
                f"Outbox event key {canonical_event.event_key!r} appears with different content"
            )
        prepared[canonical_event.event_key] = (canonical_event, payload_json, signature)
    return tuple(
        (prepared[key][0], prepared[key][1])
        for key in sorted(prepared)
    )


def _input_head_from_row(row: sqlite3.Row, *, changed: bool = False) -> InputHead:
    return InputHead(
        source_lineage_id=row["source_lineage_id"],
        input_kind=row["input_kind"],
        input_version_id=row["current_input_version_id"],
        generation=int(row["current_input_generation"]),
        input_hash=row["current_input_hash"],
        changed=changed,
    )


def _dependency_head_from_row(row: sqlite3.Row, *, changed: bool) -> DependencyHead:
    return DependencyHead(
        dependency_kind=DependencyKind(row["dependency_kind"]),
        dependency_scope_key=row["dependency_scope_key"],
        dependency_version_id=row["current_dependency_version_id"],
        generation=int(row["current_generation"]),
        dependency_hash=row["current_hash"],
        changed=changed,
    )


def _outbox_event_from_row(row: sqlite3.Row) -> OutboxEvent:
    return OutboxEvent(
        outbox_event_id=row["outbox_event_id"],
        event_key=row["event_key"],
        stage_run_id=row["stage_run_id"],
        commit_seq=int(row["commit_seq"]),
        event_kind=row["event_kind"],
        aggregate_kind=row["aggregate_kind"],
        aggregate_key=row["aggregate_key"],
        payload_json=row["payload_json"],
        processed_at=row["processed_at"],
    )


def _require_lineage(connection: sqlite3.Connection, source_lineage_id: str) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM source_lineages WHERE source_lineage_id = ?",
            (source_lineage_id,),
        ).fetchone()
        is None
    ):
        raise StateNotFoundError(f"Unknown source lineage {source_lineage_id}")


def _require_card_revision(
    connection: sqlite3.Connection,
    card_revision_id: str,
    source_lineage_id: str,
) -> None:
    if (
        connection.execute(
            """
            SELECT 1
            FROM card_revisions
            WHERE card_revision_id = ? AND source_lineage_id = ?
            """,
            (card_revision_id, source_lineage_id),
        ).fetchone()
        is None
    ):
        raise StateNotFoundError(
            f"Card revision {card_revision_id} does not belong to {source_lineage_id}"
        )


def _validate_producer(
    producer_kind: ProducerKind,
    produced_by_stage_version_id: str | None,
) -> None:
    if producer_kind == "stage" and produced_by_stage_version_id is None:
        raise StateConflictError("stage outputs require produced_by_stage_version_id")
    if producer_kind != "stage" and produced_by_stage_version_id is not None:
        raise StateConflictError(
            "ingest/manual/registry outputs cannot name produced_by_stage_version_id"
        )


def _coerce_dependency_kind(value: DependencyKind | str) -> DependencyKind:
    try:
        return DependencyKind(value)
    except ValueError as exc:
        raise StateConflictError(
            f"Unsupported data dependency kind {value!r}; schema/API extension is required"
        ) from exc


def _require_cas_update(cursor: sqlite3.Cursor, head_name: str) -> None:
    if cursor.rowcount != 1:
        raise StaleHeadError(f"CAS failed for {head_name}")


def _default_card_revision_id(
    *,
    source_lineage_id: str,
    card_content_hash: str,
) -> str:
    """Scope low-level payload hashes so generic callers cannot collide across lineages."""
    digest = content_hash(
        {
            "source_lineage_id": source_lineage_id,
            "card_content_hash": card_content_hash,
        },
        namespace="wiki-card-revision-identity:v1",
    ).removeprefix("sha256:")
    return f"cardrev:v1:sha256:{digest}"


@contextmanager
def _immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _new_id() -> str:
    return str(uuid4())


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
