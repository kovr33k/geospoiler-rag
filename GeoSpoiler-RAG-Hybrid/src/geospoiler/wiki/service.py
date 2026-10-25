"""Top-level Wiki workflow used by the pipeline, CLI, and reviewer."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from geospoiler import config
from geospoiler.providers import llm_backend
from geospoiler.wiki.analysis import (
    HierarchyAnalysisStats,
    IdentityAnalysisStats,
    propose_hierarchy_reviews_with_luna,
    propose_identity_reviews_with_luna,
)
from geospoiler.wiki.grouping import GroupingStats, group_all_claims
from geospoiler.wiki.hierarchy import pending_hierarchy_count
from geospoiler.wiki.ingest import DirectoryIngestStats, ingest_path
from geospoiler.wiki.projections import ProjectionBatchStats, rebuild_all_projections
from geospoiler.wiki.registry import RegistryScanStats, pending_proposal_count, registry_status, scan_registry
from geospoiler.wiki.relations import RelationStats, link_all_concepts, list_pending_ambiguities
from geospoiler.wiki.schema import connect_database


@dataclass(frozen=True)
class WikiReviewCounts:
    concepts: int
    hierarchy: int
    ambiguities: int

    @property
    def total(self) -> int:
        return self.concepts + self.hierarchy + self.ambiguities


@dataclass(frozen=True)
class WikiPipelineStats:
    database_path: Path
    ingest_runs: tuple[DirectoryIngestStats, ...]
    registry: RegistryScanStats
    identity_analysis: IdentityAnalysisStats | None
    grouping: GroupingStats
    relations: RelationStats
    hierarchy_analysis: HierarchyAnalysisStats | None
    projections: ProjectionBatchStats
    review_counts: WikiReviewCounts

    @property
    def ingest_errors(self) -> tuple[str, ...]:
        return tuple(
            f"{error.path}: {error.message}"
            for run in self.ingest_runs
            for error in run.errors
        )


def configured_input_paths() -> tuple[Path, ...]:
    """Return native Enriched and YouTube-card roots in deterministic order."""
    if not config.WIKI_ENABLED:
        return ()
    return tuple(
        path
        for path in (
            config.ENRICHED_DIR,
            config.YOUTUBE_SEGMENTS_DIR,
        )
        if path.exists()
    )


def run_configured_wiki_pipeline(
    *,
    use_luna: bool | None = None,
) -> WikiPipelineStats:
    """Ingest current saved cards and refresh every Wiki layer."""
    if not config.WIKI_ENABLED:
        raise RuntimeError(
            "Wiki disabled (WIKI_ENABLED=false); configured pipeline will not run"
        )
    connection = connect_database(config.WIKI_STATE_DB_PATH)
    try:
        return run_wiki_pipeline(
            connection,
            input_paths=configured_input_paths(),
            output_directory=config.WIKI_OUTPUT_DIR,
            sidecar_directory=config.WIKI_SIDECAR_DIR,
            use_luna=use_luna,
            database_path=config.WIKI_STATE_DB_PATH,
        )
    finally:
        connection.close()


def run_wiki_pipeline(
    connection: sqlite3.Connection,
    *,
    input_paths: tuple[str | Path, ...] = (),
    output_directory: str | Path | None = None,
    sidecar_directory: str | Path | None = None,
    use_luna: bool | None = None,
    database_path: str | Path = ":memory:",
) -> WikiPipelineStats:
    """Run ingest → proposals → derived DAG → disposable projections."""
    ingest_runs: list[DirectoryIngestStats] = []
    for source_path in input_paths:
        for card_path in _native_card_inputs(Path(source_path)):
            ingest_runs.append(ingest_path(connection, card_path))

    registry = scan_registry(connection)
    luna_enabled = _luna_enabled() if use_luna is None else use_luna
    identity_analysis = (
        propose_identity_reviews_with_luna(connection)
        if luna_enabled
        else None
    )
    grouping = group_all_claims(connection)
    relations = link_all_concepts(connection, use_luna=luna_enabled)
    hierarchy_analysis = (
        propose_hierarchy_reviews_with_luna(connection)
        if luna_enabled
        else None
    )
    projections = rebuild_all_projections(
        connection,
        output_directory=output_directory,
        sidecar_directory=sidecar_directory,
    )
    return WikiPipelineStats(
        database_path=Path(database_path),
        ingest_runs=tuple(ingest_runs),
        registry=registry,
        identity_analysis=identity_analysis,
        grouping=grouping,
        relations=relations,
        hierarchy_analysis=hierarchy_analysis,
        projections=projections,
        review_counts=get_wiki_review_counts(connection),
    )


def refresh_wiki_after_review(
    connection: sqlite3.Connection,
    *,
    output_directory: str | Path | None = None,
    sidecar_directory: str | Path | None = None,
    use_luna: bool | None = None,
) -> ProjectionBatchStats:
    """Recompute derived state after an authoritative reviewer decision."""
    luna_enabled = _luna_enabled() if use_luna is None else use_luna
    group_all_claims(connection)
    link_all_concepts(connection, use_luna=luna_enabled)
    if luna_enabled:
        propose_hierarchy_reviews_with_luna(connection)
    return rebuild_all_projections(
        connection,
        output_directory=output_directory,
        sidecar_directory=sidecar_directory,
    )


def get_wiki_review_counts(
    connection: sqlite3.Connection | None = None,
) -> WikiReviewCounts:
    """Count all Wiki decisions that need the user's browser review."""
    if connection is None and not config.WIKI_ENABLED:
        return WikiReviewCounts(concepts=0, hierarchy=0, ambiguities=0)
    owns_connection = connection is None
    resolved = connection or connect_database(config.WIKI_STATE_DB_PATH)
    try:
        return WikiReviewCounts(
            concepts=pending_proposal_count(resolved),
            hierarchy=pending_hierarchy_count(resolved),
            ambiguities=len(list_pending_ambiguities(resolved)),
        )
    finally:
        if owns_connection:
            resolved.close()


