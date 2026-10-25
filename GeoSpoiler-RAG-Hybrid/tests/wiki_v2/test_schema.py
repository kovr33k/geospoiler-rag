from __future__ import annotations

import sqlite3

import pytest

from retrieval.wiki.schema import (
    SCHEMA_CONTRACT,
    SCHEMA_DDL,
    SCHEMA_VERSION,
    SchemaCompatibilityError,
    UnknownSchemaVersionError,
    connect_database,
    initialize_schema,
)
from retrieval.wiki.state import (
    DependencyKey,
    activate_processor_contract,
    ensure_source_lineage,
    publish_dependency,
    record_card_revision,
    schedule_stage,
)
from tests.wiki_v2.helpers import contract, prepare_stage

REQUIRED_TABLES = {
    "schema_metadata",
    "source_lineages",
    "card_revisions",
    "source_lineage_heads",
    "lineage_input_versions",
    "lineage_input_heads",
    "card_revision_input_bindings",
    "processor_contract_versions",
    "processor_contract_activations",
    "active_processor_contract_heads",
    "dependency_versions",
    "dependency_heads",
    "lineage_stage_versions",
    "lineage_stage_heads",
    "lineage_stage_input_bindings",
    "stage_dependency_bindings",
    "stage_runs",
    "outbox_events",
    "extraction_runs",
    "extraction_artifacts",
    "extraction_artifact_items",
    "claim_occurrences",
    "extraction_run_occurrences",
    "occurrence_state_events",
    "eligibility_evaluation_versions",
    "eligibility_heads",
    "concepts",
    "concept_revisions",
    "concept_heads",
    "identity_aliases",
    "surface_revisions",
    "surface_heads",
    "concept_proposals",
    "concept_proposal_evidence",
    "concept_review_decisions",
    "claim_groups",
    "automatic_group_memberships",
    "claim_group_overrides",
    "occurrence_concept_automatic_links",
    "occurrence_concept_link_overrides",
    "metonym_candidates",
    "metonym_overrides",
    "card_relations",
    "card_relation_contributors",
    "hierarchy_proposals",
    "approved_primary_hierarchy_edges",
    "approved_related_concept_edges",
    "llm_analysis_artifacts",
    "manual_sidecars",
    "manual_sidecar_heads",
    "projection_artifacts",
    "projection_heads",
}

REQUIRED_VIEWS = {
    "occurrence_current_states",
    "lifecycle_active_occurrences",
    "effective_active_occurrences",
    "approved_concepts",
    "concept_proposal_current_decisions",
    "effective_claim_group_memberships",
    "effective_occurrence_concept_links",
    "effective_primary_hierarchy_edges",
    "effective_related_concept_edges",
}


