from __future__ import annotations

import sqlite3

import pytest

from retrieval.wiki.schema import (
    CARD_PROJECTION_STAGE_KIND,
    CLAIM_EXTRACTION_STAGE_KIND,
    CLAIM_PROJECTION_STAGE_KIND,
    HUB_PROJECTION_STAGE_KIND,
)
from retrieval.wiki.state import (
    activate_processor_contract,
    record_card_revision,
    start_stage_run,
)
from tests.wiki_v2.helpers import (
    PreparedStage,
    contract,
    insert_approved_concept,
    prepare_stage,
)


def _claim_inputs_hash(prepared: PreparedStage) -> str:
    return next(
        binding.input_hash
        for binding in prepared.stage.input_bindings
        if binding.input_kind == "claim_inputs"
    )


def _insert_extraction_artifact(
    connection: sqlite3.Connection,
    *,
    artifact_id: str,
    contract_id: str,
    claim_inputs_hash: str,
) -> None:
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
            f"key-{artifact_id}",
            contract_id,
            claim_inputs_hash,
            f"hash-{artifact_id}",
        ),
    )


def _insert_extraction_apply(
    connection: sqlite3.Connection,
    *,
    extraction_run_id: str,
    stage_run_id: str,
    artifact_id: str,
    prepared: PreparedStage,
    contract_id: str,
    claim_inputs_hash: str,
    card_revision_id: str | None = None,
    stage_kind: str = CLAIM_EXTRACTION_STAGE_KIND,
) -> None:
    source_card_revision_id = card_revision_id or prepared.card_revision_id
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
            stage_kind,
            contract_id,
            claim_inputs_hash,
            source_card_revision_id,
            source_card_revision_id,
        ),
    )


def _insert_projection_artifact(
    connection: sqlite3.Connection,
    *,
    prepared: PreparedStage,
    artifact_id: str,
    projection_kind: str,
    scope_key: str,
    contract_id: str | None = None,
    stage_version_id: str | None = None,
    card_revision_id: str | None = None,
    claim_group_id: str | None = None,
    concept_id: str | None = None,
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
            contract_id or prepared.stage.processor_contract_version_id,
            stage_version_id or prepared.stage.stage_version_id,
        ),
    )