def wiki_status(
    connection: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """Return compact registry, review, projection, and FTS counts."""
    if connection is None and not config.WIKI_ENABLED:
        return {
            "approved_concepts": 0,
            "approved_aliases": 0,
            "known_surfaces": 0,
            "pending_proposals": 0,
            "pending_hierarchy": 0,
            "pending_ambiguities": 0,
            "projection_cards": 0,
            "projection_claims": 0,
            "projection_hubs": 0,
            "fts_documents": 0,
            "rejected_proposals": 0,
            "deferred_proposals": 0,
        }
    owns_connection = connection is None
    resolved = connection or connect_database(config.WIKI_STATE_DB_PATH)
    try:
        status = registry_status(resolved)
        reviews = get_wiki_review_counts(resolved)
        status.update(
            {
                "pending_hierarchy": reviews.hierarchy,
                "pending_ambiguities": reviews.ambiguities,
                "projection_cards": _count_projection(resolved, "card"),
                "projection_claims": _count_projection(resolved, "claim"),
                "projection_hubs": _count_projection(resolved, "hub"),
                "fts_documents": int(
                    resolved.execute(
                        "SELECT COUNT(*) FROM wiki_fts_documents"
                    ).fetchone()[0]
                ),
            }
        )
        return status
    finally:
        if owns_connection:
            resolved.close()


def _native_card_inputs(path: Path) -> tuple[Path, ...]:
    if path.is_file():
        return (path,)
    if not path.is_dir():
        return ()
    native = {
        *path.rglob("*.enriched.json"),
        *path.rglob("*.youtube-segment.json"),
    }
    if native:
        return tuple(sorted(native, key=lambda item: item.as_posix()))
    # Explicit custom directories remain useful for tests/imports containing
    # plain JSON card names; unsupported files are isolated by ingest_path.
    return (path,)


def _luna_enabled() -> bool:
    return llm_backend.active_profile() == "luna"


def _count_projection(
    connection: sqlite3.Connection,
    projection_kind: str,
) -> int:
    """Count current searchable projections, excluding historical heads."""
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM wiki_fts_documents
            WHERE document_kind = ?
            """,
            (projection_kind,),
        ).fetchone()[0]
    )
