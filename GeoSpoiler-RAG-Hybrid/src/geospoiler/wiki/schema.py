"""SQLite schema v6 and connection management for Wiki v2."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 6
SCHEMA_CONTRACT = (
    "geospoiler-wiki-sqlite-v6-effective-relations-review-fts-analysis-cache"
)
DEFAULT_BUSY_TIMEOUT_MS = 5_000

CLAIM_EXTRACTION_STAGE_KIND = "claim_extraction"
ELIGIBILITY_EVALUATION_STAGE_KIND = "eligibility_evaluation"
CLAIM_GROUPING_STAGE_KIND = "claim_grouping"
CONCEPT_LINKING_STAGE_KIND = "concept_linking"
CARD_PROJECTION_STAGE_KIND = "card_projection"
CLAIM_PROJECTION_STAGE_KIND = "claim_projection"
HUB_PROJECTION_STAGE_KIND = "hub_projection"
PROJECTION_STAGE_KIND_BY_KIND = {
    "card": CARD_PROJECTION_STAGE_KIND,
    "claim": CLAIM_PROJECTION_STAGE_KIND,
    "hub": HUB_PROJECTION_STAGE_KIND,
}

ALLOWED_DEPENDENCY_KINDS = (
    "occurrence_snapshot",
    "approved_identity_alias_snapshot",
    "candidate_snapshot",
    "registry_snapshot",
    "surface_resolution",
    "effective_claim_groups",
    "effective_concept_links",
    "eligibility_state",
    "manual_sidecar",
    "concept_display_snapshot",
    "hierarchy_snapshot",
    "card_relation_snapshot",
    "card_projection_snapshot",
    "claim_projection_snapshot",
)


class SchemaCompatibilityError(RuntimeError):
    """Raised when an existing database is not compatible with this schema."""


class UnknownSchemaVersionError(SchemaCompatibilityError):
    """Raised when an existing database has an unsupported schema version."""


def connect_database(
    path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    initialize: bool = True,
) -> sqlite3.Connection:
    """Open and configure a Wiki SQLite connection."""
    database = str(path)
    connection = sqlite3.connect(
        database,
        timeout=max(busy_timeout_ms, 0) / 1_000,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    configure_connection(connection, database=database, busy_timeout_ms=busy_timeout_ms)
    if initialize:
        initialize_schema(connection)
    return connection


def configure_connection(
    connection: sqlite3.Connection,
    *,
    database: str | None = None,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> None:
    """Apply required connection-local SQLite settings."""
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {max(int(busy_timeout_ms), 0)}")
    connection.execute("PRAGMA synchronous = NORMAL")
    if database is not None and not _is_memory_database(database):
        try:
            connection.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.OperationalError:
            # Read-only and some virtual filesystems cannot switch journal mode.
            pass


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create schema v6 idempotently or reject an incompatible existing schema."""
    if _table_exists(connection, "schema_metadata"):
        _validate_schema_metadata(connection)

    script = (
        "BEGIN IMMEDIATE;\n"
        f"{SCHEMA_DDL}\n"
        "INSERT OR IGNORE INTO schema_metadata "
        "(metadata_id, schema_version, schema_contract, created_at) "
        f"VALUES (1, {SCHEMA_VERSION}, '{SCHEMA_CONTRACT}', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
        "COMMIT;"
    )
    try:
        connection.executescript(script)
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise

    _validate_schema_metadata(connection)


