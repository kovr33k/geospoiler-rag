"""CAS-protected eligibility evaluation over ready occurrence state."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from geospoiler.wiki.cards import ELIGIBILITY_INPUT_KIND, WIKI_RELEVANT_QUALITY_FLAGS
from geospoiler.wiki.hashing import canonical_json, content_hash
from geospoiler.wiki.schema import ELIGIBILITY_EVALUATION_STAGE_KIND
from geospoiler.wiki.state import (
    DependencyKind,
    ProcessorContractSpec,
    StaleHeadError,
    StateConflictError,
    StateNotFoundError,
    _immediate_transaction,
    _new_id,
    _utc_now,
    activate_processor_contract,
    get_active_processor_contract,
)

EligibilityStatus = Literal["published", "no_op", "stale"]

DEFAULT_ELIGIBILITY_CONTRACT = ProcessorContractSpec(
    algorithm_version="deterministic-eligibility-evaluation-v1",
    schema_version="eligibility-evaluation:v1",
    canonicalizer_version="wiki-canonical-json-nfc-v1",
    policy_version="wiki-eligibility-policy-v1",
)


@dataclass(frozen=True)
class PreparedEligibilityEvaluation:
    source_lineage_id: str
    evaluated_card_revision_id: str
    eligibility_input_version_id: str
    eligibility_input_generation: int
    eligibility_inputs_hash: str
    stage_kind: str
    processor_contract_activation_generation: int
    processor_contract_version_id: str
    processor_contract_hash: str
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class EligibilityEvaluation:
    eligibility_evaluation_id: str | None
    source_lineage_id: str
    evaluated_card_revision_id: str
    generation: int
    eligibility_input_version_id: str
    eligibility_input_generation: int
    eligibility_inputs_hash: str
    stage_kind: str
    processor_contract_activation_generation: int
    processor_contract_version_id: str
    processor_contract_hash: str
    eligible: bool
    reasons: tuple[str, ...]
    status: EligibilityStatus
    changed: bool
    dependency_generation: int

    @property
    def card_revision_id(self) -> str:
        """Compatibility alias for the exact evaluated card revision."""
        return self.evaluated_card_revision_id


def prepare_eligibility_evaluation(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    card_revision_id: str | None = None,
    contract: ProcessorContractSpec = DEFAULT_ELIGIBILITY_CONTRACT,
) -> PreparedEligibilityEvaluation:
    """Capture the exact card/input/processor snapshot outside final apply."""
    requested_activation = activate_processor_contract(
        connection,
        stage_kind=ELIGIBILITY_EVALUATION_STAGE_KIND,
        contract=contract,
    )
    current_card_revision_id = _current_card_revision_id(
        connection,
        source_lineage_id,
    )
    evaluated_card_revision_id = card_revision_id or current_card_revision_id
    if evaluated_card_revision_id != current_card_revision_id:
        raise StaleHeadError(
            "Eligibility can only be prepared for the current card revision"
        )

    binding = connection.execute(
        """
        SELECT
            binding.input_hash AS card_input_hash,
            head.current_input_version_id,
            head.current_input_generation,
            head.current_input_hash,
            version.canonical_payload_json
        FROM card_revision_input_bindings AS binding
        JOIN lineage_input_heads AS head
          ON head.source_lineage_id = binding.source_lineage_id
         AND head.input_kind = binding.input_kind
        JOIN lineage_input_versions AS version
          ON version.input_version_id = head.current_input_version_id
         AND version.source_lineage_id = head.source_lineage_id
         AND version.input_kind = head.input_kind
         AND version.input_generation = head.current_input_generation
         AND version.input_hash = head.current_input_hash
        WHERE binding.card_revision_id = ?
          AND binding.source_lineage_id = ?
          AND binding.input_kind = ?
        """,
        (
            evaluated_card_revision_id,
            source_lineage_id,
            ELIGIBILITY_INPUT_KIND,
        ),
    ).fetchone()
    if binding is None:
        raise StateNotFoundError(
            f"Card {evaluated_card_revision_id} has no eligibility_inputs binding"
        )
    if binding["card_input_hash"] != binding["current_input_hash"]:
        raise StaleHeadError(
            "Current card revision does not bind the current eligibility input"
        )

    active_contract = get_active_processor_contract(
        connection,
        stage_kind=ELIGIBILITY_EVALUATION_STAGE_KIND,
    )
    if (
        active_contract is None
        or active_contract.activation_generation
        != requested_activation.activation_generation
        or active_contract.processor_contract_version_id
        != requested_activation.processor_contract_version_id
        or active_contract.contract_hash != requested_activation.contract_hash
    ):
        raise StaleHeadError("Eligibility processor contract changed during prepare")

    eligible, reasons = _evaluate_payload(
        json.loads(binding["canonical_payload_json"])
    )
    return PreparedEligibilityEvaluation(
        source_lineage_id=source_lineage_id,
        evaluated_card_revision_id=evaluated_card_revision_id,
        eligibility_input_version_id=binding["current_input_version_id"],
        eligibility_input_generation=int(binding["current_input_generation"]),
        eligibility_inputs_hash=binding["current_input_hash"],
        stage_kind=ELIGIBILITY_EVALUATION_STAGE_KIND,
        processor_contract_activation_generation=active_contract.activation_generation,
        processor_contract_version_id=active_contract.processor_contract_version_id,
        processor_contract_hash=active_contract.contract_hash,
        eligible=eligible,
        reasons=reasons,
    )


def apply_prepared_eligibility(
    connection: sqlite3.Connection,
    prepared: PreparedEligibilityEvaluation,
) -> EligibilityEvaluation:
    """CAS-publish a prepared evaluation or return stale without moving state."""
    with _immediate_transaction(connection):
        current = _current_evaluation_row(
            connection,
            prepared.source_lineage_id,
        )
        if not _prepared_snapshot_is_current(connection, prepared):
            return _stale_result(connection, prepared, current)

        if current is not None and _current_matches_prepared(current, prepared):
            return _evaluation_from_row(
                connection,
                current,
                status="no_op",
                changed=False,
            )

        generation = (
            1 if current is None else int(current["current_eligibility_generation"]) + 1
        )
        evaluation_payload = {
            "source_lineage_id": prepared.source_lineage_id,
            "evaluated_card_revision_id": prepared.evaluated_card_revision_id,
            "eligibility_generation": generation,
            "eligibility_input_version_id": prepared.eligibility_input_version_id,
            "eligibility_input_generation": prepared.eligibility_input_generation,
            "eligibility_inputs_hash": prepared.eligibility_inputs_hash,
            "stage_kind": prepared.stage_kind,
            "processor_contract_activation_generation": (
                prepared.processor_contract_activation_generation
            ),
            "processor_contract_version_id": prepared.processor_contract_version_id,
            "processor_contract_hash": prepared.processor_contract_hash,
            "eligible": prepared.eligible,
            "reasons": list(prepared.reasons),
        }
        evaluation_hash = content_hash(
            evaluation_payload,
            namespace="wiki-eligibility-evaluation:v2",
        )
        evaluation_id = (
            "eligibility:v2:sha256:"
            f"{evaluation_hash.removeprefix('sha256:')}"
        )
        reasons_json = canonical_json(list(prepared.reasons))
        now = _utc_now()
        connection.execute(
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
            ) VALUES (?, ?, ?, ?, 'eligibility_inputs', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation_id,
                prepared.source_lineage_id,
                prepared.evaluated_card_revision_id,
                generation,
                prepared.eligibility_input_version_id,
                prepared.eligibility_input_generation,
                prepared.eligibility_inputs_hash,
                prepared.stage_kind,
                prepared.processor_contract_activation_generation,
                prepared.processor_contract_version_id,
                prepared.processor_contract_hash,
                int(prepared.eligible),
                reasons_json,
                evaluation_hash,
                now,
            ),
        )
        head_values = (
            evaluation_id,
            generation,
            prepared.evaluated_card_revision_id,
            prepared.eligibility_input_version_id,
            prepared.eligibility_input_generation,
            prepared.eligibility_inputs_hash,
            prepared.stage_kind,
            prepared.processor_contract_activation_generation,
            prepared.processor_contract_version_id,
            prepared.processor_contract_hash,
            int(prepared.eligible),
            now,
        )
        if current is None:
            connection.execute(
                """
                INSERT INTO eligibility_heads (
                    source_lineage_id,
                    current_eligibility_evaluation_id,
                    current_eligibility_generation,
                    evaluated_card_revision_id,
                    current_eligibility_input_version_id,
                    current_eligibility_input_generation,
                    current_eligibility_inputs_hash,
                    stage_kind,
                    current_processor_contract_activation_generation,
                    current_processor_contract_version_id,
                    current_processor_contract_hash,
                    current_eligible,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (prepared.source_lineage_id, *head_values),
            )
        else:
            updated = connection.execute(
                """
                UPDATE eligibility_heads
                SET
                    current_eligibility_evaluation_id = ?,
                    current_eligibility_generation = ?,
                    evaluated_card_revision_id = ?,
                    current_eligibility_input_version_id = ?,
                    current_eligibility_input_generation = ?,
                    current_eligibility_inputs_hash = ?,
                    stage_kind = ?,
                    current_processor_contract_activation_generation = ?,
                    current_processor_contract_version_id = ?,
                    current_processor_contract_hash = ?,
                    current_eligible = ?,
                    updated_at = ?
                WHERE source_lineage_id = ?
                  AND current_eligibility_evaluation_id = ?
                """,
                (
                    *head_values,
                    prepared.source_lineage_id,
                    current["current_eligibility_evaluation_id"],
                ),
            )
            if updated.rowcount != 1:
                raise StateConflictError("Eligibility head CAS failed")

        dependency_generation = _publish_eligibility_dependency(
            connection,
            evaluation_id=evaluation_id,
            evaluation_generation=generation,
            prepared=prepared,
            created_at=now,
        )
        row = _current_evaluation_row(connection, prepared.source_lineage_id)
        if row is None:
            raise StateConflictError("Published eligibility head is missing")
        return _evaluation_from_row(
            connection,
            row,
            status="published",
            changed=True,
            dependency_generation=dependency_generation,
        )


