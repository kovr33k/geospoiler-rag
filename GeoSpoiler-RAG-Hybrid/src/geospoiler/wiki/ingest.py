"""Offline Enriched v2 ingest orchestration for Wiki v3."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from geospoiler.wiki.cards import (
    CLAIM_INPUT_KIND,
    AdaptedWikiCard,
    CardDocumentError,
    CardModel,
    adapt_card,
    discover_card_files,
    load_card_file,
)
from geospoiler.wiki.eligibility import EligibilityEvaluation, evaluate_eligibility
from geospoiler.wiki.extraction import (
    BLUEPRINT_SCHEMA_VERSION,
    ExtractionArtifact,
    build_extraction_artifact,
    store_extraction_artifact,
)
from geospoiler.wiki.lifecycle import (
    LifecycleApplyResult,
    PreparedLifecycleApply,
    apply_lifecycle,
    prepare_lifecycle_apply,
)
from geospoiler.wiki.schema import CLAIM_EXTRACTION_STAGE_KIND
from geospoiler.wiki.state import (
    CardRevision,
    ProcessorContractSpec,
    SourceLineage,
    StageRun,
    StageVersion,
    StateConflictError,
    activate_processor_contract,
    ensure_source_lineage,
    fail_stage_run,
    record_card_revision,
    schedule_stage,
    start_stage_run,
)

DEFAULT_CLAIM_EXTRACTION_CONTRACT = ProcessorContractSpec(
    algorithm_version="deterministic-occurrence-blueprints-v1",
    schema_version=BLUEPRINT_SCHEMA_VERSION,
    canonicalizer_version="wiki-canonical-json-nfc-v1",
    policy_version="strict-locator-lifecycle-v1",
)

IngestExtractionStatus = Literal["committed", "stale", "no_op", "failed"]


@dataclass(frozen=True)
class RecordedWikiCard:
    adapted_card: AdaptedWikiCard
    lineage: SourceLineage
    card_revision: CardRevision
    eligibility: EligibilityEvaluation
    lineage_changed: bool


@dataclass(frozen=True)
class PreparedExtraction:
    recorded_card: RecordedWikiCard
    stage_version: StageVersion
    stage_run: StageRun
    artifact: ExtractionArtifact
    lifecycle_apply: PreparedLifecycleApply


@dataclass(frozen=True)
class CardIngestResult:
    recorded_card: RecordedWikiCard
    extraction_status: IngestExtractionStatus
    lifecycle_result: LifecycleApplyResult | None


@dataclass(frozen=True)
class IngestError:
    path: Path
    message: str
    card_ordinal: int | None = None


@dataclass(frozen=True)
class DirectoryIngestStats:
    files_seen: int
    files_valid: int
    files_invalid: int
    lineages_changed: int
    cards_changed: int
    extraction_runs_committed: int
    extraction_runs_stale: int
    extraction_runs_no_op: int
    extraction_runs_failed: int
    occurrences_active: int
    occurrences_retired: int
    occurrences_superseded: int
    occurrences_reactivated: int
    eligibility_changes: int
    errors: tuple[IngestError, ...]


def record_ingested_card(
    connection: sqlite3.Connection,
    value: Mapping[str, Any] | CardModel | AdaptedWikiCard,
) -> RecordedWikiCard:
    """Validate and record card/input revisions plus the independent eligibility overlay."""
    adapted = value if isinstance(value, AdaptedWikiCard) else adapt_card(value)
    existing_lineage = connection.execute(
        """
        SELECT source_lineage_id
        FROM source_lineages
        WHERE source_kind = ? AND external_key = ?
        """,
        (adapted.source_kind, adapted.external_key),
    ).fetchone()
    lineage = ensure_source_lineage(
        connection,
        source_kind=adapted.source_kind,
        external_key=adapted.external_key,
        source_lineage_id=adapted.source_lineage_id,
    )
    card_revision = record_card_revision(
        connection,
        source_lineage_id=lineage.source_lineage_id,
        card_payload=adapted.card_payload,
        input_payloads=adapted.input_payloads,
        card_revision_id=adapted.card_revision_id,
        card_unordered_collection_paths=adapted.card_unordered_collection_paths,
        card_exact_quote_paths=adapted.card_exact_quote_paths,
        input_unordered_collection_paths=adapted.input_unordered_collection_paths,
        input_exact_quote_paths=adapted.input_exact_quote_paths,
    )
    eligibility = evaluate_eligibility(
        connection,
        source_lineage_id=lineage.source_lineage_id,
        card_revision_id=card_revision.card_revision_id,
    )
    return RecordedWikiCard(
        adapted_card=adapted,
        lineage=lineage,
        card_revision=card_revision,
        eligibility=eligibility,
        lineage_changed=existing_lineage is None,
    )


def prepare_card_extraction(
    connection: sqlite3.Connection,
    recorded_card: RecordedWikiCard,
    *,
    contract: ProcessorContractSpec = DEFAULT_CLAIM_EXTRACTION_CONTRACT,
    idempotency_key: str | None = None,
) -> PreparedExtraction:
    """Build/store an artifact and start an attempt, but do not apply lifecycle state."""
    activate_processor_contract(
        connection,
        stage_kind=CLAIM_EXTRACTION_STAGE_KIND,
        contract=contract,
    )
    stage = schedule_stage(
        connection,
        source_lineage_id=recorded_card.lineage.source_lineage_id,
        stage_kind=CLAIM_EXTRACTION_STAGE_KIND,
        input_kinds=[CLAIM_INPUT_KIND],
    )
    claim_head = next(
        head
        for head in recorded_card.card_revision.input_heads
        if head.input_kind == CLAIM_INPUT_KIND
    )
    staged_claim_hash = _stage_claim_inputs_hash(stage)
    if claim_head.input_hash != staged_claim_hash:
        raise StateConflictError(
            "Recorded card claim inputs are no longer the scheduled stage input"
        )
    artifact = build_extraction_artifact(
        claim_inputs=recorded_card.adapted_card.input_payloads[CLAIM_INPUT_KIND],
        claim_inputs_hash=staged_claim_hash,
        processor_contract_version_id=stage.processor_contract_version_id,
        processor_contract_hash=stage.processor_contract_hash,
    )
    store_extraction_artifact(connection, artifact)
    resolved_idempotency_key = idempotency_key or (
        f"wiki-claim-extraction:{recorded_card.lineage.source_lineage_id}:"
        f"{stage.stage_version_id}"
    )
    run = start_stage_run(
        connection,
        stage_version_id=stage.stage_version_id,
        idempotency_key=resolved_idempotency_key,
        artifact_source_card_revision_id=recorded_card.card_revision.card_revision_id,
    )
    lifecycle_apply = prepare_lifecycle_apply(
        connection,
        stage_run_id=run.stage_run_id,
        extraction_artifact_id=artifact.extraction_artifact_id,
    )
    return PreparedExtraction(
        recorded_card=recorded_card,
        stage_version=stage,
        stage_run=run,
        artifact=artifact,
        lifecycle_apply=lifecycle_apply,
    )


def apply_prepared_extraction(
    connection: sqlite3.Connection,
    prepared: PreparedExtraction,
) -> LifecycleApplyResult:
    """Apply a previously prepared attempt under the final transaction/CAS."""
    return apply_lifecycle(connection, prepared.lifecycle_apply)


def ingest_card(
    connection: sqlite3.Connection,
    value: Mapping[str, Any] | CardModel | AdaptedWikiCard,
    *,
    contract: ProcessorContractSpec = DEFAULT_CLAIM_EXTRACTION_CONTRACT,
) -> CardIngestResult:
    """Record, evaluate, extract, and atomically apply one validated native card."""
    recorded = record_ingested_card(connection, value)
    activate_processor_contract(
        connection,
        stage_kind=CLAIM_EXTRACTION_STAGE_KIND,
        contract=contract,
    )
    stage = schedule_stage(
        connection,
        source_lineage_id=recorded.lineage.source_lineage_id,
        stage_kind=CLAIM_EXTRACTION_STAGE_KIND,
        input_kinds=[CLAIM_INPUT_KIND],
    )
    idempotency_key = (
        f"wiki-claim-extraction:{recorded.lineage.source_lineage_id}:"
        f"{stage.stage_version_id}"
    )
    committed = connection.execute(
        """
        SELECT stage_run_id
        FROM stage_runs
        WHERE idempotency_key = ? AND status = 'committed'
        """,
        (idempotency_key,),
    ).fetchone()
    if committed is not None:
        return CardIngestResult(
            recorded_card=recorded,
            extraction_status="no_op",
            lifecycle_result=None,
        )

    claim_head = next(
        head
        for head in recorded.card_revision.input_heads
        if head.input_kind == CLAIM_INPUT_KIND
    )
    staged_claim_hash = _stage_claim_inputs_hash(stage)
    if claim_head.input_hash != staged_claim_hash:
        raise StateConflictError(
            "Recorded card claim inputs are no longer the scheduled stage input"
        )
    artifact = build_extraction_artifact(
        claim_inputs=recorded.adapted_card.input_payloads[CLAIM_INPUT_KIND],
        claim_inputs_hash=staged_claim_hash,
        processor_contract_version_id=stage.processor_contract_version_id,
        processor_contract_hash=stage.processor_contract_hash,
    )
    store_extraction_artifact(connection, artifact)
    run = start_stage_run(
        connection,
        stage_version_id=stage.stage_version_id,
        idempotency_key=idempotency_key,
        artifact_source_card_revision_id=recorded.card_revision.card_revision_id,
    )
    try:
        prepared = prepare_lifecycle_apply(
            connection,
            stage_run_id=run.stage_run_id,
            extraction_artifact_id=artifact.extraction_artifact_id,
        )
        lifecycle_result = apply_lifecycle(connection, prepared)
    except Exception as exc:
        fail_stage_run(
            connection,
            stage_run_id=run.stage_run_id,
            error_text=str(exc),
        )
        raise
    return CardIngestResult(
        recorded_card=recorded,
        extraction_status=lifecycle_result.status,
        lifecycle_result=lifecycle_result,
    )


def ingest_path(
    connection: sqlite3.Connection,
    path: str | Path,
    *,
    contract: ProcessorContractSpec = DEFAULT_CLAIM_EXTRACTION_CONTRACT,
) -> DirectoryIngestStats:
    """Recursively ingest JSON cards while isolating invalid files."""
    files = discover_card_files(path)
    counts = _MutableStats(files_seen=len(files))
    for card_path in files:
        try:
            cards = load_card_file(card_path)
        except CardDocumentError as exc:
            counts.files_invalid += 1
            counts.errors.append(IngestError(path=card_path, message=str(exc)))
            continue
        counts.files_valid += 1
        ordered_cards = sorted(
            (
                (card_ordinal, adapt_card(card))
                for card_ordinal, card in enumerate(cards)
            ),
            key=lambda item: (
                item[1].source_kind,
                item[1].external_key,
                item[1].card_revision_id,
                item[0],
            ),
        )
        for original_card_ordinal, card in ordered_cards:
            failed_before = _failed_stage_run_count(connection)
            try:
                result = ingest_card(connection, card, contract=contract)
            except Exception as exc:
                counts.extraction_runs_failed += max(
                    _failed_stage_run_count(connection) - failed_before,
                    0,
                )
                counts.errors.append(
                    IngestError(
                        path=card_path,
                        card_ordinal=(
                            original_card_ordinal if len(cards) > 1 else None
                        ),
                        message=str(exc),
                    )
                )
                continue
            counts.add_result(result)
    return counts.freeze()


@dataclass
class _MutableStats:
    files_seen: int = 0
    files_valid: int = 0
    files_invalid: int = 0
    lineages_changed: int = 0
    cards_changed: int = 0
    extraction_runs_committed: int = 0
    extraction_runs_stale: int = 0
    extraction_runs_no_op: int = 0
    extraction_runs_failed: int = 0
    occurrences_active: int = 0
    occurrences_retired: int = 0
    occurrences_superseded: int = 0
    occurrences_reactivated: int = 0
    eligibility_changes: int = 0
    errors: list[IngestError] = field(default_factory=list)

    def add_result(self, result: CardIngestResult) -> None:
        self.lineages_changed += int(result.recorded_card.lineage_changed)
        self.cards_changed += int(result.recorded_card.card_revision.changed)
        self.eligibility_changes += int(result.recorded_card.eligibility.changed)
        if result.extraction_status == "committed":
            self.extraction_runs_committed += 1
        elif result.extraction_status == "stale":
            self.extraction_runs_stale += 1
        elif result.extraction_status == "no_op":
            self.extraction_runs_no_op += 1
        elif result.extraction_status == "failed":
            self.extraction_runs_failed += 1
        if result.lifecycle_result is not None:
            lifecycle = result.lifecycle_result.counts
            self.occurrences_active += lifecycle.active
            self.occurrences_retired += lifecycle.retired
            self.occurrences_superseded += lifecycle.superseded
            self.occurrences_reactivated += lifecycle.reactivated

    def freeze(self) -> DirectoryIngestStats:
        return DirectoryIngestStats(
            files_seen=self.files_seen,
            files_valid=self.files_valid,
            files_invalid=self.files_invalid,
            lineages_changed=self.lineages_changed,
            cards_changed=self.cards_changed,
            extraction_runs_committed=self.extraction_runs_committed,
            extraction_runs_stale=self.extraction_runs_stale,
            extraction_runs_no_op=self.extraction_runs_no_op,
            extraction_runs_failed=self.extraction_runs_failed,
            occurrences_active=self.occurrences_active,
            occurrences_retired=self.occurrences_retired,
            occurrences_superseded=self.occurrences_superseded,
            occurrences_reactivated=self.occurrences_reactivated,
            eligibility_changes=self.eligibility_changes,
            errors=tuple(self.errors),
        )


def _stage_claim_inputs_hash(stage: StageVersion) -> str:
    bindings = [
        binding for binding in stage.input_bindings if binding.input_kind == CLAIM_INPUT_KIND
    ]
    if len(bindings) != 1:
        raise StateConflictError("claim_extraction stage must bind exactly one claim_inputs")
    return bindings[0].input_hash


def _failed_stage_run_count(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM stage_runs WHERE status = 'failed'"
        ).fetchone()[0]
    )