def test_schema_initialization_is_idempotent_and_complete(wiki_db) -> None:
    initialize_schema(wiki_db)
    initialize_schema(wiki_db)

    tables = {
        row["name"]
        for row in wiki_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    views = {
        row["name"]
        for row in wiki_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view'"
        )
    }
    assert REQUIRED_TABLES <= tables
    assert REQUIRED_VIEWS <= views
    metadata = wiki_db.execute(
        """
        SELECT schema_version, schema_contract
        FROM schema_metadata
        WHERE metadata_id = 1
        """
    ).fetchone()
    assert tuple(metadata) == (SCHEMA_VERSION, SCHEMA_CONTRACT)
    assert "__WIKI_" not in SCHEMA_DDL
    assert wiki_db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_old_v1_shape_is_rejected_before_ddl_and_not_mutated(tmp_path) -> None:
    path = tmp_path / "old-v1.sqlite"
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE schema_metadata (
            metadata_id INTEGER PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            schema_contract TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    raw.execute(
        """
        INSERT INTO schema_metadata
        VALUES (1, 1, 'geospoiler-wiki-sqlite-v1', 'audit-only')
        """
    )
    raw.execute(
        """
        CREATE TABLE stage_runs (
            stage_run_id TEXT PRIMARY KEY,
            legacy_status TEXT NOT NULL
        )
        """
    )
    raw.commit()
    before = raw.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
        """
    ).fetchall()
    raw.close()

    connection = connect_database(path, initialize=False)
    try:
        with pytest.raises(UnknownSchemaVersionError, match="version 1"):
            initialize_schema(connection)
        after = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT name, sql
                FROM sqlite_master
                WHERE type = 'table'
                ORDER BY name
                """
            )
        ]
        assert after == before
        assert (
            connection.execute(
                "SELECT schema_version FROM schema_metadata WHERE metadata_id = 1"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'source_lineages'"
        ).fetchone() is None
    finally:
        connection.close()


def test_dev_v2_shape_is_rejected_before_ddl_and_not_mutated(tmp_path) -> None:
    path = tmp_path / "dev-v2.sqlite"
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE schema_metadata (
            metadata_id INTEGER PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            schema_contract TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    raw.execute(
        """
        INSERT INTO schema_metadata
        VALUES (
            1,
            2,
            'geospoiler-wiki-sqlite-v2-stage-bound-artifacts',
            'audit-only'
        )
        """
    )
    raw.execute(
        "CREATE TABLE stage_runs (stage_run_id TEXT PRIMARY KEY, old_shape TEXT)"
    )
    raw.commit()
    before = raw.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    raw.close()

    connection = connect_database(path, initialize=False)
    try:
        with pytest.raises(UnknownSchemaVersionError, match="version 2"):
            initialize_schema(connection)
        after = [
            tuple(row)
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        assert after == before
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'eligibility_heads'"
        ).fetchone() is None
    finally:
        connection.close()


def test_dev_v3_shape_is_rejected_before_ddl_and_not_mutated(tmp_path) -> None:
    path = tmp_path / "dev-v3.sqlite"
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE schema_metadata (
            metadata_id INTEGER PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            schema_contract TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    raw.execute(
        """
        INSERT INTO schema_metadata
        VALUES (
            1,
            3,
            'geospoiler-wiki-sqlite-v3-ingest-lifecycle-eligibility',
            'audit-only'
        )
        """
    )
    raw.execute(
        """
        CREATE TABLE eligibility_heads (
            source_lineage_id TEXT PRIMARY KEY,
            current_eligibility_generation INTEGER NOT NULL
        )
        """
    )
    raw.commit()
    before = raw.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    raw.close()

    connection = connect_database(path, initialize=False)
    try:
        with pytest.raises(UnknownSchemaVersionError, match="version 3"):
            initialize_schema(connection)
        after = [
            tuple(row)
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        assert after == before
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(eligibility_heads)")
        }
        assert columns == {
            "source_lineage_id",
            "current_eligibility_generation",
        }
        assert connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'eligibility_evaluation_versions'
            """
        ).fetchone() is None
    finally:
        connection.close()


def test_current_version_with_wrong_contract_is_rejected_before_ddl(tmp_path) -> None:
    path = tmp_path / "old-v2-contract.sqlite"
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE schema_metadata (
            metadata_id INTEGER PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            schema_contract TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    raw.execute(
        """
        INSERT INTO schema_metadata
        VALUES (1, ?, 'geospoiler-wiki-sqlite-v2-frozen-claim-projection', 'audit-only')
        """,
        (SCHEMA_VERSION,),
    )
    raw.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
    raw.commit()
    before = raw.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    raw.close()

    connection = connect_database(path, initialize=False)
    try:
        with pytest.raises(
            SchemaCompatibilityError,
            match="geospoiler-wiki-sqlite-v2-frozen-claim-projection",
        ):
            initialize_schema(connection)
        after = [
            tuple(row)
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        assert after == before
    finally:
        connection.close()


def test_populated_schema_passes_foreign_key_and_integrity_checks(wiki_db) -> None:
    prepare_stage(wiki_db, external_key="telegram:integrity:1")
    assert wiki_db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert wiki_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_version_rows_reject_update_and_delete(wiki_db) -> None:
    lineage = ensure_source_lineage(
        wiki_db,
        source_kind="telegram",
        external_key="telegram:immutable:1",
    )
    card = record_card_revision(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        card_payload={"summary": "immutable"},
        input_payloads={"claim_inputs": {"key_points": ["immutable"]}},
    )
    activate_processor_contract(
        wiki_db,
        stage_kind="claim_extraction",
        contract=contract("immutable"),
    )
    dependency = publish_dependency(
        wiki_db,
        dependency_kind="candidate_snapshot",
        dependency_scope_key=lineage.source_lineage_id,
        payload={"ids": []},
        expected_version_id=None,
        producer_kind="registry",
    )
    stage = schedule_stage(
        wiki_db,
        source_lineage_id=lineage.source_lineage_id,
        stage_kind="claim_extraction",
        input_kinds=["claim_inputs"],
        dependencies=[
            DependencyKey("candidate_snapshot", lineage.source_lineage_id)
        ],
    )

    immutable_targets = [
        ("card_revisions", "card_revision_id", card.card_revision_id),
        (
            "lineage_input_versions",
            "input_version_id",
            card.input_heads[0].input_version_id,
        ),
        (
            "dependency_versions",
            "dependency_version_id",
            dependency.dependency_version_id,
        ),
        ("lineage_stage_versions", "stage_version_id", stage.stage_version_id),
    ]
    for table, id_column, row_id in immutable_targets:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            wiki_db.execute(
                f"UPDATE {table} SET created_at = created_at WHERE {id_column} = ?",
                (row_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            wiki_db.execute(
                f"DELETE FROM {table} WHERE {id_column} = ?",
                (row_id,),
            )


def test_identity_alias_rejects_metonym_kind(wiki_db) -> None:
    wiki_db.execute(
        """
        INSERT INTO concepts (
            concept_id, concept_kind, approval_status, canonical_key, created_at
        ) VALUES ('concept-1', 'entity', 'approved', 'russia', 'audit')
        """
    )
    wiki_db.execute(
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
        ) VALUES ('concept-rev-1', 'concept-1', 1, 'i', 'd', 'h', '{}', 'audit')
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        wiki_db.execute(
            """
            INSERT INTO identity_aliases (
                identity_alias_id,
                concept_id,
                concept_revision_id,
                normalized_surface,
                display_surface,
                alias_kind,
                approved_at
            ) VALUES ('alias-1', 'concept-1', 'concept-rev-1', 'москва', 'Москва', 'metonym', 'audit')
            """
        )


def test_unapproved_concepts_cannot_enter_registry_or_hierarchy(wiki_db) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        wiki_db.execute(
            """
            INSERT INTO concepts (
                concept_id, concept_kind, approval_status, canonical_key, created_at
            ) VALUES ('proposal-only', 'topic', 'proposed', 'proposal-only', 'audit')
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        wiki_db.execute(
            """
            INSERT INTO approved_primary_hierarchy_edges (
                primary_hierarchy_edge_id,
                child_concept_id,
                parent_concept_id,
                approval_generation,
                action,
                approved_at
            ) VALUES ('edge-1', 'missing-child', 'missing-parent', 1, 'approve', 'audit')
            """
        )


def test_occurrence_chain_indexes_exist(wiki_db) -> None:
    indexes = {
        row["name"]
        for row in wiki_db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'index' AND tbl_name = 'occurrence_state_events'
            """
        )
    }
    assert {
        "ux_occurrence_state_successor",
        "ux_occurrence_state_root",
        "ux_extraction_run_occurrence_event",
    } <= indexes