def evaluate_eligibility(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    card_revision_id: str | None = None,
    contract: ProcessorContractSpec = DEFAULT_ELIGIBILITY_CONTRACT,
) -> EligibilityEvaluation:
    """Prepare and CAS-apply current eligibility under a processor contract."""
    prepared = prepare_eligibility_evaluation(
        connection,
        source_lineage_id=source_lineage_id,
        card_revision_id=card_revision_id,
        contract=contract,
    )
    return apply_prepared_eligibility(connection, prepared)


def get_current_eligibility(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
) -> EligibilityEvaluation | None:
    row = _current_evaluation_row(connection, source_lineage_id)
    if row is None:
        return None
    return _evaluation_from_row(
        connection,
        row,
        status="no_op",
        changed=False,
    )


def _prepared_snapshot_is_current(
    connection: sqlite3.Connection,
    prepared: PreparedEligibilityEvaluation,
) -> bool:
    row = connection.execute(
        """
        SELECT
            card_head.current_card_revision_id,
            input_head.current_input_version_id,
            input_head.current_input_generation,
            input_head.current_input_hash,
            binding.input_hash AS card_input_hash,
            contract_head.current_activation_generation,
            activation.processor_contract_version_id,
            contract.contract_hash
        FROM source_lineage_heads AS card_head
        JOIN lineage_input_heads AS input_head
          ON input_head.source_lineage_id = card_head.source_lineage_id
         AND input_head.input_kind = 'eligibility_inputs'
        JOIN card_revision_input_bindings AS binding
          ON binding.card_revision_id = card_head.current_card_revision_id
         AND binding.source_lineage_id = card_head.source_lineage_id
         AND binding.input_kind = input_head.input_kind
        JOIN active_processor_contract_heads AS contract_head
          ON contract_head.stage_kind = ?
        JOIN processor_contract_activations AS activation
          ON activation.stage_kind = contract_head.stage_kind
         AND activation.activation_generation =
             contract_head.current_activation_generation
        JOIN processor_contract_versions AS contract
          ON contract.processor_contract_version_id =
             activation.processor_contract_version_id
         AND contract.stage_kind = activation.stage_kind
        WHERE card_head.source_lineage_id = ?
        """,
        (prepared.stage_kind, prepared.source_lineage_id),
    ).fetchone()
    return bool(
        row is not None
        and row["current_card_revision_id"]
        == prepared.evaluated_card_revision_id
        and row["current_input_version_id"]
        == prepared.eligibility_input_version_id
        and int(row["current_input_generation"])
        == prepared.eligibility_input_generation
        and row["current_input_hash"] == prepared.eligibility_inputs_hash
        and row["card_input_hash"] == prepared.eligibility_inputs_hash
        and int(row["current_activation_generation"])
        == prepared.processor_contract_activation_generation
        and row["processor_contract_version_id"]
        == prepared.processor_contract_version_id
        and row["contract_hash"] == prepared.processor_contract_hash
    )