def _insert_card_projection_head(
    connection: sqlite3.Connection,
    *,
    artifact_id: str,
    card_revision_id: str,
    generation: int,
    inputs_hash: str,
    output_hash: str,
    fts_hash: str,
) -> None:
    connection.execute(
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
        ) VALUES ('card', ?, ?, NULL, NULL, ?, ?, ?, ?, ?, 'audit')
        """,
        (
            card_revision_id,
            card_revision_id,
            artifact_id,
            generation,
            inputs_hash,
            output_hash,
            fts_hash,
        ),
    )


def test_exact_extraction_artifact_apply_is_structurally_compatible(wiki_db) -> None:
    prepared = prepare_stage(
        wiki_db,
        stage_kind=CLAIM_EXTRACTION_STAGE_KIND,
        external_key="telegram:exact-extraction:1",
    )
    claim_hash = _claim_inputs_hash(prepared)
    run = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="exact-extraction",
        artifact_source_card_revision_id=prepared.card_revision_id,
    )
    assert run.stage_kind == CLAIM_EXTRACTION_STAGE_KIND
    assert (
        run.processor_contract_version_id
        == prepared.stage.processor_contract_version_id
    )

    _insert_extraction_artifact(
        wiki_db,
        artifact_id="exact-artifact",
        contract_id=run.processor_contract_version_id,
        claim_inputs_hash=claim_hash,
    )
    _insert_extraction_apply(
        wiki_db,
        extraction_run_id="exact-apply",
        stage_run_id=run.stage_run_id,
        artifact_id="exact-artifact",
        prepared=prepared,
        contract_id=run.processor_contract_version_id,
        claim_inputs_hash=claim_hash,
    )

    stored = wiki_db.execute(
        """
        SELECT stage_kind, processor_contract_version_id, claim_inputs_hash
        FROM extraction_runs
        WHERE extraction_run_id = 'exact-apply'
        """
    ).fetchone()
    assert tuple(stored) == (
        CLAIM_EXTRACTION_STAGE_KIND,
        run.processor_contract_version_id,
        claim_hash,
    )
    assert wiki_db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert wiki_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_extraction_apply_rejects_artifact_input_hash_mismatch(wiki_db) -> None:
    prepared = prepare_stage(
        wiki_db,
        external_key="telegram:artifact-input-mismatch:1",
    )
    run = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="artifact-input-mismatch",
        artifact_source_card_revision_id=prepared.card_revision_id,
    )
    _insert_extraction_artifact(
        wiki_db,
        artifact_id="wrong-input-artifact",
        contract_id=run.processor_contract_version_id,
        claim_inputs_hash="wrong-claim-inputs-hash",
    )

    with pytest.raises(sqlite3.IntegrityError, match="matching claim_extraction"):
        _insert_extraction_apply(
            wiki_db,
            extraction_run_id="wrong-input-apply",
            stage_run_id=run.stage_run_id,
            artifact_id="wrong-input-artifact",
            prepared=prepared,
            contract_id=run.processor_contract_version_id,
            claim_inputs_hash="wrong-claim-inputs-hash",
        )


def test_extraction_apply_rejects_source_card_claim_binding_mismatch(wiki_db) -> None:
    prepared = prepare_stage(
        wiki_db,
        external_key="telegram:card-binding-mismatch:1",
    )
    original_claim_hash = _claim_inputs_hash(prepared)
    changed_card = record_card_revision(
        wiki_db,
        source_lineage_id=prepared.lineage_id,
        card_payload={"summary": "B", "key_points": ["claim B"]},
        input_payloads={
            "claim_inputs": {"key_points": ["claim B"]},
            "card_projection_inputs": {
                "summary": "B",
                "key_points": ["claim B"],
            },
        },
    )
    run = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="card-binding-mismatch",
        artifact_source_card_revision_id=changed_card.card_revision_id,
    )
    _insert_extraction_artifact(
        wiki_db,
        artifact_id="old-input-artifact",
        contract_id=run.processor_contract_version_id,
        claim_inputs_hash=original_claim_hash,
    )

    with pytest.raises(sqlite3.IntegrityError, match="matching claim_extraction"):
        _insert_extraction_apply(
            wiki_db,
            extraction_run_id="card-binding-mismatch-apply",
            stage_run_id=run.stage_run_id,
            artifact_id="old-input-artifact",
            prepared=prepared,
            contract_id=run.processor_contract_version_id,
            claim_inputs_hash=original_claim_hash,
            card_revision_id=changed_card.card_revision_id,
        )


def test_stage_run_and_extraction_apply_reject_contract_a_b_mismatch(wiki_db) -> None:
    prepared = prepare_stage(
        wiki_db,
        external_key="telegram:contract-mismatch:1",
    )
    claim_hash = _claim_inputs_hash(prepared)
    contract_b = activate_processor_contract(
        wiki_db,
        stage_kind=CLAIM_EXTRACTION_STAGE_KIND,
        contract=contract("B"),
    )

    with pytest.raises(sqlite3.IntegrityError):
        wiki_db.execute(
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
                started_at
            ) VALUES (?, ?, ?, ?, ?, 'bad-contract-run', 'started', ?, ?, 'audit')
            """,
            (
                "bad-contract-run",
                prepared.stage.stage_version_id,
                prepared.lineage_id,
                CLAIM_EXTRACTION_STAGE_KIND,
                contract_b.processor_contract_version_id,
                prepared.stage.generation,
                prepared.stage.contract_activation_generation,
            ),
        )

    run = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="contract-a-run",
        artifact_source_card_revision_id=prepared.card_revision_id,
    )
    _insert_extraction_artifact(
        wiki_db,
        artifact_id="contract-b-artifact",
        contract_id=contract_b.processor_contract_version_id,
        claim_inputs_hash=claim_hash,
    )
    with pytest.raises(sqlite3.IntegrityError, match="matching claim_extraction"):
        _insert_extraction_apply(
            wiki_db,
            extraction_run_id="contract-b-apply",
            stage_run_id=run.stage_run_id,
            artifact_id="contract-b-artifact",
            prepared=prepared,
            contract_id=contract_b.processor_contract_version_id,
            claim_inputs_hash=claim_hash,
        )


