from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from retrieval.wiki.state import (
    DependencyHead,
    DependencyKey,
    ProcessorContractSpec,
    StageVersion,
    activate_processor_contract,
    ensure_source_lineage,
    publish_dependency,
    record_card_revision,
    schedule_stage,
)


@dataclass(frozen=True)
class PreparedStage:
    lineage_id: str
    card_revision_id: str
    dependency: DependencyHead
    stage: StageVersion


@dataclass(frozen=True)
class PreparedOccurrence:
    extraction_artifact_id: str
    extraction_run_id: str
    occurrence_version_id: str


def contract(label: str) -> ProcessorContractSpec:
    return ProcessorContractSpec(
        algorithm_version=f"algorithm-{label}",
        schema_version="schema-v1",
        canonicalizer_version="canonical-v1",
        policy_version=f"policy-{label}",
        prompt_template_version=f"prompt-{label}",
        model_profile_version=f"model-{label}",
    )


def prepare_stage(
    connection: sqlite3.Connection,
    *,
    stage_kind: str = "claim_extraction",
    external_key: str = "telegram:test:1",
) -> PreparedStage:
    lineage = ensure_source_lineage(
        connection,
        source_kind="telegram",
        external_key=external_key,
    )
    card = record_card_revision(
        connection,
        source_lineage_id=lineage.source_lineage_id,
        card_payload={"summary": "A", "key_points": ["claim A"]},
        input_payloads={
            "claim_inputs": {"key_points": ["claim A"]},
            "card_projection_inputs": {"summary": "A", "key_points": ["claim A"]},
        },
    )
    activate_processor_contract(
        connection,
        stage_kind=stage_kind,
        contract=contract("A"),
    )
    dependency = publish_dependency(
        connection,
        dependency_kind="candidate_snapshot",
        dependency_scope_key=lineage.source_lineage_id,
        payload={"candidate_concept_ids": ["concept-a"]},
        expected_version_id=None,
        producer_kind="registry",
    )
    stage = schedule_stage(
        connection,
        source_lineage_id=lineage.source_lineage_id,
        stage_kind=stage_kind,
        input_kinds=["claim_inputs"],
        dependencies=[
            DependencyKey("candidate_snapshot", lineage.source_lineage_id)
        ],
    )
    return PreparedStage(
        lineage_id=lineage.source_lineage_id,
        card_revision_id=card.card_revision_id,
        dependency=dependency,
        stage=stage,
    )


def insert_occurrence_fixture(
    connection: sqlite3.Connection,
    *,
    prepared: PreparedStage,
    stage_run_id: str,
    suffix: str,
) -> PreparedOccurrence:
    artifact_id = f"artifact-{suffix}"
    extraction_run_id = f"extraction-{suffix}"
    occurrence_id = f"occurrence-{suffix}"
    connection.execute(
        """
        INSERT INTO extraction_artifacts (
            extraction_artifact_id,
            extraction_artifact_key,
            processor_contract_version_id,
            claim_inputs_hash,
            artifact_hash,
            artifact_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, '{}', 'audit')
        """,
        (
            artifact_id,
            f"artifact-key-{suffix}",
            prepared.stage.processor_contract_version_id,
            prepared.stage.input_bindings[0].input_hash,
            f"artifact-hash-{suffix}",
        ),
    )
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
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'audit')
        """,
        (
            extraction_run_id,
            stage_run_id,
            artifact_id,
            prepared.lineage_id,
            prepared.stage.stage_kind,
            prepared.stage.processor_contract_version_id,
            prepared.stage.input_bindings[0].input_hash,
            prepared.card_revision_id,
            prepared.card_revision_id,
        ),
    )
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
        ) VALUES (?, ?, ?, ?, 'key_point', '{}', '{}', ?, ?, 'v1', ?, 'audit')
        """,
        (
            occurrence_id,
            prepared.lineage_id,
            prepared.card_revision_id,
            extraction_run_id,
            f"payload-hash-{suffix}",
            f"fingerprint-{suffix}",
            prepared.stage.input_bindings[0].input_hash,
        ),
    )
    connection.execute(
        """
        INSERT INTO extraction_run_occurrences (
            extraction_run_id,
            occurrence_version_id,
            source_lineage_id,
            manifest_ordinal
        ) VALUES (?, ?, ?, 0)
        """,
        (extraction_run_id, occurrence_id, prepared.lineage_id),
    )
    return PreparedOccurrence(
        extraction_artifact_id=artifact_id,
        extraction_run_id=extraction_run_id,
        occurrence_version_id=occurrence_id,
    )


def insert_approved_concept(
    connection: sqlite3.Connection,
    *,
    concept_id: str,
) -> str:
    revision_id = f"{concept_id}-revision-1"
    connection.execute(
        """
        INSERT INTO concepts (
            concept_id, concept_kind, approval_status, canonical_key, created_at
        ) VALUES (?, 'entity', 'approved', ?, 'audit')
        """,
        (concept_id, f"canonical-{concept_id}"),
    )
    connection.execute(
        """
        INSERT INTO concept_revisions (
            concept_revision_id,
            concept_id,
            concept_generation,
            identity_hash,
            display_hash,
            hierarchy_hash,
            canonical_payload_json,
            created_at
        ) VALUES (?, ?, 1, 'identity', 'display', 'hierarchy', '{}', 'audit')
        """,
        (revision_id, concept_id),
    )
    connection.execute(
        """
        INSERT INTO concept_heads (
            concept_id,
            current_concept_revision_id,
            current_concept_generation,
            updated_at
        ) VALUES (?, ?, 1, 'audit')
        """,
        (concept_id, revision_id),
    )
    return revision_id
