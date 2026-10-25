from __future__ import annotations

import sqlite3

import pytest

from retrieval.wiki.schema import connect_database
from retrieval.wiki.state import (
    DependencyKey,
    ProcessorContractSpec,
    StaleHeadError,
    StateConflictError,
    activate_processor_contract,
    advance_input_head,
    ensure_source_lineage,
    get_dependency_head,
    get_input_head,
    publish_dependency,
    record_card_revision,
    schedule_stage,
)
from tests.wiki_v2.helpers import contract


def test_input_generation_same_hash_and_a_to_b_to_a(wiki_db) -> None:
    lineage = ensure_source_lineage(
        wiki_db,
        source_kind="telegram",
        external_key="telegram:inputs:1",
    )

    first = advance_input_head(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        input_kind="claim_inputs",
        payload={"claims": ["A"]},
    )
    same = advance_input_head(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        input_kind="claim_inputs",
        payload={"claims": ["A"]},
    )
    second = advance_input_head(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        input_kind="claim_inputs",
        payload={"claims": ["B"]},
    )
    third = advance_input_head(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        input_kind="claim_inputs",
        payload={"claims": ["A"]},
    )

    assert (first.generation, first.changed) == (1, True)
    assert (same.generation, same.changed) == (1, False)
    assert same.input_version_id == first.input_version_id
    assert second.generation == 2
    assert third.generation == 3
    assert third.input_hash == first.input_hash
    assert third.input_version_id != first.input_version_id


def test_projection_change_does_not_move_claim_input(wiki_db) -> None:
    lineage = ensure_source_lineage(
        wiki_db,
        source_kind="telegram",
        external_key="telegram:projection:1",
    )
    first = record_card_revision(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        card_payload={"summary": "old", "key_points": ["stable claim"]},
        input_payloads={
            "claim_inputs": {"key_points": ["stable claim"]},
            "card_projection_inputs": {
                "summary": "old",
                "key_points": ["stable claim"],
            },
        },
    )
    second = record_card_revision(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        card_payload={"summary": "new", "key_points": ["stable claim"]},
        input_payloads={
            "claim_inputs": {"key_points": ["stable claim"]},
            "card_projection_inputs": {
                "summary": "new",
                "key_points": ["stable claim"],
            },
        },
    )

    first_heads = {head.input_kind: head for head in first.input_heads}
    second_heads = {head.input_kind: head for head in second.input_heads}
    assert second.card_head_generation == 2
    assert second_heads["claim_inputs"].generation == 1
    assert (
        second_heads["claim_inputs"].input_version_id
        == first_heads["claim_inputs"].input_version_id
    )
    assert second_heads["card_projection_inputs"].generation == 2


def test_one_field_can_contribute_to_multiple_input_kinds(wiki_db) -> None:
    lineage = ensure_source_lineage(
        wiki_db,
        source_kind="telegram",
        external_key="telegram:overlap:1",
    )
    card = record_card_revision(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        card_payload={"key_points": ["shared"]},
        input_payloads={
            "claim_inputs": {"key_points": ["shared"]},
            "card_projection_inputs": {"key_points": ["shared"]},
        },
    )
    assert [head.input_kind for head in card.input_heads] == [
        "card_projection_inputs",
        "claim_inputs",
    ]


def test_processor_activation_history_supports_a_b_a(wiki_db) -> None:
    first = activate_processor_contract(
        wiki_db,
        stage_kind="grouping",
        contract=contract("A"),
    )
    same = activate_processor_contract(
        wiki_db,
        stage_kind="grouping",
        contract=contract("A"),
    )
    second = activate_processor_contract(
        wiki_db,
        stage_kind="grouping",
        contract=contract("B"),
    )
    third = activate_processor_contract(
        wiki_db,
        stage_kind="grouping",
        contract=contract("A"),
    )

    assert (first.activation_generation, first.changed) == (1, True)
    assert (same.activation_generation, same.changed) == (1, False)
    assert second.activation_generation == 2
    assert third.activation_generation == 3
    assert third.processor_contract_version_id == first.processor_contract_version_id

    history = wiki_db.execute(
        """
        SELECT activation_generation, processor_contract_version_id
        FROM processor_contract_activations
        WHERE stage_kind = 'grouping'
        ORDER BY activation_generation
        """
    ).fetchall()
    assert [row["activation_generation"] for row in history] == [1, 2, 3]
    assert history[0]["processor_contract_version_id"] == history[2][
        "processor_contract_version_id"
    ]