def test_non_extraction_stage_cannot_create_or_apply_extraction_artifact(
    wiki_db,
) -> None:
    prepared = prepare_stage(
        wiki_db,
        stage_kind="relation_linking",
        external_key="telegram:wrong-extraction-stage:1",
    )
    claim_hash = _claim_inputs_hash(prepared)
    relation_run = start_stage_run(
        wiki_db,
        stage_version_id=prepared.stage.stage_version_id,
        idempotency_key="relation-run",
        artifact_source_card_revision_id=prepared.card_revision_id,
    )

    with pytest.raises(sqlite3.IntegrityError, match="claim_extraction contract"):
        _insert_extraction_artifact(
            wiki_db,
            artifact_id="relation-artifact",
            contract_id=prepared.stage.processor_contract_version_id,
            claim_inputs_hash=claim_hash,
        )

    claim_contract = activate_processor_contract(
        wiki_db,
        stage_kind=CLAIM_EXTRACTION_STAGE_KIND,
        contract=contract("claim-for-stage-guard"),
    )
    _insert_extraction_artifact(
        wiki_db,
        artifact_id="valid-claim-artifact",
        contract_id=claim_contract.processor_contract_version_id,
        claim_inputs_hash=claim_hash,
    )
    with pytest.raises(sqlite3.IntegrityError, match="matching claim_extraction"):
        _insert_extraction_apply(
            wiki_db,
            extraction_run_id="relation-apply",
            stage_run_id=relation_run.stage_run_id,
            artifact_id="valid-claim-artifact",
            prepared=prepared,
            contract_id=claim_contract.processor_contract_version_id,
            claim_inputs_hash=claim_hash,
        )


def test_each_projection_kind_rejects_unmapped_stage(wiki_db) -> None:
    prepared = prepare_stage(
        wiki_db,
        stage_kind=CLAIM_EXTRACTION_STAGE_KIND,
        external_key="telegram:wrong-projection-stage:1",
    )
    wiki_db.execute(
        """
        INSERT INTO claim_groups (
            claim_group_id,
            canonical_claim_hash,
            canonical_claim_json,
            created_at
        ) VALUES ('guarded-group', 'guarded-hash', '{}', 'audit')
        """
    )
    insert_approved_concept(wiki_db, concept_id="guarded-concept")
    cases = (
        {
            "projection_kind": "card",
            "scope_key": prepared.card_revision_id,
            "card_revision_id": prepared.card_revision_id,
        },
        {
            "projection_kind": "claim",
            "scope_key": "guarded-group",
            "claim_group_id": "guarded-group",
        },
        {
            "projection_kind": "hub",
            "scope_key": "guarded-concept",
            "concept_id": "guarded-concept",
        },
    )

    for index, case in enumerate(cases):
        with pytest.raises(sqlite3.IntegrityError, match="mapped stage kind"):
            _insert_projection_artifact(
                wiki_db,
                prepared=prepared,
                artifact_id=f"wrong-stage-projection-{index}",
                **case,
            )


def test_projection_artifact_rejects_stage_contract_a_b_mismatch(wiki_db) -> None:
    prepared = prepare_stage(
        wiki_db,
        stage_kind=CARD_PROJECTION_STAGE_KIND,
        external_key="telegram:projection-contract-mismatch:1",
    )
    contract_b = activate_processor_contract(
        wiki_db,
        stage_kind=CARD_PROJECTION_STAGE_KIND,
        contract=contract("card-projection-B"),
    )

    with pytest.raises(sqlite3.IntegrityError, match="mapped stage kind"):
        _insert_projection_artifact(
            wiki_db,
            prepared=prepared,
            artifact_id="projection-contract-b",
            projection_kind="card",
            scope_key=prepared.card_revision_id,
            card_revision_id=prepared.card_revision_id,
            contract_id=contract_b.processor_contract_version_id,
        )