def _validate_schema_metadata(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute(
            "SELECT schema_version, schema_contract FROM schema_metadata WHERE metadata_id = 1"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise SchemaCompatibilityError("Invalid schema_metadata table") from exc
    if row is None:
        raise SchemaCompatibilityError("schema_metadata exists without the required metadata row")
    version = int(row["schema_version"])
    if version != SCHEMA_VERSION:
        raise UnknownSchemaVersionError(
            f"Unsupported Wiki schema version {version}; expected {SCHEMA_VERSION}"
        )
    if row["schema_contract"] != SCHEMA_CONTRACT:
        raise SchemaCompatibilityError(
            f"Wiki schema contract {row['schema_contract']!r} is not {SCHEMA_CONTRACT!r}"
        )


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _is_memory_database(database: str) -> bool:
    normalized = database.lower()
    return normalized == ":memory:" or (normalized.startswith("file:") and "mode=memory" in normalized)


_BASE_SCHEMA_DDL = r"""
CREATE TABLE IF NOT EXISTS schema_metadata (
    metadata_id INTEGER PRIMARY KEY CHECK (metadata_id = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    schema_contract TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_lineages (
    source_lineage_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    external_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_kind, external_key)
);

CREATE TABLE IF NOT EXISTS processor_contract_versions (
    processor_contract_version_id TEXT PRIMARY KEY,
    stage_kind TEXT NOT NULL,
    contract_hash TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (stage_kind, contract_hash),
    UNIQUE (processor_contract_version_id, stage_kind),
    UNIQUE (processor_contract_version_id, stage_kind, contract_hash)
);

CREATE TABLE IF NOT EXISTS processor_contract_activations (
    stage_kind TEXT NOT NULL,
    activation_generation INTEGER NOT NULL CHECK (activation_generation > 0),
    processor_contract_version_id TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    PRIMARY KEY (stage_kind, activation_generation),
    UNIQUE (stage_kind, activation_generation, processor_contract_version_id),
    FOREIGN KEY (processor_contract_version_id, stage_kind)
        REFERENCES processor_contract_versions (processor_contract_version_id, stage_kind)
);

CREATE TABLE IF NOT EXISTS active_processor_contract_heads (
    stage_kind TEXT PRIMARY KEY,
    current_activation_generation INTEGER NOT NULL CHECK (current_activation_generation > 0),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (stage_kind, current_activation_generation)
        REFERENCES processor_contract_activations (stage_kind, activation_generation)
);

CREATE TABLE IF NOT EXISTS lineage_stage_versions (
    stage_version_id TEXT PRIMARY KEY,
    source_lineage_id TEXT NOT NULL,
    stage_kind TEXT NOT NULL,
    stage_generation INTEGER NOT NULL CHECK (stage_generation > 0),
    stage_inputs_hash TEXT NOT NULL,
    processor_contract_activation_generation INTEGER NOT NULL,
    processor_contract_version_id TEXT NOT NULL,
    processor_contract_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_lineage_id, stage_kind, stage_generation),
    UNIQUE (stage_version_id, source_lineage_id),
    UNIQUE (stage_version_id, processor_contract_version_id),
    UNIQUE (stage_version_id, source_lineage_id, stage_kind, stage_generation),
    UNIQUE (
        stage_version_id,
        source_lineage_id,
        stage_kind,
        stage_generation,
        processor_contract_activation_generation,
        processor_contract_version_id
    ),
    FOREIGN KEY (source_lineage_id) REFERENCES source_lineages (source_lineage_id),
    FOREIGN KEY (
        stage_kind,
        processor_contract_activation_generation,
        processor_contract_version_id
    ) REFERENCES processor_contract_activations (
        stage_kind,
        activation_generation,
        processor_contract_version_id
    ),
    FOREIGN KEY (processor_contract_version_id, stage_kind)
        REFERENCES processor_contract_versions (processor_contract_version_id, stage_kind)
);

CREATE TABLE IF NOT EXISTS lineage_stage_heads (
    source_lineage_id TEXT NOT NULL,
    stage_kind TEXT NOT NULL,
    current_stage_version_id TEXT NOT NULL,
    current_stage_generation INTEGER NOT NULL CHECK (current_stage_generation > 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_lineage_id, stage_kind),
    FOREIGN KEY (
        current_stage_version_id,
        source_lineage_id,
        stage_kind,
        current_stage_generation
    ) REFERENCES lineage_stage_versions (
        stage_version_id,
        source_lineage_id,
        stage_kind,
        stage_generation
    )
);

CREATE TABLE IF NOT EXISTS card_revisions (
    card_revision_id TEXT PRIMARY KEY,
    source_lineage_id TEXT NOT NULL,
    card_content_hash TEXT NOT NULL,
    canonical_payload_json TEXT NOT NULL,
    producer_kind TEXT NOT NULL CHECK (producer_kind IN ('ingest', 'manual', 'registry', 'stage')),
    produced_by_stage_version_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source_lineage_id, card_content_hash),
    UNIQUE (card_revision_id, source_lineage_id),
    FOREIGN KEY (source_lineage_id) REFERENCES source_lineages (source_lineage_id),
    FOREIGN KEY (produced_by_stage_version_id) REFERENCES lineage_stage_versions (stage_version_id),
    CHECK (
        (producer_kind = 'stage' AND produced_by_stage_version_id IS NOT NULL)
        OR (producer_kind <> 'stage' AND produced_by_stage_version_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS source_lineage_heads (
    source_lineage_id TEXT PRIMARY KEY,
    current_card_revision_id TEXT NOT NULL,
    card_head_generation INTEGER NOT NULL CHECK (card_head_generation > 0),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (current_card_revision_id, source_lineage_id)
        REFERENCES card_revisions (card_revision_id, source_lineage_id)
);

CREATE TABLE IF NOT EXISTS lineage_input_versions (
    input_version_id TEXT PRIMARY KEY,
    source_lineage_id TEXT NOT NULL,
    input_kind TEXT NOT NULL,
    input_generation INTEGER NOT NULL CHECK (input_generation > 0),
    input_hash TEXT NOT NULL,
    canonical_payload_json TEXT NOT NULL,
    observed_card_revision_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source_lineage_id, input_kind, input_generation),
    UNIQUE (
        input_version_id,
        source_lineage_id,
        input_kind,
        input_generation,
        input_hash
    ),
    FOREIGN KEY (source_lineage_id) REFERENCES source_lineages (source_lineage_id),
    FOREIGN KEY (observed_card_revision_id, source_lineage_id)
        REFERENCES card_revisions (card_revision_id, source_lineage_id)
);

CREATE TABLE IF NOT EXISTS lineage_input_heads (
    source_lineage_id TEXT NOT NULL,
    input_kind TEXT NOT NULL,
    current_input_version_id TEXT NOT NULL,
    current_input_generation INTEGER NOT NULL CHECK (current_input_generation > 0),
    current_input_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_lineage_id, input_kind),
    FOREIGN KEY (
        current_input_version_id,
        source_lineage_id,
        input_kind,
        current_input_generation,
        current_input_hash
    ) REFERENCES lineage_input_versions (
        input_version_id,
        source_lineage_id,
        input_kind,
        input_generation,
        input_hash
    )
);

CREATE TABLE IF NOT EXISTS card_revision_input_bindings (
    card_revision_id TEXT NOT NULL,
    input_kind TEXT NOT NULL,
    source_lineage_id TEXT NOT NULL,
    input_version_id TEXT NOT NULL,
    input_generation INTEGER NOT NULL CHECK (input_generation > 0),
    input_hash TEXT NOT NULL,
    PRIMARY KEY (card_revision_id, input_kind),
    FOREIGN KEY (card_revision_id, source_lineage_id)
        REFERENCES card_revisions (card_revision_id, source_lineage_id),
    FOREIGN KEY (
        input_version_id,
        source_lineage_id,
        input_kind,
        input_generation,
        input_hash
    ) REFERENCES lineage_input_versions (
        input_version_id,
        source_lineage_id,
        input_kind,
        input_generation,
        input_hash
    )
);

CREATE TABLE IF NOT EXISTS dependency_versions (
    dependency_version_id TEXT PRIMARY KEY,
    dependency_kind TEXT NOT NULL CHECK (
        dependency_kind IN (__WIKI_DEPENDENCY_KINDS__)
    ),
    dependency_scope_key TEXT NOT NULL,
    dependency_generation INTEGER NOT NULL CHECK (dependency_generation > 0),
    dependency_hash TEXT NOT NULL,
    canonical_payload_json TEXT NOT NULL,
    producer_kind TEXT NOT NULL CHECK (producer_kind IN ('ingest', 'manual', 'registry', 'stage')),
    produced_by_stage_version_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (dependency_kind, dependency_scope_key, dependency_generation),
    UNIQUE (
        dependency_version_id,
        dependency_kind,
        dependency_scope_key,
        dependency_generation,
        dependency_hash
    ),
    FOREIGN KEY (produced_by_stage_version_id) REFERENCES lineage_stage_versions (stage_version_id),
    CHECK (
        (producer_kind = 'stage' AND produced_by_stage_version_id IS NOT NULL)
        OR (producer_kind <> 'stage' AND produced_by_stage_version_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS dependency_heads (
    dependency_kind TEXT NOT NULL CHECK (
        dependency_kind IN (__WIKI_DEPENDENCY_KINDS__)
    ),
    dependency_scope_key TEXT NOT NULL,
    current_dependency_version_id TEXT NOT NULL,
    current_generation INTEGER NOT NULL CHECK (current_generation > 0),
    current_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dependency_kind, dependency_scope_key),
    FOREIGN KEY (
        current_dependency_version_id,
        dependency_kind,
        dependency_scope_key,
        current_generation,
        current_hash
    ) REFERENCES dependency_versions (
        dependency_version_id,
        dependency_kind,
        dependency_scope_key,
        dependency_generation,
        dependency_hash
    )
);

CREATE TABLE IF NOT EXISTS lineage_stage_input_bindings (
    stage_version_id TEXT NOT NULL,
    source_lineage_id TEXT NOT NULL,
    input_kind TEXT NOT NULL,
    input_version_id TEXT NOT NULL,
    input_generation INTEGER NOT NULL CHECK (input_generation > 0),
    input_hash TEXT NOT NULL,
    PRIMARY KEY (stage_version_id, input_kind),
    FOREIGN KEY (stage_version_id, source_lineage_id)
        REFERENCES lineage_stage_versions (stage_version_id, source_lineage_id),
    FOREIGN KEY (
        input_version_id,
        source_lineage_id,
        input_kind,
        input_generation,
        input_hash
    ) REFERENCES lineage_input_versions (
        input_version_id,
        source_lineage_id,
        input_kind,
        input_generation,
        input_hash
    )
);

CREATE TABLE IF NOT EXISTS stage_dependency_bindings (
    stage_version_id TEXT NOT NULL,
    dependency_kind TEXT NOT NULL CHECK (
        dependency_kind IN (__WIKI_DEPENDENCY_KINDS__)
    ),
    dependency_scope_key TEXT NOT NULL,
    dependency_version_id TEXT NOT NULL,
    dependency_generation INTEGER NOT NULL CHECK (dependency_generation > 0),
    dependency_hash TEXT NOT NULL,
    PRIMARY KEY (stage_version_id, dependency_kind, dependency_scope_key),
    FOREIGN KEY (stage_version_id) REFERENCES lineage_stage_versions (stage_version_id),
    FOREIGN KEY (
        dependency_version_id,
        dependency_kind,
        dependency_scope_key,
        dependency_generation,
        dependency_hash
    ) REFERENCES dependency_versions (
        dependency_version_id,
        dependency_kind,
        dependency_scope_key,
        dependency_generation,
        dependency_hash
    )
);

CREATE TABLE IF NOT EXISTS stage_runs (
    stage_run_id TEXT PRIMARY KEY,
    stage_version_id TEXT NOT NULL,
    source_lineage_id TEXT NOT NULL,
    stage_kind TEXT NOT NULL,
    processor_contract_version_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'committed', 'failed', 'stale', 'no_op')),
    observed_stage_generation INTEGER NOT NULL CHECK (observed_stage_generation > 0),
    observed_contract_activation_generation INTEGER NOT NULL CHECK (observed_contract_activation_generation > 0),
    artifact_source_card_revision_id TEXT,
    applied_against_card_revision_id TEXT,
    duplicate_of_stage_run_id TEXT,
    error_text TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    commit_seq INTEGER,
    UNIQUE (stage_run_id, source_lineage_id),
    UNIQUE (
        stage_run_id,
        source_lineage_id,
        stage_kind,
        processor_contract_version_id
    ),
    FOREIGN KEY (
        stage_version_id,
        source_lineage_id,
        stage_kind,
        observed_stage_generation,
        observed_contract_activation_generation,
        processor_contract_version_id
    ) REFERENCES lineage_stage_versions (
        stage_version_id,
        source_lineage_id,
        stage_kind,
        stage_generation,
        processor_contract_activation_generation,
        processor_contract_version_id
    ),
    FOREIGN KEY (artifact_source_card_revision_id, source_lineage_id)
        REFERENCES card_revisions (card_revision_id, source_lineage_id),
    FOREIGN KEY (applied_against_card_revision_id, source_lineage_id)
        REFERENCES card_revisions (card_revision_id, source_lineage_id),
    FOREIGN KEY (duplicate_of_stage_run_id) REFERENCES stage_runs (stage_run_id),
    CHECK (
        (status = 'committed' AND commit_seq IS NOT NULL AND finished_at IS NOT NULL)
        OR (status = 'started' AND commit_seq IS NULL AND finished_at IS NULL)
        OR (status IN ('failed', 'stale', 'no_op') AND commit_seq IS NULL AND finished_at IS NOT NULL)
    ),
    CHECK (
        (status = 'no_op' AND duplicate_of_stage_run_id IS NOT NULL)
        OR (status <> 'no_op' AND duplicate_of_stage_run_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_committed_stage_run_key
ON stage_runs (idempotency_key)
WHERE status = 'committed';

CREATE UNIQUE INDEX IF NOT EXISTS ux_committed_stage_run_seq
ON stage_runs (commit_seq)
WHERE status = 'committed';

CREATE TABLE IF NOT EXISTS outbox_events (
    outbox_event_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    stage_run_id TEXT NOT NULL,
    commit_seq INTEGER NOT NULL CHECK (commit_seq > 0),
    event_kind TEXT NOT NULL,
    aggregate_kind TEXT NOT NULL,
    aggregate_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    processed_at TEXT,
    FOREIGN KEY (stage_run_id) REFERENCES stage_runs (stage_run_id)
);

CREATE INDEX IF NOT EXISTS ix_outbox_pending
ON outbox_events (commit_seq, outbox_event_id)
WHERE processed_at IS NULL;

CREATE TRIGGER IF NOT EXISTS wiki_outbox_requires_committed_run
BEFORE INSERT ON outbox_events
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM stage_runs
    WHERE stage_run_id = NEW.stage_run_id
      AND status = 'committed'
      AND commit_seq = NEW.commit_seq
)
BEGIN
    SELECT RAISE(ABORT, 'outbox events require a matching committed stage run');
END;

CREATE TRIGGER IF NOT EXISTS wiki_stage_runs_no_delete
BEFORE DELETE ON stage_runs
BEGIN
    SELECT RAISE(ABORT, 'stage_runs rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS wiki_stage_runs_valid_transition
BEFORE UPDATE ON stage_runs
WHEN
    OLD.status <> 'started'
    OR NEW.status NOT IN ('committed', 'failed', 'stale', 'no_op')
    OR NEW.stage_run_id <> OLD.stage_run_id
    OR NEW.stage_version_id <> OLD.stage_version_id
    OR NEW.source_lineage_id <> OLD.source_lineage_id
    OR NEW.stage_kind <> OLD.stage_kind
    OR NEW.processor_contract_version_id <> OLD.processor_contract_version_id
    OR NEW.idempotency_key <> OLD.idempotency_key
    OR NEW.observed_stage_generation <> OLD.observed_stage_generation
    OR NEW.observed_contract_activation_generation
       <> OLD.observed_contract_activation_generation
    OR NEW.artifact_source_card_revision_id IS NOT OLD.artifact_source_card_revision_id
    OR NEW.started_at <> OLD.started_at
BEGIN
    SELECT RAISE(ABORT, 'invalid stage run lifecycle transition');
END;

CREATE TRIGGER IF NOT EXISTS wiki_outbox_no_delete
BEFORE DELETE ON outbox_events
BEGIN
    SELECT RAISE(ABORT, 'outbox_events rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS wiki_outbox_processed_only_update
BEFORE UPDATE ON outbox_events
WHEN
    OLD.processed_at IS NOT NULL
    OR NEW.processed_at IS NULL
    OR NEW.outbox_event_id <> OLD.outbox_event_id
    OR NEW.event_key <> OLD.event_key
    OR NEW.stage_run_id <> OLD.stage_run_id
    OR NEW.commit_seq <> OLD.commit_seq
    OR NEW.event_kind <> OLD.event_kind
    OR NEW.aggregate_kind <> OLD.aggregate_kind
    OR NEW.aggregate_key <> OLD.aggregate_key
    OR NEW.payload_json <> OLD.payload_json
    OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'only first outbox processed_at transition is mutable');
END;

CREATE TABLE IF NOT EXISTS extraction_artifacts (
    extraction_artifact_id TEXT PRIMARY KEY,
    extraction_artifact_key TEXT NOT NULL UNIQUE,
    processor_contract_version_id TEXT NOT NULL,
    claim_inputs_hash TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        extraction_artifact_id,
        processor_contract_version_id,
        claim_inputs_hash
    ),
    FOREIGN KEY (processor_contract_version_id)
        REFERENCES processor_contract_versions (processor_contract_version_id)
);

CREATE TRIGGER IF NOT EXISTS wiki_extraction_artifact_contract_kind
BEFORE INSERT ON extraction_artifacts
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM processor_contract_versions
    WHERE processor_contract_version_id = NEW.processor_contract_version_id
      AND stage_kind = '__WIKI_CLAIM_EXTRACTION_STAGE_KIND__'
)
BEGIN
    SELECT RAISE(ABORT, 'extraction artifacts require a claim_extraction contract');
END;

CREATE TABLE IF NOT EXISTS extraction_artifact_items (
    extraction_artifact_item_id TEXT PRIMARY KEY,
    extraction_artifact_id TEXT NOT NULL,
    item_ordinal INTEGER NOT NULL CHECK (item_ordinal >= 0),
    field_kind TEXT NOT NULL,
    exact_payload_json TEXT NOT NULL,
    exact_payload_hash TEXT NOT NULL,
    occurrence_fingerprint TEXT NOT NULL,
    locator_json TEXT NOT NULL,
    evidence_metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (extraction_artifact_id, item_ordinal),
    FOREIGN KEY (extraction_artifact_id)
        REFERENCES extraction_artifacts (extraction_artifact_id)
);

CREATE TABLE IF NOT EXISTS extraction_runs (
    extraction_run_id TEXT PRIMARY KEY,
    stage_run_id TEXT NOT NULL UNIQUE,
    extraction_artifact_id TEXT NOT NULL,
    source_lineage_id TEXT NOT NULL,
    stage_kind TEXT NOT NULL CHECK (
        stage_kind = '__WIKI_CLAIM_EXTRACTION_STAGE_KIND__'
    ),
    processor_contract_version_id TEXT NOT NULL,
    claim_inputs_hash TEXT NOT NULL,
    artifact_source_card_revision_id TEXT NOT NULL,
    applied_against_card_revision_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (extraction_run_id, source_lineage_id),
    UNIQUE (extraction_run_id, source_lineage_id, claim_inputs_hash),
    FOREIGN KEY (
        stage_run_id,
        source_lineage_id,
        stage_kind,
        processor_contract_version_id
    ) REFERENCES stage_runs (
        stage_run_id,
        source_lineage_id,
        stage_kind,
        processor_contract_version_id
    ),
    FOREIGN KEY (
        extraction_artifact_id,
        processor_contract_version_id,
        claim_inputs_hash
    ) REFERENCES extraction_artifacts (
        extraction_artifact_id,
        processor_contract_version_id,
        claim_inputs_hash
    ),
    FOREIGN KEY (source_lineage_id) REFERENCES source_lineages (source_lineage_id),
    FOREIGN KEY (artifact_source_card_revision_id, source_lineage_id)
        REFERENCES card_revisions (card_revision_id, source_lineage_id),
    FOREIGN KEY (applied_against_card_revision_id, source_lineage_id)
        REFERENCES card_revisions (card_revision_id, source_lineage_id)
);

CREATE TRIGGER IF NOT EXISTS wiki_extraction_run_artifact_compatibility
BEFORE INSERT ON extraction_runs
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM stage_runs AS run
    JOIN lineage_stage_input_bindings AS stage_input
      ON stage_input.stage_version_id = run.stage_version_id
     AND stage_input.source_lineage_id = run.source_lineage_id
     AND stage_input.input_kind = 'claim_inputs'
    JOIN card_revision_input_bindings AS card_input
      ON card_input.card_revision_id = NEW.artifact_source_card_revision_id
     AND card_input.source_lineage_id = NEW.source_lineage_id
     AND card_input.input_kind = 'claim_inputs'
    WHERE run.stage_run_id = NEW.stage_run_id
      AND run.source_lineage_id = NEW.source_lineage_id
      AND run.stage_kind = '__WIKI_CLAIM_EXTRACTION_STAGE_KIND__'
      AND NEW.stage_kind = run.stage_kind
      AND run.processor_contract_version_id = NEW.processor_contract_version_id
      AND stage_input.input_hash = NEW.claim_inputs_hash
      AND card_input.input_hash = NEW.claim_inputs_hash
      AND (
          run.artifact_source_card_revision_id IS NULL
          OR run.artifact_source_card_revision_id = NEW.artifact_source_card_revision_id
      )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'extraction apply requires matching claim_extraction stage, contract, and claim inputs'
    );
END;

CREATE TABLE IF NOT EXISTS claim_occurrences (
    occurrence_version_id TEXT PRIMARY KEY,
    source_lineage_id TEXT NOT NULL,
    card_revision_id TEXT NOT NULL,
    extraction_run_id TEXT NOT NULL,
    field_kind TEXT NOT NULL,
    stable_locator_json TEXT NOT NULL,
    exact_occurrence_payload_json TEXT NOT NULL,
    exact_payload_hash TEXT NOT NULL,
    occurrence_fingerprint TEXT NOT NULL,
    occurrence_schema_version TEXT NOT NULL,
    extracted_from_claim_inputs_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (occurrence_version_id, source_lineage_id),
    FOREIGN KEY (source_lineage_id) REFERENCES source_lineages (source_lineage_id),
    FOREIGN KEY (card_revision_id, source_lineage_id)
        REFERENCES card_revisions (card_revision_id, source_lineage_id),
    FOREIGN KEY (
        extraction_run_id,
        source_lineage_id,
        extracted_from_claim_inputs_hash
    ) REFERENCES extraction_runs (
        extraction_run_id,
        source_lineage_id,
        claim_inputs_hash
    )
);

CREATE TABLE IF NOT EXISTS extraction_run_occurrences (
    extraction_run_id TEXT NOT NULL,
    occurrence_version_id TEXT NOT NULL,
    source_lineage_id TEXT NOT NULL,
    manifest_ordinal INTEGER NOT NULL CHECK (manifest_ordinal >= 0),
    PRIMARY KEY (extraction_run_id, occurrence_version_id),
    UNIQUE (extraction_run_id, manifest_ordinal),
    FOREIGN KEY (extraction_run_id, source_lineage_id)
        REFERENCES extraction_runs (extraction_run_id, source_lineage_id),
    FOREIGN KEY (occurrence_version_id, source_lineage_id)
        REFERENCES claim_occurrences (occurrence_version_id, source_lineage_id)
);

CREATE TABLE IF NOT EXISTS occurrence_state_events (
    state_event_id TEXT PRIMARY KEY,
    occurrence_version_id TEXT NOT NULL,
    extraction_run_id TEXT NOT NULL,
    source_lineage_id TEXT NOT NULL,
    previous_state_event_id TEXT,
    to_status TEXT NOT NULL CHECK (to_status IN ('active', 'superseded', 'retired')),
    superseded_by_occurrence_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (state_event_id, occurrence_version_id, source_lineage_id),
    FOREIGN KEY (occurrence_version_id, source_lineage_id)
        REFERENCES claim_occurrences (occurrence_version_id, source_lineage_id),
    FOREIGN KEY (extraction_run_id, source_lineage_id)
        REFERENCES extraction_runs (extraction_run_id, source_lineage_id),
    FOREIGN KEY (superseded_by_occurrence_id, source_lineage_id)
        REFERENCES claim_occurrences (occurrence_version_id, source_lineage_id),
    FOREIGN KEY (
        previous_state_event_id,
        occurrence_version_id,
        source_lineage_id
    ) REFERENCES occurrence_state_events (
        state_event_id,
        occurrence_version_id,
        source_lineage_id
    ),
    CHECK (
        (to_status = 'superseded' AND superseded_by_occurrence_id IS NOT NULL)
        OR (to_status <> 'superseded' AND superseded_by_occurrence_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_occurrence_state_successor
ON occurrence_state_events (previous_state_event_id)
WHERE previous_state_event_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_occurrence_state_root
ON occurrence_state_events (occurrence_version_id)
WHERE previous_state_event_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_extraction_run_occurrence_event
ON occurrence_state_events (extraction_run_id, occurrence_version_id);

CREATE TRIGGER IF NOT EXISTS wiki_occurrence_events_require_committed_run
BEFORE INSERT ON occurrence_state_events
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM extraction_runs AS extraction_run
    JOIN stage_runs AS stage_run
      ON stage_run.stage_run_id = extraction_run.stage_run_id
     AND stage_run.source_lineage_id = extraction_run.source_lineage_id
    WHERE extraction_run.extraction_run_id = NEW.extraction_run_id
      AND extraction_run.source_lineage_id = NEW.source_lineage_id
      AND stage_run.status = 'committed'
      AND stage_run.commit_seq IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'occurrence state events require a committed extraction run');
END;

CREATE VIEW IF NOT EXISTS occurrence_current_states AS
WITH committed_events AS (
    SELECT
        event.*,
        stage_run.commit_seq,
        ROW_NUMBER() OVER (
            PARTITION BY event.occurrence_version_id
            ORDER BY stage_run.commit_seq DESC
        ) AS row_number
    FROM occurrence_state_events AS event
    JOIN extraction_runs AS extraction_run
      ON extraction_run.extraction_run_id = event.extraction_run_id
     AND extraction_run.source_lineage_id = event.source_lineage_id
    JOIN stage_runs AS stage_run
      ON stage_run.stage_run_id = extraction_run.stage_run_id
     AND stage_run.source_lineage_id = extraction_run.source_lineage_id
    WHERE stage_run.status = 'committed'
)
SELECT
    state_event_id,
    occurrence_version_id,
    extraction_run_id,
    source_lineage_id,
    previous_state_event_id,
    to_status AS status,
    superseded_by_occurrence_id,
    commit_seq
FROM committed_events
WHERE row_number = 1;

CREATE TABLE IF NOT EXISTS eligibility_evaluation_versions (
    eligibility_evaluation_id TEXT PRIMARY KEY,
    source_lineage_id TEXT NOT NULL,
    evaluated_card_revision_id TEXT NOT NULL,
    eligibility_generation INTEGER NOT NULL CHECK (eligibility_generation > 0),
    eligibility_input_kind TEXT NOT NULL DEFAULT 'eligibility_inputs' CHECK (
        eligibility_input_kind = 'eligibility_inputs'
    ),
    eligibility_input_version_id TEXT NOT NULL,
    eligibility_input_generation INTEGER NOT NULL CHECK (
        eligibility_input_generation > 0
    ),
    eligibility_inputs_hash TEXT NOT NULL,
    stage_kind TEXT NOT NULL CHECK (
        stage_kind = '__WIKI_ELIGIBILITY_EVALUATION_STAGE_KIND__'
    ),
    processor_contract_activation_generation INTEGER NOT NULL CHECK (
        processor_contract_activation_generation > 0
    ),
    processor_contract_version_id TEXT NOT NULL,
    processor_contract_hash TEXT NOT NULL,
    eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
    reasons_json TEXT NOT NULL,
    evaluation_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_lineage_id, eligibility_generation),
    UNIQUE (
        eligibility_evaluation_id,
        source_lineage_id,
        eligibility_generation,
        evaluated_card_revision_id,
        eligibility_input_version_id,
        eligibility_input_generation,
        eligibility_inputs_hash,
        stage_kind,
        processor_contract_activation_generation,
        processor_contract_version_id,
        processor_contract_hash,
        eligible
    ),
    FOREIGN KEY (source_lineage_id) REFERENCES source_lineages (source_lineage_id),
    FOREIGN KEY (evaluated_card_revision_id, source_lineage_id)
        REFERENCES card_revisions (card_revision_id, source_lineage_id),
    FOREIGN KEY (
        eligibility_input_version_id,
        source_lineage_id,
        eligibility_input_kind,
        eligibility_input_generation,
        eligibility_inputs_hash
    ) REFERENCES lineage_input_versions (
        input_version_id,
        source_lineage_id,
        input_kind,
        input_generation,
        input_hash
    ),
    FOREIGN KEY (
        stage_kind,
        processor_contract_activation_generation,
        processor_contract_version_id
    ) REFERENCES processor_contract_activations (
        stage_kind,
        activation_generation,
        processor_contract_version_id
    ),
    FOREIGN KEY (
        processor_contract_version_id,
        stage_kind,
        processor_contract_hash
    ) REFERENCES processor_contract_versions (
        processor_contract_version_id,
        stage_kind,
        contract_hash
    )
);

CREATE TRIGGER IF NOT EXISTS wiki_eligibility_requires_card_input_binding
BEFORE INSERT ON eligibility_evaluation_versions
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM card_revision_input_bindings AS binding
    WHERE binding.card_revision_id = NEW.evaluated_card_revision_id
      AND binding.source_lineage_id = NEW.source_lineage_id
      AND binding.input_kind = NEW.eligibility_input_kind
      AND binding.input_hash = NEW.eligibility_inputs_hash
)
BEGIN
    SELECT RAISE(ABORT, 'eligibility evaluation requires the exact card eligibility_inputs binding');
END;

CREATE TRIGGER IF NOT EXISTS wiki_eligibility_evaluation_requires_current_snapshot
BEFORE INSERT ON eligibility_evaluation_versions
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM source_lineage_heads AS card_head
    JOIN lineage_input_heads AS input_head
      ON input_head.source_lineage_id = card_head.source_lineage_id
     AND input_head.input_kind = NEW.eligibility_input_kind
    JOIN active_processor_contract_heads AS contract_head
      ON contract_head.stage_kind = NEW.stage_kind
    WHERE card_head.source_lineage_id = NEW.source_lineage_id
      AND card_head.current_card_revision_id =
          NEW.evaluated_card_revision_id
      AND input_head.current_input_version_id =
          NEW.eligibility_input_version_id
      AND input_head.current_input_generation =
          NEW.eligibility_input_generation
      AND input_head.current_input_hash = NEW.eligibility_inputs_hash
      AND contract_head.current_activation_generation =
          NEW.processor_contract_activation_generation
      AND NEW.eligibility_generation = COALESCE(
          (
              SELECT current_eligibility_generation + 1
              FROM eligibility_heads
              WHERE source_lineage_id = NEW.source_lineage_id
          ),
          1
      )
)
BEGIN
    SELECT RAISE(ABORT, 'eligibility evaluation requires the current CAS snapshot');
END;

CREATE TABLE IF NOT EXISTS eligibility_heads (
    source_lineage_id TEXT PRIMARY KEY,
    current_eligibility_evaluation_id TEXT NOT NULL,
    current_eligibility_generation INTEGER NOT NULL CHECK (
        current_eligibility_generation > 0
    ),
    evaluated_card_revision_id TEXT NOT NULL,
    current_eligibility_input_version_id TEXT NOT NULL,
    current_eligibility_input_generation INTEGER NOT NULL CHECK (
        current_eligibility_input_generation > 0
    ),
    current_eligibility_inputs_hash TEXT NOT NULL,
    stage_kind TEXT NOT NULL CHECK (
        stage_kind = '__WIKI_ELIGIBILITY_EVALUATION_STAGE_KIND__'
    ),
    current_processor_contract_activation_generation INTEGER NOT NULL CHECK (
        current_processor_contract_activation_generation > 0
    ),
    current_processor_contract_version_id TEXT NOT NULL,
    current_processor_contract_hash TEXT NOT NULL,
    current_eligible INTEGER NOT NULL CHECK (current_eligible IN (0, 1)),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (
        current_eligibility_evaluation_id,
        source_lineage_id,
        current_eligibility_generation,
        evaluated_card_revision_id,
        current_eligibility_input_version_id,
        current_eligibility_input_generation,
        current_eligibility_inputs_hash,
        stage_kind,
        current_processor_contract_activation_generation,
        current_processor_contract_version_id,
        current_processor_contract_hash,
        current_eligible
    ) REFERENCES eligibility_evaluation_versions (
        eligibility_evaluation_id,
        source_lineage_id,
        eligibility_generation,
        evaluated_card_revision_id,
        eligibility_input_version_id,
        eligibility_input_generation,
        eligibility_inputs_hash,
        stage_kind,
        processor_contract_activation_generation,
        processor_contract_version_id,
        processor_contract_hash,
        eligible
    )
);

CREATE TRIGGER IF NOT EXISTS wiki_eligibility_head_requires_current_snapshot_insert
BEFORE INSERT ON eligibility_heads
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM source_lineage_heads AS card_head
    JOIN lineage_input_heads AS input_head
      ON input_head.source_lineage_id = card_head.source_lineage_id
     AND input_head.input_kind = 'eligibility_inputs'
    JOIN active_processor_contract_heads AS contract_head
      ON contract_head.stage_kind = NEW.stage_kind
    WHERE card_head.source_lineage_id = NEW.source_lineage_id
      AND card_head.current_card_revision_id = NEW.evaluated_card_revision_id
      AND input_head.current_input_version_id =
          NEW.current_eligibility_input_version_id
      AND input_head.current_input_generation =
          NEW.current_eligibility_input_generation
      AND input_head.current_input_hash = NEW.current_eligibility_inputs_hash
      AND contract_head.current_activation_generation =
          NEW.current_processor_contract_activation_generation
)
BEGIN
    SELECT RAISE(ABORT, 'eligibility head requires the current card, input, and contract snapshot');
END;

CREATE TRIGGER IF NOT EXISTS wiki_eligibility_head_requires_current_snapshot_update
BEFORE UPDATE ON eligibility_heads
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM source_lineage_heads AS card_head
    JOIN lineage_input_heads AS input_head
      ON input_head.source_lineage_id = card_head.source_lineage_id
     AND input_head.input_kind = 'eligibility_inputs'
    JOIN active_processor_contract_heads AS contract_head
      ON contract_head.stage_kind = NEW.stage_kind
    WHERE card_head.source_lineage_id = NEW.source_lineage_id
      AND card_head.current_card_revision_id = NEW.evaluated_card_revision_id
      AND input_head.current_input_version_id =
          NEW.current_eligibility_input_version_id
      AND input_head.current_input_generation =
          NEW.current_eligibility_input_generation
      AND input_head.current_input_hash = NEW.current_eligibility_inputs_hash
      AND contract_head.current_activation_generation =
          NEW.current_processor_contract_activation_generation
)
BEGIN
    SELECT RAISE(ABORT, 'eligibility head requires the current card, input, and contract snapshot');
END;

CREATE VIEW IF NOT EXISTS lifecycle_active_occurrences AS
SELECT
    occurrence.occurrence_version_id,
    occurrence.source_lineage_id,
    occurrence.card_revision_id,
    occurrence.extraction_run_id,
    occurrence.field_kind,
    occurrence.stable_locator_json,
    occurrence.exact_occurrence_payload_json,
    occurrence.exact_payload_hash,
    occurrence.occurrence_fingerprint,
    occurrence.occurrence_schema_version,
    occurrence.extracted_from_claim_inputs_hash,
    state.state_event_id,
    state.commit_seq
FROM claim_occurrences AS occurrence
JOIN occurrence_current_states AS state
  ON state.occurrence_version_id = occurrence.occurrence_version_id
 AND state.source_lineage_id = occurrence.source_lineage_id
WHERE state.status = 'active';

CREATE VIEW IF NOT EXISTS effective_active_occurrences AS
SELECT
    occurrence.occurrence_version_id,
    occurrence.source_lineage_id,
    occurrence.card_revision_id,
    occurrence.extraction_run_id,
    occurrence.field_kind,
    occurrence.stable_locator_json,
    occurrence.exact_occurrence_payload_json,
    occurrence.exact_payload_hash,
    occurrence.occurrence_fingerprint,
    occurrence.occurrence_schema_version,
    occurrence.extracted_from_claim_inputs_hash,
    state.state_event_id,
    state.commit_seq,
    eligibility.current_eligibility_evaluation_id AS eligibility_evaluation_id,
    eligibility.current_eligibility_generation AS eligibility_generation,
    eligibility.evaluated_card_revision_id AS eligibility_card_revision_id,
    eligibility.current_processor_contract_version_id AS
        eligibility_processor_contract_version_id
FROM claim_occurrences AS occurrence
JOIN occurrence_current_states AS state
  ON state.occurrence_version_id = occurrence.occurrence_version_id
 AND state.source_lineage_id = occurrence.source_lineage_id
JOIN eligibility_heads AS eligibility
  ON eligibility.source_lineage_id = occurrence.source_lineage_id
JOIN source_lineage_heads AS card_head
  ON card_head.source_lineage_id = eligibility.source_lineage_id
 AND card_head.current_card_revision_id = eligibility.evaluated_card_revision_id
JOIN lineage_input_heads AS input_head
  ON input_head.source_lineage_id = eligibility.source_lineage_id
 AND input_head.input_kind = 'eligibility_inputs'
 AND input_head.current_input_version_id =
     eligibility.current_eligibility_input_version_id
 AND input_head.current_input_generation =
     eligibility.current_eligibility_input_generation
 AND input_head.current_input_hash = eligibility.current_eligibility_inputs_hash
JOIN active_processor_contract_heads AS contract_head
  ON contract_head.stage_kind = eligibility.stage_kind
 AND contract_head.current_activation_generation =
     eligibility.current_processor_contract_activation_generation
WHERE state.status = 'active'
  AND eligibility.current_eligible = 1;

CREATE TABLE IF NOT EXISTS concepts (
    concept_id TEXT PRIMARY KEY,
    concept_kind TEXT NOT NULL CHECK (concept_kind IN ('entity', 'topic')),
    approval_status TEXT NOT NULL DEFAULT 'approved' CHECK (approval_status = 'approved'),
    canonical_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concept_revisions (
    concept_revision_id TEXT PRIMARY KEY,
    concept_id TEXT NOT NULL,
    concept_generation INTEGER NOT NULL CHECK (concept_generation > 0),
    identity_hash TEXT NOT NULL,
    display_hash TEXT NOT NULL,
    hierarchy_hash TEXT NOT NULL,
    canonical_payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (concept_id, concept_generation),
    UNIQUE (concept_revision_id, concept_id),
    UNIQUE (concept_revision_id, concept_id, concept_generation),
    FOREIGN KEY (concept_id) REFERENCES concepts (concept_id)
);

CREATE TABLE IF NOT EXISTS concept_heads (
    concept_id TEXT PRIMARY KEY,
    current_concept_revision_id TEXT NOT NULL,
    current_concept_generation INTEGER NOT NULL CHECK (current_concept_generation > 0),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (
        current_concept_revision_id,
        concept_id,
        current_concept_generation
    ) REFERENCES concept_revisions (
        concept_revision_id,
        concept_id,
        concept_generation
    )
);

CREATE VIEW IF NOT EXISTS approved_concepts AS
SELECT
    concept.concept_id,
    concept.concept_kind,
    concept.canonical_key,
    head.current_concept_revision_id,
    head.current_concept_generation
FROM concepts AS concept
JOIN concept_heads AS head ON head.concept_id = concept.concept_id
WHERE concept.approval_status = 'approved';

CREATE TABLE IF NOT EXISTS identity_aliases (
    identity_alias_id TEXT PRIMARY KEY,
    concept_id TEXT NOT NULL,
    concept_revision_id TEXT NOT NULL,
    normalized_surface TEXT NOT NULL,
    display_surface TEXT NOT NULL,
    alias_kind TEXT NOT NULL CHECK (
        alias_kind IN ('canonical', 'technical', 'abbreviation', 'translation', 'spelling')
    ),
    approved_at TEXT NOT NULL,
    UNIQUE (concept_id, normalized_surface, alias_kind),
    FOREIGN KEY (concept_id) REFERENCES concepts (concept_id),
    FOREIGN KEY (concept_revision_id, concept_id)
        REFERENCES concept_revisions (concept_revision_id, concept_id)
);

CREATE TABLE IF NOT EXISTS surface_revisions (
    surface_revision_id TEXT PRIMARY KEY,
    normalized_surface TEXT NOT NULL,
    surface_generation INTEGER NOT NULL CHECK (surface_generation > 0),
    surface_resolution_hash TEXT NOT NULL,
    candidate_concept_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (normalized_surface, surface_generation),
    UNIQUE (surface_revision_id, normalized_surface, surface_generation)
);

CREATE TABLE IF NOT EXISTS surface_heads (
    normalized_surface TEXT PRIMARY KEY,
    current_surface_revision_id TEXT NOT NULL,
    current_surface_generation INTEGER NOT NULL CHECK (current_surface_generation > 0),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (
        current_surface_revision_id,
        normalized_surface,
        current_surface_generation
    ) REFERENCES surface_revisions (
        surface_revision_id,
        normalized_surface,
        surface_generation
    )
);

CREATE TABLE IF NOT EXISTS concept_proposals (
    concept_proposal_id TEXT PRIMARY KEY,
    proposal_kind TEXT NOT NULL CHECK (proposal_kind IN ('entity', 'topic', 'alias', 'split', 'merge')),
    normalized_candidate_key TEXT NOT NULL,
    proposal_payload_json TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    produced_by_stage_version_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (produced_by_stage_version_id)
        REFERENCES lineage_stage_versions (stage_version_id)
);

CREATE TABLE IF NOT EXISTS concept_proposal_evidence (
    concept_proposal_evidence_id TEXT PRIMARY KEY,
    concept_proposal_id TEXT NOT NULL,
    source_lineage_id TEXT,
    card_revision_id TEXT,
    occurrence_version_id TEXT,
    evidence_payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (concept_proposal_id) REFERENCES concept_proposals (concept_proposal_id),
    FOREIGN KEY (source_lineage_id) REFERENCES source_lineages (source_lineage_id),
    FOREIGN KEY (card_revision_id) REFERENCES card_revisions (card_revision_id),
    FOREIGN KEY (occurrence_version_id) REFERENCES claim_occurrences (occurrence_version_id)
);

CREATE TABLE IF NOT EXISTS concept_review_decisions (
    concept_review_decision_id TEXT PRIMARY KEY,
    concept_proposal_id TEXT NOT NULL,
    decision_generation INTEGER NOT NULL CHECK (decision_generation > 0),
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'deferred', 'reopened')),
    created_concept_id TEXT,
    rationale TEXT,
    decided_at TEXT NOT NULL,
    UNIQUE (concept_proposal_id, decision_generation),
    FOREIGN KEY (concept_proposal_id) REFERENCES concept_proposals (concept_proposal_id),
    FOREIGN KEY (created_concept_id) REFERENCES concepts (concept_id),
    CHECK (
        (decision = 'approved' AND created_concept_id IS NOT NULL)
        OR (decision <> 'approved' AND created_concept_id IS NULL)
    )
);

CREATE VIEW IF NOT EXISTS concept_proposal_current_decisions AS
WITH ranked AS (
    SELECT
        decision.*,
        ROW_NUMBER() OVER (
            PARTITION BY concept_proposal_id
            ORDER BY decision_generation DESC
        ) AS row_number
    FROM concept_review_decisions AS decision
)
SELECT *
FROM ranked
WHERE row_number = 1;

CREATE TABLE IF NOT EXISTS claim_groups (
    claim_group_id TEXT PRIMARY KEY,
    canonical_claim_hash TEXT NOT NULL,
    canonical_claim_json TEXT NOT NULL,
    created_by_stage_version_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (created_by_stage_version_id)
        REFERENCES lineage_stage_versions (stage_version_id)
);

CREATE TABLE IF NOT EXISTS automatic_group_memberships (
    automatic_group_membership_id TEXT PRIMARY KEY,
    occurrence_version_id TEXT NOT NULL,
    claim_group_id TEXT NOT NULL,
    automatic_generation INTEGER NOT NULL CHECK (automatic_generation > 0),
    produced_by_stage_version_id TEXT NOT NULL,
    rule_inputs_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (occurrence_version_id, automatic_generation),
    FOREIGN KEY (occurrence_version_id) REFERENCES claim_occurrences (occurrence_version_id),
    FOREIGN KEY (claim_group_id) REFERENCES claim_groups (claim_group_id),
    FOREIGN KEY (produced_by_stage_version_id)
        REFERENCES lineage_stage_versions (stage_version_id)
);

CREATE TABLE IF NOT EXISTS claim_group_overrides (
    claim_group_override_id TEXT PRIMARY KEY,
    occurrence_version_id TEXT NOT NULL,
    decision_generation INTEGER NOT NULL CHECK (decision_generation > 0),
    action TEXT NOT NULL CHECK (action IN ('assign', 'clear')),
    claim_group_id TEXT,
    occurrence_fingerprint TEXT NOT NULL,
    override_status TEXT NOT NULL CHECK (override_status IN ('active', 'stale')),
    rationale TEXT,
    decided_at TEXT NOT NULL,
    UNIQUE (occurrence_version_id, decision_generation),
    FOREIGN KEY (occurrence_version_id) REFERENCES claim_occurrences (occurrence_version_id),
    FOREIGN KEY (claim_group_id) REFERENCES claim_groups (claim_group_id),
    CHECK (
        (action = 'assign' AND claim_group_id IS NOT NULL)
        OR (action = 'clear' AND claim_group_id IS NULL)
    )
);

CREATE VIEW IF NOT EXISTS effective_claim_group_memberships AS
WITH latest_override AS (
    SELECT *
    FROM (
        SELECT
            override.*,
            ROW_NUMBER() OVER (
                PARTITION BY occurrence_version_id
                ORDER BY decision_generation DESC
            ) AS row_number
        FROM claim_group_overrides AS override
    )
    WHERE row_number = 1
),
latest_automatic AS (
    SELECT *
    FROM (
        SELECT
            automatic.*,
            ROW_NUMBER() OVER (
                PARTITION BY occurrence_version_id
                ORDER BY automatic_generation DESC
            ) AS row_number
        FROM automatic_group_memberships AS automatic
    )
    WHERE row_number = 1
)
SELECT
    occurrence_version_id,
    claim_group_id,
    'override' AS membership_source
FROM latest_override
WHERE override_status = 'active' AND action = 'assign'
UNION ALL
SELECT
    automatic.occurrence_version_id,
    automatic.claim_group_id,
    'automatic' AS membership_source
FROM latest_automatic AS automatic
WHERE NOT EXISTS (
    SELECT 1
    FROM latest_override AS override
    WHERE override.occurrence_version_id = automatic.occurrence_version_id
      AND override.override_status = 'active'
);

CREATE TABLE IF NOT EXISTS occurrence_concept_automatic_links (
    automatic_link_id TEXT PRIMARY KEY,
    occurrence_version_id TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    automatic_generation INTEGER NOT NULL CHECK (automatic_generation > 0),
    link_status TEXT NOT NULL DEFAULT 'active' CHECK (
        link_status IN ('active', 'absent')
    ),
    relation_role TEXT NOT NULL CHECK (
        relation_role IN ('subject', 'actor', 'object', 'context', 'mentioned', 'unknown')
    ),
    source_locator_json TEXT NOT NULL,
    resolver_version TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    rule_inputs_json TEXT NOT NULL,
    rule_inputs_hash TEXT NOT NULL,
    explanation TEXT NOT NULL,
    confidence REAL,
    produced_by_stage_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (occurrence_version_id, concept_id, automatic_generation),
    FOREIGN KEY (occurrence_version_id) REFERENCES claim_occurrences (occurrence_version_id),
    FOREIGN KEY (concept_id) REFERENCES concepts (concept_id),
    FOREIGN KEY (produced_by_stage_version_id)
        REFERENCES lineage_stage_versions (stage_version_id)
);

CREATE TABLE IF NOT EXISTS occurrence_concept_link_overrides (
    concept_link_override_id TEXT PRIMARY KEY,
    occurrence_version_id TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    decision_generation INTEGER NOT NULL CHECK (decision_generation > 0),
    action TEXT NOT NULL CHECK (action IN ('include', 'exclude')),
    relation_role TEXT CHECK (
        relation_role IN ('subject', 'actor', 'object', 'context', 'mentioned', 'unknown')
    ),
    occurrence_fingerprint TEXT NOT NULL,
    override_status TEXT NOT NULL CHECK (override_status IN ('active', 'stale')),
    rationale TEXT,
    decided_at TEXT NOT NULL,
    UNIQUE (occurrence_version_id, concept_id, decision_generation),
    FOREIGN KEY (occurrence_version_id) REFERENCES claim_occurrences (occurrence_version_id),
    FOREIGN KEY (concept_id) REFERENCES concepts (concept_id),
    CHECK (
        (action = 'include' AND relation_role IS NOT NULL)
        OR (action = 'exclude' AND relation_role IS NULL)
    )
);

CREATE VIEW IF NOT EXISTS effective_occurrence_concept_links AS
WITH latest_override AS (
    SELECT *
    FROM (
        SELECT
            override.*,
            ROW_NUMBER() OVER (
                PARTITION BY occurrence_version_id, concept_id
                ORDER BY decision_generation DESC
            ) AS row_number
        FROM occurrence_concept_link_overrides AS override
    )
    WHERE row_number = 1
),
latest_automatic AS (
    SELECT *
    FROM (
        SELECT
            automatic.*,
            ROW_NUMBER() OVER (
                PARTITION BY occurrence_version_id, concept_id
                ORDER BY automatic_generation DESC
            ) AS row_number
        FROM occurrence_concept_automatic_links AS automatic
    )
    WHERE row_number = 1
)
SELECT
    occurrence_version_id,
    concept_id,
    relation_role,
    'override' AS link_source
FROM latest_override
WHERE override_status = 'active' AND action = 'include'
UNION ALL
SELECT
    automatic.occurrence_version_id,
    automatic.concept_id,
    automatic.relation_role,
    'automatic' AS link_source
FROM latest_automatic AS automatic
WHERE automatic.link_status = 'active'
  AND NOT EXISTS (
    SELECT 1
    FROM latest_override AS override
    WHERE override.occurrence_version_id = automatic.occurrence_version_id
      AND override.concept_id = automatic.concept_id
      AND override.override_status = 'active'
);

CREATE TABLE IF NOT EXISTS metonym_candidates (
    metonym_candidate_id TEXT PRIMARY KEY,
    occurrence_version_id TEXT NOT NULL,
    normalized_surface TEXT NOT NULL,
    candidate_concept_id TEXT NOT NULL,
    candidate_generation INTEGER NOT NULL CHECK (candidate_generation > 0),
    reason TEXT NOT NULL,
    confidence REAL,
    produced_by_stage_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        occurrence_version_id,
        normalized_surface,
        candidate_concept_id,
        candidate_generation
    ),
    FOREIGN KEY (occurrence_version_id) REFERENCES claim_occurrences (occurrence_version_id),
    FOREIGN KEY (candidate_concept_id) REFERENCES concepts (concept_id),
    FOREIGN KEY (produced_by_stage_version_id)
        REFERENCES lineage_stage_versions (stage_version_id)
);

CREATE TABLE IF NOT EXISTS metonym_overrides (
    metonym_override_id TEXT PRIMARY KEY,
    occurrence_version_id TEXT NOT NULL,
    normalized_surface TEXT NOT NULL,
    decision_generation INTEGER NOT NULL CHECK (decision_generation > 0),
    decision TEXT NOT NULL CHECK (decision IN ('pin', 'unresolved', 'reject')),
    selected_concept_id TEXT,
    occurrence_fingerprint TEXT NOT NULL,
    override_status TEXT NOT NULL CHECK (override_status IN ('active', 'stale')),
    rationale TEXT,
    decided_at TEXT NOT NULL,
    UNIQUE (occurrence_version_id, normalized_surface, decision_generation),
    FOREIGN KEY (occurrence_version_id) REFERENCES claim_occurrences (occurrence_version_id),
    FOREIGN KEY (selected_concept_id) REFERENCES concepts (concept_id),
    CHECK (
        (decision = 'pin' AND selected_concept_id IS NOT NULL)
        OR (decision <> 'pin' AND selected_concept_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS card_relations (
    card_relation_id TEXT PRIMARY KEY,
    card_revision_id TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    relation_generation INTEGER NOT NULL CHECK (relation_generation > 0),
    relation_status TEXT NOT NULL DEFAULT 'active' CHECK (
        relation_status IN ('active', 'absent')
    ),
    relation_kind TEXT NOT NULL CHECK (relation_kind IN ('direct', 'context', 'mentioned')),
    strongest_relation_role TEXT NOT NULL CHECK (
        strongest_relation_role IN ('subject', 'actor', 'object', 'context', 'mentioned', 'unknown')
    ),
    relation_inputs_hash TEXT NOT NULL,
    produced_by_stage_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (card_revision_id, concept_id, relation_generation),
    FOREIGN KEY (card_revision_id) REFERENCES card_revisions (card_revision_id),
    FOREIGN KEY (concept_id) REFERENCES concepts (concept_id),
    FOREIGN KEY (produced_by_stage_version_id)
        REFERENCES lineage_stage_versions (stage_version_id)
);

CREATE TABLE IF NOT EXISTS card_relation_contributors (
    card_relation_id TEXT NOT NULL,
    occurrence_version_id TEXT NOT NULL,
    contribution_role TEXT NOT NULL CHECK (
        contribution_role IN ('subject', 'actor', 'object', 'context', 'mentioned', 'unknown')
    ),
    PRIMARY KEY (card_relation_id, occurrence_version_id),
    FOREIGN KEY (card_relation_id) REFERENCES card_relations (card_relation_id),
    FOREIGN KEY (occurrence_version_id) REFERENCES claim_occurrences (occurrence_version_id)
);

CREATE VIEW IF NOT EXISTS effective_card_relations AS
WITH latest AS (
    SELECT
        relation.*,
        ROW_NUMBER() OVER (
            PARTITION BY card_revision_id, concept_id
            ORDER BY relation_generation DESC
        ) AS row_number
    FROM card_relations AS relation
)
SELECT
    card_relation_id,
    card_revision_id,
    concept_id,
    relation_generation,
    relation_kind,
    strongest_relation_role,
    relation_inputs_hash,
    produced_by_stage_version_id,
    created_at
FROM latest
WHERE row_number = 1 AND relation_status = 'active';

CREATE TABLE IF NOT EXISTS hierarchy_proposals (
    hierarchy_proposal_id TEXT PRIMARY KEY,
    child_concept_id TEXT NOT NULL,
    parent_or_related_concept_id TEXT NOT NULL,
    edge_kind TEXT NOT NULL CHECK (edge_kind IN ('primary_parent', 'related')),
    proposal_generation INTEGER NOT NULL CHECK (proposal_generation > 0),
    proposal_payload_json TEXT NOT NULL,
    produced_by_stage_version_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (
        child_concept_id,
        parent_or_related_concept_id,
        edge_kind,
        proposal_generation
    ),
    FOREIGN KEY (child_concept_id) REFERENCES concepts (concept_id),
    FOREIGN KEY (parent_or_related_concept_id) REFERENCES concepts (concept_id),
    FOREIGN KEY (produced_by_stage_version_id)
        REFERENCES lineage_stage_versions (stage_version_id),
    CHECK (child_concept_id <> parent_or_related_concept_id)
);

CREATE TABLE IF NOT EXISTS hierarchy_review_decisions (
    hierarchy_review_decision_id TEXT PRIMARY KEY,
    hierarchy_proposal_id TEXT NOT NULL,
    decision_generation INTEGER NOT NULL CHECK (decision_generation > 0),
    decision TEXT NOT NULL CHECK (
        decision IN ('approved', 'rejected', 'deferred', 'reopened')
    ),
    rationale TEXT,
    decided_at TEXT NOT NULL,
    UNIQUE (hierarchy_proposal_id, decision_generation),
    FOREIGN KEY (hierarchy_proposal_id)
        REFERENCES hierarchy_proposals (hierarchy_proposal_id)
);

CREATE VIEW IF NOT EXISTS hierarchy_proposal_current_decisions AS
WITH ranked AS (
    SELECT
        decision.*,
        ROW_NUMBER() OVER (
            PARTITION BY hierarchy_proposal_id
            ORDER BY decision_generation DESC
        ) AS row_number
    FROM hierarchy_review_decisions AS decision
)
SELECT *
FROM ranked
WHERE row_number = 1;

CREATE TABLE IF NOT EXISTS approved_primary_hierarchy_edges (
    primary_hierarchy_edge_id TEXT PRIMARY KEY,
    child_concept_id TEXT NOT NULL,
    parent_concept_id TEXT,
    approval_generation INTEGER NOT NULL CHECK (approval_generation > 0),
    action TEXT NOT NULL CHECK (action IN ('approve', 'remove')),
    review_decision_id TEXT,
    hierarchy_review_decision_id TEXT,
    approved_at TEXT NOT NULL,
    UNIQUE (child_concept_id, approval_generation),
    FOREIGN KEY (child_concept_id) REFERENCES concepts (concept_id),
    FOREIGN KEY (parent_concept_id) REFERENCES concepts (concept_id),
    FOREIGN KEY (review_decision_id)
        REFERENCES concept_review_decisions (concept_review_decision_id),
    FOREIGN KEY (hierarchy_review_decision_id)
        REFERENCES hierarchy_review_decisions (hierarchy_review_decision_id),
    CHECK (
        (action = 'approve' AND parent_concept_id IS NOT NULL AND child_concept_id <> parent_concept_id)
        OR (action = 'remove' AND parent_concept_id IS NULL)
    )
);

CREATE VIEW IF NOT EXISTS effective_primary_hierarchy_edges AS
WITH ranked AS (
    SELECT
        edge.*,
        ROW_NUMBER() OVER (
            PARTITION BY child_concept_id
            ORDER BY approval_generation DESC
        ) AS row_number
    FROM approved_primary_hierarchy_edges AS edge
)
SELECT
    primary_hierarchy_edge_id,
    child_concept_id,
    parent_concept_id,
    approval_generation
FROM ranked
WHERE row_number = 1 AND action = 'approve';

CREATE TABLE IF NOT EXISTS approved_related_concept_edges (
    related_concept_edge_id TEXT PRIMARY KEY,
    left_concept_id TEXT NOT NULL,
    right_concept_id TEXT NOT NULL,
    approval_generation INTEGER NOT NULL CHECK (approval_generation > 0),
    action TEXT NOT NULL CHECK (action IN ('approve', 'remove')),
    review_decision_id TEXT,
    hierarchy_review_decision_id TEXT,
    approved_at TEXT NOT NULL,
    UNIQUE (left_concept_id, right_concept_id, approval_generation),
    FOREIGN KEY (left_concept_id) REFERENCES concepts (concept_id),
    FOREIGN KEY (right_concept_id) REFERENCES concepts (concept_id),
    FOREIGN KEY (review_decision_id)
        REFERENCES concept_review_decisions (concept_review_decision_id),
    FOREIGN KEY (hierarchy_review_decision_id)
        REFERENCES hierarchy_review_decisions (hierarchy_review_decision_id),
    CHECK (left_concept_id < right_concept_id)
);

CREATE VIEW IF NOT EXISTS effective_related_concept_edges AS
WITH ranked AS (
    SELECT
        edge.*,
        ROW_NUMBER() OVER (
            PARTITION BY left_concept_id, right_concept_id
            ORDER BY approval_generation DESC
        ) AS row_number
    FROM approved_related_concept_edges AS edge
)
SELECT
    related_concept_edge_id,
    left_concept_id,
    right_concept_id,
    approval_generation
FROM ranked
WHERE row_number = 1 AND action = 'approve';

CREATE TABLE IF NOT EXISTS llm_analysis_artifacts (
    llm_analysis_artifact_id TEXT PRIMARY KEY,
    analysis_kind TEXT NOT NULL
        CHECK (analysis_kind IN ('identity_review', 'hierarchy_review')),
    analysis_inputs_hash TEXT NOT NULL,
    model_profile_version TEXT NOT NULL,
    prompt_template_version TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (analysis_kind, analysis_inputs_hash)
);

CREATE TABLE IF NOT EXISTS manual_sidecars (
    manual_sidecar_version_id TEXT PRIMARY KEY,
    concept_id TEXT NOT NULL,
    sidecar_generation INTEGER NOT NULL CHECK (sidecar_generation > 0),
    content_hash TEXT NOT NULL,
    markdown_text TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (concept_id, sidecar_generation),
    UNIQUE (manual_sidecar_version_id, concept_id, sidecar_generation),
    FOREIGN KEY (concept_id) REFERENCES concepts (concept_id)
);

CREATE TABLE IF NOT EXISTS manual_sidecar_heads (
    concept_id TEXT PRIMARY KEY,
    current_manual_sidecar_version_id TEXT NOT NULL,
    current_sidecar_generation INTEGER NOT NULL CHECK (current_sidecar_generation > 0),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (
        current_manual_sidecar_version_id,
        concept_id,
        current_sidecar_generation
    ) REFERENCES manual_sidecars (
        manual_sidecar_version_id,
        concept_id,
        sidecar_generation
    )
);

CREATE TABLE IF NOT EXISTS projection_artifacts (
    projection_artifact_id TEXT PRIMARY KEY,
    projection_kind TEXT NOT NULL CHECK (projection_kind IN ('card', 'claim', 'hub')),
    projection_scope_key TEXT NOT NULL,
    card_revision_id TEXT,
    concept_id TEXT,
    claim_group_id TEXT,
    projection_generation INTEGER NOT NULL CHECK (projection_generation > 0),
    projection_inputs_hash TEXT NOT NULL,
    projection_output_hash TEXT NOT NULL,
    fts_document_hash TEXT NOT NULL,
    rendered_content TEXT NOT NULL,
    search_text TEXT NOT NULL,
    processor_contract_version_id TEXT NOT NULL,
    produced_by_stage_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (projection_kind, projection_scope_key, projection_generation),
    UNIQUE (
        projection_artifact_id,
        projection_kind,
        projection_scope_key,
        projection_generation,
        projection_inputs_hash,
        projection_output_hash,
        fts_document_hash
    ),
    UNIQUE (
        projection_artifact_id,
        projection_kind,
        projection_scope_key,
        projection_generation,
        concept_id
    ),
    UNIQUE (
        projection_artifact_id,
        projection_kind,
        projection_scope_key,
        projection_generation,
        claim_group_id
    ),
    UNIQUE (
        projection_artifact_id,
        projection_kind,
        projection_scope_key,
        projection_generation,
        card_revision_id
    ),
    FOREIGN KEY (
        produced_by_stage_version_id,
        processor_contract_version_id
    ) REFERENCES lineage_stage_versions (
        stage_version_id,
        processor_contract_version_id
    ),
    FOREIGN KEY (card_revision_id) REFERENCES card_revisions (card_revision_id),
    FOREIGN KEY (concept_id) REFERENCES concept_heads (concept_id),
    FOREIGN KEY (claim_group_id) REFERENCES claim_groups (claim_group_id),
    CHECK (
        (
            projection_kind = 'card'
            AND card_revision_id IS NOT NULL
            AND projection_scope_key = card_revision_id
            AND concept_id IS NULL
            AND claim_group_id IS NULL
        )
        OR (
            projection_kind = 'claim'
            AND card_revision_id IS NULL
            AND concept_id IS NULL
            AND claim_group_id IS NOT NULL
            AND projection_scope_key = claim_group_id
        )
        OR (
            projection_kind = 'hub'
            AND card_revision_id IS NULL
            AND concept_id IS NOT NULL
            AND claim_group_id IS NULL
            AND projection_scope_key = concept_id
        )
    )
);

CREATE TRIGGER IF NOT EXISTS wiki_projection_artifact_stage_kind
BEFORE INSERT ON projection_artifacts
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM lineage_stage_versions AS stage
    WHERE stage.stage_version_id = NEW.produced_by_stage_version_id
      AND stage.processor_contract_version_id = NEW.processor_contract_version_id
      AND stage.stage_kind = CASE NEW.projection_kind
          WHEN 'card' THEN '__WIKI_CARD_PROJECTION_STAGE_KIND__'
          WHEN 'claim' THEN '__WIKI_CLAIM_PROJECTION_STAGE_KIND__'
          WHEN 'hub' THEN '__WIKI_HUB_PROJECTION_STAGE_KIND__'
      END
      AND (
          NEW.projection_kind <> 'card'
          OR EXISTS (
              SELECT 1
              FROM card_revisions AS card
              WHERE card.card_revision_id = NEW.card_revision_id
                AND card.source_lineage_id = stage.source_lineage_id
          )
      )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'projection artifact requires its mapped stage kind, contract, and lineage'
    );
END;

CREATE TABLE IF NOT EXISTS projection_heads (
    projection_kind TEXT NOT NULL CHECK (projection_kind IN ('card', 'claim', 'hub')),
    projection_scope_key TEXT NOT NULL,
    card_revision_id TEXT,
    concept_id TEXT,
    claim_group_id TEXT,
    current_projection_artifact_id TEXT NOT NULL,
    current_projection_generation INTEGER NOT NULL CHECK (current_projection_generation > 0),
    current_projection_inputs_hash TEXT NOT NULL,
    current_projection_output_hash TEXT NOT NULL,
    current_fts_document_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (projection_kind, projection_scope_key),
    FOREIGN KEY (
        current_projection_artifact_id,
        projection_kind,
        projection_scope_key,
        current_projection_generation,
        current_projection_inputs_hash,
        current_projection_output_hash,
        current_fts_document_hash
    ) REFERENCES projection_artifacts (
        projection_artifact_id,
        projection_kind,
        projection_scope_key,
        projection_generation,
        projection_inputs_hash,
        projection_output_hash,
        fts_document_hash
    ),
    FOREIGN KEY (
        current_projection_artifact_id,
        projection_kind,
        projection_scope_key,
        current_projection_generation,
        concept_id
    ) REFERENCES projection_artifacts (
        projection_artifact_id,
        projection_kind,
        projection_scope_key,
        projection_generation,
        concept_id
    ),
    FOREIGN KEY (
        current_projection_artifact_id,
        projection_kind,
        projection_scope_key,
        current_projection_generation,
        claim_group_id
    ) REFERENCES projection_artifacts (
        projection_artifact_id,
        projection_kind,
        projection_scope_key,
        projection_generation,
        claim_group_id
    ),
    FOREIGN KEY (
        current_projection_artifact_id,
        projection_kind,
        projection_scope_key,
        current_projection_generation,
        card_revision_id
    ) REFERENCES projection_artifacts (
        projection_artifact_id,
        projection_kind,
        projection_scope_key,
        projection_generation,
        card_revision_id
    ),
    FOREIGN KEY (card_revision_id) REFERENCES card_revisions (card_revision_id),
    FOREIGN KEY (concept_id) REFERENCES concept_heads (concept_id),
    FOREIGN KEY (claim_group_id) REFERENCES claim_groups (claim_group_id),
    CHECK (
        (
            projection_kind = 'card'
            AND card_revision_id IS NOT NULL
            AND projection_scope_key = card_revision_id
            AND concept_id IS NULL
            AND claim_group_id IS NULL
        )
        OR (
            projection_kind = 'claim'
            AND card_revision_id IS NULL
            AND concept_id IS NULL
            AND claim_group_id IS NOT NULL
            AND projection_scope_key = claim_group_id
        )
        OR (
            projection_kind = 'hub'
            AND card_revision_id IS NULL
            AND concept_id IS NOT NULL
            AND claim_group_id IS NULL
            AND projection_scope_key = concept_id
        )
    )
);

CREATE TABLE IF NOT EXISTS wiki_fts_documents (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    document_key TEXT NOT NULL UNIQUE,
    document_kind TEXT NOT NULL CHECK (
        document_kind IN ('card', 'claim', 'hub')
    ),
    scope_key TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source_ref_json TEXT NOT NULL,
    projection_output_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (document_kind, scope_key)
);

CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
    title,
    body,
    content='wiki_fts_documents',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS wiki_fts_documents_ai
AFTER INSERT ON wiki_fts_documents
BEGIN
    INSERT INTO wiki_fts(rowid, title, body)
    VALUES (NEW.rowid, NEW.title, NEW.body);
END;

CREATE TRIGGER IF NOT EXISTS wiki_fts_documents_ad
AFTER DELETE ON wiki_fts_documents
BEGIN
    INSERT INTO wiki_fts(wiki_fts, rowid, title, body)
    VALUES ('delete', OLD.rowid, OLD.title, OLD.body);
END;

CREATE TRIGGER IF NOT EXISTS wiki_fts_documents_au
AFTER UPDATE ON wiki_fts_documents
BEGIN
    INSERT INTO wiki_fts(wiki_fts, rowid, title, body)
    VALUES ('delete', OLD.rowid, OLD.title, OLD.body);
    INSERT INTO wiki_fts(rowid, title, body)
    VALUES (NEW.rowid, NEW.title, NEW.body);
END;
"""

_BASE_SCHEMA_DDL = _BASE_SCHEMA_DDL.replace(
    "__WIKI_DEPENDENCY_KINDS__",
    ", ".join(f"'{kind}'" for kind in ALLOWED_DEPENDENCY_KINDS),
)
_BASE_SCHEMA_DDL = _BASE_SCHEMA_DDL.replace(
    "__WIKI_CLAIM_EXTRACTION_STAGE_KIND__",
    CLAIM_EXTRACTION_STAGE_KIND,
)
_BASE_SCHEMA_DDL = _BASE_SCHEMA_DDL.replace(
    "__WIKI_ELIGIBILITY_EVALUATION_STAGE_KIND__",
    ELIGIBILITY_EVALUATION_STAGE_KIND,
)
_BASE_SCHEMA_DDL = _BASE_SCHEMA_DDL.replace(
    "__WIKI_CARD_PROJECTION_STAGE_KIND__",
    PROJECTION_STAGE_KIND_BY_KIND["card"],
)
_BASE_SCHEMA_DDL = _BASE_SCHEMA_DDL.replace(
    "__WIKI_CLAIM_PROJECTION_STAGE_KIND__",
    PROJECTION_STAGE_KIND_BY_KIND["claim"],
)
_BASE_SCHEMA_DDL = _BASE_SCHEMA_DDL.replace(
    "__WIKI_HUB_PROJECTION_STAGE_KIND__",
    PROJECTION_STAGE_KIND_BY_KIND["hub"],
)

_IMMUTABLE_TABLES = (
    "schema_metadata",
    "source_lineages",
    "processor_contract_versions",
    "processor_contract_activations",
    "lineage_stage_versions",
    "card_revisions",
    "lineage_input_versions",
    "card_revision_input_bindings",
    "dependency_versions",
    "lineage_stage_input_bindings",
    "stage_dependency_bindings",
    "extraction_artifacts",
    "extraction_artifact_items",
    "extraction_runs",
    "claim_occurrences",
    "extraction_run_occurrences",
    "occurrence_state_events",
    "eligibility_evaluation_versions",
    "concepts",
    "concept_revisions",
    "identity_aliases",
    "surface_revisions",
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
    "hierarchy_review_decisions",
    "approved_primary_hierarchy_edges",
    "approved_related_concept_edges",
    "llm_analysis_artifacts",
    "manual_sidecars",
    "projection_artifacts",
)

_MONOTONIC_HEADS = (
    ("source_lineage_heads", "card_head_generation"),
    ("lineage_input_heads", "current_input_generation"),
    ("active_processor_contract_heads", "current_activation_generation"),
    ("dependency_heads", "current_generation"),
    ("lineage_stage_heads", "current_stage_generation"),
    ("concept_heads", "current_concept_generation"),
    ("surface_heads", "current_surface_generation"),
    ("manual_sidecar_heads", "current_sidecar_generation"),
    ("projection_heads", "current_projection_generation"),
    ("eligibility_heads", "current_eligibility_generation"),
)


def _immutable_trigger_sql(table_name: str) -> str:
    return f"""
CREATE TRIGGER IF NOT EXISTS wiki_immutable_{table_name}_update
BEFORE UPDATE ON {table_name}
BEGIN
    SELECT RAISE(ABORT, '{table_name} rows are immutable');
END;

CREATE TRIGGER IF NOT EXISTS wiki_immutable_{table_name}_delete
BEFORE DELETE ON {table_name}
BEGIN
    SELECT RAISE(ABORT, '{table_name} rows are immutable');
END;
"""


def _monotonic_head_trigger_sql(table_name: str, generation_column: str) -> str:
    return f"""
CREATE TRIGGER IF NOT EXISTS wiki_monotonic_{table_name}
BEFORE UPDATE ON {table_name}
WHEN NEW.{generation_column} <= OLD.{generation_column}
BEGIN
    SELECT RAISE(ABORT, '{table_name} generation must increase');
END;
"""


SCHEMA_DDL = (
    _BASE_SCHEMA_DDL
    + "\n".join(_immutable_trigger_sql(table) for table in _IMMUTABLE_TABLES)
    + "\n".join(
        _monotonic_head_trigger_sql(table, generation_column)
        for table, generation_column in _MONOTONIC_HEADS
    )
)