def test_dependency_head_compare_and_swap_with_two_connections(tmp_path) -> None:
    path = tmp_path / "concurrent.sqlite"
    first_connection = connect_database(path)
    second_connection = connect_database(path)
    try:
        initial = publish_dependency(
            first_connection,
            dependency_kind="registry_snapshot",
            dependency_scope_key="global",
            payload={"revision": "A"},
            expected_version_id=None,
            producer_kind="registry",
        )
        first_snapshot = get_dependency_head(
            first_connection,
            dependency_kind="registry_snapshot",
            dependency_scope_key="global",
        )
        second_snapshot = get_dependency_head(
            second_connection,
            dependency_kind="registry_snapshot",
            dependency_scope_key="global",
        )
        assert first_snapshot is not None
        assert second_snapshot is not None
        assert first_snapshot.dependency_version_id == initial.dependency_version_id
        assert second_snapshot.dependency_version_id == initial.dependency_version_id

        second = publish_dependency(
            first_connection,
            dependency_kind="registry_snapshot",
            dependency_scope_key="global",
            payload={"revision": "B"},
            expected_version_id=first_snapshot.dependency_version_id,
            producer_kind="registry",
        )
        # BEGIN IMMEDIATE serializes writers; connection two still carries the
        # pre-write snapshot and must lose the version-ID compare-and-swap.
        with pytest.raises(StaleHeadError, match="expected"):
            publish_dependency(
                second_connection,
                dependency_kind="registry_snapshot",
                dependency_scope_key="global",
                payload={"revision": "C"},
                expected_version_id=second_snapshot.dependency_version_id,
                producer_kind="registry",
            )
        current = get_dependency_head(
            first_connection,
            dependency_kind="registry_snapshot",
            dependency_scope_key="global",
        )
        assert current is not None
        assert current.dependency_version_id == second.dependency_version_id
        assert current.generation == 2
    finally:
        first_connection.close()
        second_connection.close()


def test_dependency_a_to_b_to_a_uses_new_generation(wiki_db) -> None:
    first = publish_dependency(
        wiki_db,
        dependency_kind="surface_resolution",
        dependency_scope_key="кнр",
        payload={"concept_ids": ["china"]},
        expected_version_id=None,
        producer_kind="registry",
    )
    second = publish_dependency(
        wiki_db,
        dependency_kind="surface_resolution",
        dependency_scope_key="кнр",
        payload={"concept_ids": []},
        expected_version_id=first.dependency_version_id,
        producer_kind="registry",
    )
    third = publish_dependency(
        wiki_db,
        dependency_kind="surface_resolution",
        dependency_scope_key="кнр",
        payload={"concept_ids": ["china"]},
        expected_version_id=second.dependency_version_id,
        producer_kind="registry",
    )
    assert third.generation == 3
    assert third.dependency_hash == first.dependency_hash
    assert third.dependency_version_id != first.dependency_version_id