def test_card_projection_scope_identity_and_rebuild_are_strict(wiki_db) -> None:
    prepared = prepare_stage(
        wiki_db,
        stage_kind=CARD_PROJECTION_STAGE_KIND,
        external_key="telegram:strict-card-projection:1",
    )

    with pytest.raises(sqlite3.IntegrityError, match="mapped stage kind"):
        _insert_projection_artifact(
            wiki_db,
            prepared=prepared,
            artifact_id="orphan-card-artifact",
            projection_kind="card",
            scope_key="missing-card",
            card_revision_id="missing-card",
        )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_projection_artifact(
            wiki_db,
            prepared=prepared,
            artifact_id="mismatched-card-artifact",
            projection_kind="card",
            scope_key="wrong-scope",
            card_revision_id=prepared.card_revision_id,
        )

    foreign_card = prepare_stage(
        wiki_db,
        stage_kind=CARD_PROJECTION_STAGE_KIND,
        external_key="telegram:strict-card-projection:foreign",
    )
    with pytest.raises(sqlite3.IntegrityError, match="mapped stage kind"):
        _insert_projection_artifact(
            wiki_db,
            prepared=prepared,
            artifact_id="foreign-lineage-card-artifact",
            projection_kind="card",
            scope_key=foreign_card.card_revision_id,
            card_revision_id=foreign_card.card_revision_id,
        )

    _insert_projection_artifact(
        wiki_db,
        prepared=prepared,
        artifact_id="card-artifact-1",
        projection_kind="card",
        scope_key=prepared.card_revision_id,
        card_revision_id=prepared.card_revision_id,
    )
    _insert_card_projection_head(
        wiki_db,
        artifact_id="card-artifact-1",
        card_revision_id=prepared.card_revision_id,
        generation=1,
        inputs_hash="inputs",
        output_hash="output",
        fts_hash="fts",
    )

    other_card = record_card_revision(
        wiki_db,
        source_lineage_id=prepared.lineage_id,
        card_payload={"summary": "other"},
        input_payloads={
            "claim_inputs": {"key_points": ["claim A"]},
            "card_projection_inputs": {"summary": "other"},
        },
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_card_projection_head(
            wiki_db,
            artifact_id="card-artifact-1",
            card_revision_id=other_card.card_revision_id,
            generation=1,
            inputs_hash="inputs",
            output_hash="output",
            fts_hash="fts",
        )

    _insert_projection_artifact(
        wiki_db,
        prepared=prepared,
        artifact_id="card-artifact-2",
        projection_kind="card",
        scope_key=prepared.card_revision_id,
        card_revision_id=prepared.card_revision_id,
        generation=2,
        inputs_hash="inputs-2",
        output_hash="output-2",
        fts_hash="fts-2",
    )
    wiki_db.execute(
        """
        UPDATE projection_heads
        SET
            current_projection_artifact_id = 'card-artifact-2',
            current_projection_generation = 2,
            current_projection_inputs_hash = 'inputs-2',
            current_projection_output_hash = 'output-2',
            current_fts_document_hash = 'fts-2',
            updated_at = 'audit-2'
        WHERE projection_kind = 'card'
          AND projection_scope_key = ?
        """,
        (prepared.card_revision_id,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="generation must increase"):
        wiki_db.execute(
            """
            UPDATE projection_heads
            SET
                current_projection_artifact_id = 'card-artifact-1',
                current_projection_generation = 1,
                current_projection_inputs_hash = 'inputs',
                current_projection_output_hash = 'output',
                current_fts_document_hash = 'fts',
                updated_at = 'audit-rollback'
            WHERE projection_kind = 'card'
              AND projection_scope_key = ?
            """,
            (prepared.card_revision_id,),
        )

    head = wiki_db.execute(
        """
        SELECT
            card_revision_id,
            current_projection_generation,
            current_projection_output_hash
        FROM projection_heads
        WHERE projection_kind = 'card'
          AND projection_scope_key = ?
        """,
        (prepared.card_revision_id,),
    ).fetchone()
    assert tuple(head) == (prepared.card_revision_id, 2, "output-2")
    assert wiki_db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert wiki_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize(
    ("projection_kind", "stage_kind"),
    [
        ("card", CARD_PROJECTION_STAGE_KIND),
        ("claim", CLAIM_PROJECTION_STAGE_KIND),
        ("hub", HUB_PROJECTION_STAGE_KIND),
    ],
)
def test_canonical_projection_stage_kind_constants(
    projection_kind: str,
    stage_kind: str,
) -> None:
    assert stage_kind == f"{projection_kind}_projection"