def _current_matches_prepared(
    current: sqlite3.Row,
    prepared: PreparedEligibilityEvaluation,
) -> bool:
    return bool(
        current["evaluated_card_revision_id"]
        == prepared.evaluated_card_revision_id
        and current["current_eligibility_input_version_id"]
        == prepared.eligibility_input_version_id
        and int(current["current_eligibility_input_generation"])
        == prepared.eligibility_input_generation
        and current["current_eligibility_inputs_hash"]
        == prepared.eligibility_inputs_hash
        and current["stage_kind"] == prepared.stage_kind
        and int(current["current_processor_contract_activation_generation"])
        == prepared.processor_contract_activation_generation
        and current["current_processor_contract_version_id"]
        == prepared.processor_contract_version_id
        and current["current_processor_contract_hash"]
        == prepared.processor_contract_hash
        and bool(current["current_eligible"]) == prepared.eligible
        and tuple(json.loads(current["reasons_json"])) == prepared.reasons
    )


def _current_evaluation_row(
    connection: sqlite3.Connection,
    source_lineage_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT head.*, evaluation.reasons_json
        FROM eligibility_heads AS head
        JOIN eligibility_evaluation_versions AS evaluation
          ON evaluation.eligibility_evaluation_id =
             head.current_eligibility_evaluation_id
         AND evaluation.source_lineage_id = head.source_lineage_id
        WHERE head.source_lineage_id = ?
        """,
        (source_lineage_id,),
    ).fetchone()


def _stale_result(
    connection: sqlite3.Connection,
    prepared: PreparedEligibilityEvaluation,
    current: sqlite3.Row | None,
) -> EligibilityEvaluation:
    return EligibilityEvaluation(
        eligibility_evaluation_id=None,
        source_lineage_id=prepared.source_lineage_id,
        evaluated_card_revision_id=prepared.evaluated_card_revision_id,
        generation=(
            0 if current is None else int(current["current_eligibility_generation"])
        ),
        eligibility_input_version_id=prepared.eligibility_input_version_id,
        eligibility_input_generation=prepared.eligibility_input_generation,
        eligibility_inputs_hash=prepared.eligibility_inputs_hash,
        stage_kind=prepared.stage_kind,
        processor_contract_activation_generation=(
            prepared.processor_contract_activation_generation
        ),
        processor_contract_version_id=prepared.processor_contract_version_id,
        processor_contract_hash=prepared.processor_contract_hash,
        eligible=prepared.eligible,
        reasons=prepared.reasons,
        status="stale",
        changed=False,
        dependency_generation=_eligibility_dependency_generation(
            connection,
            prepared.source_lineage_id,
        ),
    )


def _evaluation_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    status: EligibilityStatus,
    changed: bool,
    dependency_generation: int | None = None,
) -> EligibilityEvaluation:
    return EligibilityEvaluation(
        eligibility_evaluation_id=row["current_eligibility_evaluation_id"],
        source_lineage_id=row["source_lineage_id"],
        evaluated_card_revision_id=row["evaluated_card_revision_id"],
        generation=int(row["current_eligibility_generation"]),
        eligibility_input_version_id=row[
            "current_eligibility_input_version_id"
        ],
        eligibility_input_generation=int(
            row["current_eligibility_input_generation"]
        ),
        eligibility_inputs_hash=row["current_eligibility_inputs_hash"],
        stage_kind=row["stage_kind"],
        processor_contract_activation_generation=int(
            row["current_processor_contract_activation_generation"]
        ),
        processor_contract_version_id=row[
            "current_processor_contract_version_id"
        ],
        processor_contract_hash=row["current_processor_contract_hash"],
        eligible=bool(row["current_eligible"]),
        reasons=tuple(json.loads(row["reasons_json"])),
        status=status,
        changed=changed,
        dependency_generation=(
            _eligibility_dependency_generation(
                connection,
                row["source_lineage_id"],
            )
            if dependency_generation is None
            else dependency_generation
        ),
    )


def _evaluate_payload(payload: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    reasons = {
        str(flag)
        for flag in payload.get("quality_flags", ())
        if flag in WIKI_RELEVANT_QUALITY_FLAGS
    }
    if not payload.get("schema_eligible", False):
        reasons.add("schema_ineligible")
    ordered = tuple(sorted(reasons))
    return not ordered, ordered


def _current_card_revision_id(
    connection: sqlite3.Connection,
    source_lineage_id: str,
) -> str:
    row = connection.execute(
        """
        SELECT current_card_revision_id
        FROM source_lineage_heads
        WHERE source_lineage_id = ?
        """,
        (source_lineage_id,),
    ).fetchone()
    if row is None:
        raise StateNotFoundError(f"Lineage {source_lineage_id} has no current card")
    return row["current_card_revision_id"]


def _publish_eligibility_dependency(
    connection: sqlite3.Connection,
    *,
    evaluation_id: str,
    evaluation_generation: int,
    prepared: PreparedEligibilityEvaluation,
    created_at: str,
) -> int:
    payload = {
        "eligibility_evaluation_id": evaluation_id,
        "eligibility_generation": evaluation_generation,
        "source_lineage_id": prepared.source_lineage_id,
        "evaluated_card_revision_id": prepared.evaluated_card_revision_id,
        "eligible": prepared.eligible,
        "reasons": list(prepared.reasons),
    }
    payload_json = canonical_json(payload)
    dependency_hash = content_hash(
        payload,
        namespace=(
            f"wiki-v2-dependency:{DependencyKind.ELIGIBILITY_STATE}:"
            f"{prepared.source_lineage_id}"
        ),
    )
    current = connection.execute(
        """
        SELECT *
        FROM dependency_heads
        WHERE dependency_kind = ? AND dependency_scope_key = ?
        """,
        (DependencyKind.ELIGIBILITY_STATE, prepared.source_lineage_id),
    ).fetchone()
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
        ) VALUES (?, ?, ?, ?, ?, ?, 'ingest', NULL, ?)
        """,
        (
            dependency_version_id,
            DependencyKind.ELIGIBILITY_STATE,
            prepared.source_lineage_id,
            generation,
            dependency_hash,
            payload_json,
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
                DependencyKind.ELIGIBILITY_STATE,
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
                DependencyKind.ELIGIBILITY_STATE,
                prepared.source_lineage_id,
                current["current_dependency_version_id"],
            ),
        )
        if updated.rowcount != 1:
            raise StateConflictError("Eligibility dependency head CAS failed")
    return generation


def _eligibility_dependency_generation(
    connection: sqlite3.Connection,
    source_lineage_id: str,
) -> int:
    row = connection.execute(
        """
        SELECT current_generation
        FROM dependency_heads
        WHERE dependency_kind = ? AND dependency_scope_key = ?
        """,
        (DependencyKind.ELIGIBILITY_STATE, source_lineage_id),
    ).fetchone()
    return 0 if row is None else int(row["current_generation"])