def test_stage_version_hashes_full_sorted_binding_snapshot(wiki_db) -> None:
    lineage = ensure_source_lineage(
        wiki_db,
        source_kind="telegram",
        external_key="telegram:stage-sort:1",
    )
    record_card_revision(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        card_payload={"summary": "sort", "key_points": ["one"]},
        input_payloads={
            "z_projection_inputs": {"summary": "sort"},
            "a_claim_inputs": {"key_points": ["one"]},
        },
    )
    activate_processor_contract(
        wiki_db,
        stage_kind="relation_linking",
        contract=contract("sort"),
    )
    dependency_z = publish_dependency(
        wiki_db,
        dependency_kind="registry_snapshot",
        dependency_scope_key="z",
        payload={"z": 1},
        expected_version_id=None,
        producer_kind="registry",
    )
    dependency_a = publish_dependency(
        wiki_db,
        dependency_kind="candidate_snapshot",
        dependency_scope_key="a",
        payload={"a": 1},
        expected_version_id=None,
        producer_kind="registry",
    )

    first = schedule_stage(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        stage_kind="relation_linking",
        input_kinds=["z_projection_inputs", "a_claim_inputs"],
        dependencies=[
            DependencyKey("registry_snapshot", "z"),
            DependencyKey("candidate_snapshot", "a"),
        ],
    )
    same = schedule_stage(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        stage_kind="relation_linking",
        input_kinds=["a_claim_inputs", "z_projection_inputs"],
        dependencies=[
            DependencyKey("candidate_snapshot", "a"),
            DependencyKey("registry_snapshot", "z"),
        ],
    )

    assert [binding.input_kind for binding in first.input_bindings] == [
        "a_claim_inputs",
        "z_projection_inputs",
    ]
    assert [
        (binding.dependency_kind, binding.dependency_scope_key)
        for binding in first.dependency_bindings
    ] == [
        ("candidate_snapshot", "a"),
        ("registry_snapshot", "z"),
    ]
    assert first.stage_inputs_hash == same.stage_inputs_hash
    assert same.stage_version_id == first.stage_version_id
    assert same.changed is False
    assert {
        binding.dependency_version_id for binding in first.dependency_bindings
    } == {
        dependency_z.dependency_version_id,
        dependency_a.dependency_version_id,
    }


@pytest.mark.parametrize(
    "forbidden_kind",
    [
        "dedup_policy",
        "relation_policy",
        "hub_builder_version",
        "model_profile_version",
        "prompt_template_version",
        "schema_version",
        "canonicalizer_version",
    ],
)
def test_method_contract_parts_cannot_be_dependencies(
    wiki_db,
    forbidden_kind: str,
) -> None:
    specification = ProcessorContractSpec(
        algorithm_version="group-v1",
        schema_version="claim-v1",
        canonicalizer_version="canonical-v1",
        policy_version="dedup-policy-v1",
    )
    activation = activate_processor_contract(
        wiki_db,
        stage_kind="grouping",
        contract=specification,
    )
    contract_json = wiki_db.execute(
        """
        SELECT contract_json
        FROM processor_contract_versions
        WHERE processor_contract_version_id = ?
        """,
        (activation.processor_contract_version_id,),
    ).fetchone()["contract_json"]

    assert '"policy_version":"dedup-policy-v1"' in contract_json
    with pytest.raises(StateConflictError, match="Unsupported data dependency"):
        DependencyKey(forbidden_kind, "global")
    with pytest.raises(StateConflictError, match="Unsupported data dependency"):
        publish_dependency(
            wiki_db,
            dependency_kind=forbidden_kind,
            dependency_scope_key="global",
            payload={"method": "must stay in contract"},
            expected_version_id=None,
            producer_kind="registry",
        )
    with pytest.raises(sqlite3.IntegrityError):
        wiki_db.execute(
            """
            INSERT INTO dependency_versions (
                dependency_version_id,
                dependency_kind,
                dependency_scope_key,
                dependency_generation,
                dependency_hash,
                canonical_payload_json,
                producer_kind,
                created_at
            ) VALUES (?, ?, 'global', 1, 'hash', '{}', 'registry', 'audit')
            """,
            (f"invalid-{forbidden_kind}", forbidden_kind),
        )


def test_get_input_head_returns_current_generation(wiki_db) -> None:
    lineage = ensure_source_lineage(
        wiki_db,
        source_kind="telegram",
        external_key="telegram:get-input:1",
    )
    created = advance_input_head(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        input_kind="eligibility_inputs",
        payload={"eligible": True},
    )
    loaded = get_input_head(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        input_kind="eligibility_inputs",
    )
    assert loaded is not None
    assert loaded.input_version_id == created.input_version_id
