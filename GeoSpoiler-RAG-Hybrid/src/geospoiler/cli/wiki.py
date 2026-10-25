"""CLI commands for the approved-concept GeoSpoiler Wiki."""

from __future__ import annotations

from pathlib import Path

from geospoiler import config
from geospoiler.wiki.schema import connect_database
from geospoiler.wiki.search import search_wiki
from geospoiler.wiki.service import WikiPipelineStats, configured_input_paths, run_wiki_pipeline, wiki_status


def cmd_wiki_run(
    paths: list[str] | None = None,
    *,
    use_luna: bool | None = None,
) -> WikiPipelineStats:
    """Ingest native cards, propose reviews, and rebuild derived Wiki state."""
    _require_wiki_enabled()
    input_paths = (
        tuple(Path(path) for path in paths)
        if paths
        else configured_input_paths()
    )
    connection = connect_database(config.WIKI_STATE_DB_PATH)
    try:
        stats = run_wiki_pipeline(
            connection,
            input_paths=input_paths,
            output_directory=config.WIKI_OUTPUT_DIR,
            sidecar_directory=config.WIKI_SIDECAR_DIR,
            use_luna=use_luna,
            database_path=config.WIKI_STATE_DB_PATH,
        )
    finally:
        connection.close()
    _print_wiki_run(stats)
    return stats


def cmd_wiki_rebuild(
    *,
    use_luna: bool | None = None,
) -> WikiPipelineStats:
    """Recompute derived Wiki data without re-reading card files."""
    _require_wiki_enabled()
    connection = connect_database(config.WIKI_STATE_DB_PATH)
    try:
        stats = run_wiki_pipeline(
            connection,
            input_paths=(),
            output_directory=config.WIKI_OUTPUT_DIR,
            sidecar_directory=config.WIKI_SIDECAR_DIR,
            use_luna=use_luna,
            database_path=config.WIKI_STATE_DB_PATH,
        )
    finally:
        connection.close()
    _print_wiki_run(stats)
    return stats


def cmd_wiki_search(query: str, *, limit: int = 12) -> None:
    _require_wiki_enabled()
    connection = connect_database(config.WIKI_STATE_DB_PATH)
    try:
        matches = search_wiki(connection, query, limit=limit)
    finally:
        connection.close()
    if not matches:
        print("Wiki: совпадений нет.")
        return
    print(f"\nWiki search: {len(matches)} result(s)")
    print("=" * 72)
    for ordinal, match in enumerate(matches, 1):
        print(
            f"{ordinal}. [{match.document_kind}] {match.title}\n"
            f"   {match.snippet}\n"
            f"   scope: {match.scope_key}"
        )


def cmd_wiki_status() -> None:
    _require_wiki_enabled()
    status = wiki_status()
    print("\nWiki status")
    print("=" * 60)
    labels = {
        "approved_concepts": "Approved concepts",
        "approved_aliases": "Approved aliases",
        "known_surfaces": "Known surfaces",
        "pending_proposals": "Concept/alias proposals",
        "pending_hierarchy": "Hierarchy proposals",
        "pending_ambiguities": "Ambiguity reviews",
        "projection_cards": "Card projections",
        "projection_claims": "Claim projections",
        "projection_hubs": "Hub projections",
        "fts_documents": "Wiki FTS documents",
        "rejected_proposals": "Rejected proposals",
        "deferred_proposals": "Deferred proposals",
    }
    for key, label in labels.items():
        print(f"{label}: {status.get(key, 0)}")
    print(f"DB: {config.WIKI_STATE_DB_PATH}")
    print(f"Generated hubs: {config.WIKI_OUTPUT_DIR}")
    print(f"Manual sidecars: {config.WIKI_SIDECAR_DIR}")


def _require_wiki_enabled() -> None:
    if not config.WIKI_ENABLED:
        raise RuntimeError(
            "Wiki disabled (WIKI_ENABLED=false); set WIKI_ENABLED=true to enable it"
        )


def _print_wiki_run(stats: WikiPipelineStats) -> None:
    ingested = sum(run.cards_changed for run in stats.ingest_runs)
    ingest_errors = stats.ingest_errors
    print("\nWiki refresh")
    print("=" * 60)
    print(f"Changed cards: {ingested}")
    print(f"Input errors: {len(ingest_errors)}")
    print(f"New registry proposals: {stats.registry.proposals_created}")
    print(
        "Derived: "
        f"{stats.grouping.occurrences_seen} occurrences, "
        f"{stats.relations.active_links} concept links"
    )
    print(
        "Projections: "
        f"{stats.projections.cards_built} cards, "
        f"{stats.projections.claims_built} claims, "
        f"{stats.projections.hubs_built} hubs, "
        f"{stats.projections.fts_documents} FTS documents"
    )
    print(
        "Review: "
        f"{stats.review_counts.concepts} concepts/aliases, "
        f"{stats.review_counts.hierarchy} hierarchy, "
        f"{stats.review_counts.ambiguities} ambiguities"
    )
    analysis_parts: list[str] = []
    if stats.identity_analysis is not None:
        analysis_parts.append(
            "identity "
            + (
                "cache hit"
                if stats.identity_analysis.cache_hit
                else (
                    f"{stats.identity_analysis.identity_groups_created} groups, "
                    f"{stats.identity_analysis.canonicalizations_created} "
                    "canonicalizations, "
                    f"{stats.identity_analysis.alias_proposals_created} aliases"
                )
            )
        )
    if stats.hierarchy_analysis is not None:
        analysis_parts.append(
            "hierarchy "
            + (
                "cache hit"
                if stats.hierarchy_analysis.cache_hit
                else (
                    f"{stats.hierarchy_analysis.primary_proposals_created} primary, "
                    f"{stats.hierarchy_analysis.related_proposals_created} related"
                )
            )
        )
    if analysis_parts:
        print(f"Luna analysis: {'; '.join(analysis_parts)}")
    analysis_errors = [
        error
        for error in (
            None
            if stats.identity_analysis is None
            else stats.identity_analysis.error,
            None
            if stats.hierarchy_analysis is None
            else stats.hierarchy_analysis.error,
            *stats.relations.resolver_errors,
        )
        if error
    ]
    for error in analysis_errors:
        print(f"  Luna warning: {error}")
    if ingest_errors:
        print("Input errors:")
        for error in ingest_errors[:10]:
            print(f"  - {error}")
