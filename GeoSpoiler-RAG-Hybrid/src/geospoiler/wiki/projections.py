"""Disposable card, claim, and approved-concept Wiki projections."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from geospoiler.wiki.cards import CARD_PROJECTION_INPUT_KIND
from geospoiler.wiki.hashing import canonical_json, content_hash, sha256_hex
from geospoiler.wiki.hierarchy import publish_hierarchy_dependencies
from geospoiler.wiki.registry import ApprovedConcept, get_concept, list_concepts, publish_registry_dependencies
from geospoiler.wiki.schema import CARD_PROJECTION_STAGE_KIND, CLAIM_PROJECTION_STAGE_KIND, HUB_PROJECTION_STAGE_KIND
from geospoiler.wiki.sidecars import (
    _atomic_write_text,
    get_manual_sidecar,
    publish_all_manual_sidecar_dependencies,
    sync_sidecars,
)
from geospoiler.wiki.state import (
    DependencyKey,
    ProcessorContractSpec,
    StageVersion,
    activate_processor_contract,
    commit_stage_run,
    deterministic_source_lineage_id,
    ensure_source_lineage,
    get_dependency_head,
    publish_dependency,
    schedule_stage,
    start_stage_run,
)

ProjectionKind = Literal["card", "claim", "hub"]

DEFAULT_CARD_PROJECTION_CONTRACT = ProcessorContractSpec(
    algorithm_version="wiki-card-projection-v1",
    schema_version="wiki-card-markdown-v1",
    canonicalizer_version="wiki-canonical-json-nfc-v1",
    policy_version="current-eligible-card-only-v1",
    builder_version="card-projection-builder-v1",
)
DEFAULT_CLAIM_PROJECTION_CONTRACT = ProcessorContractSpec(
    algorithm_version="wiki-claim-projection-v1",
    schema_version="wiki-claim-markdown-v1",
    canonicalizer_version="wiki-canonical-json-nfc-v1",
    policy_version="effective-occurrence-group-v1",
    builder_version="claim-projection-builder-v1",
)
DEFAULT_HUB_PROJECTION_CONTRACT = ProcessorContractSpec(
    algorithm_version="wiki-hub-projection-v1",
    schema_version="wiki-hub-markdown-v1",
    canonicalizer_version="wiki-canonical-json-nfc-v1",
    policy_version="approved-concept-complete-card-navigation-v1",
    builder_version="hub-projection-builder-v1",
)

_GENERATED_MARKER = "generated_by: geospoiler-wiki-v2"


class ProjectionError(RuntimeError):
    """Raised when a projection cannot be applied against a current DAG snapshot."""


@dataclass(frozen=True)
class ProjectionArtifact:
    projection_artifact_id: str
    projection_kind: ProjectionKind
    scope_key: str
    generation: int
    inputs_hash: str
    output_hash: str
    fts_document_hash: str
    rendered_content: str
    search_text: str
    changed: bool


@dataclass(frozen=True)
class ProjectionBatchStats:
    cards_built: int
    claims_built: int
    hubs_built: int
    fts_documents: int
    hub_files_written: int
    stale_hub_files_removed: int
    sidecar_files_seen: int = 0
    sidecar_files_written: int = 0
    sidecar_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratedHubStats:
    files_written: int
    stale_files_removed: int


def rebuild_all_projections(
    connection: sqlite3.Connection,
    *,
    output_directory: str | Path | None = None,
    sidecar_directory: str | Path | None = None,
) -> ProjectionBatchStats:
    """Refresh every disposable projection from current authoritative/effective state."""
    publish_registry_dependencies(connection)
    publish_hierarchy_dependencies(connection)
    if sidecar_directory is None:
        publish_all_manual_sidecar_dependencies(connection)
        sidecar_seen = 0
        sidecar_written = 0
        sidecar_errors: tuple[str, ...] = ()
    else:
        sidecars = sync_sidecars(connection, sidecar_directory)
        sidecar_seen = sidecars.files_seen
        sidecar_written = sidecars.files_written
        sidecar_errors = sidecars.errors

    cards = build_card_projections(connection)
    claims = build_claim_projections(connection)
    hubs = build_hub_projections(connection)
    fts_count = rebuild_wiki_fts(connection)
    generated = (
        write_generated_hubs(connection, output_directory)
        if output_directory is not None
        else GeneratedHubStats(files_written=0, stale_files_removed=0)
    )
    return ProjectionBatchStats(
        cards_built=sum(int(item.changed) for item in cards),
        claims_built=sum(int(item.changed) for item in claims),
        hubs_built=sum(int(item.changed) for item in hubs),
        fts_documents=fts_count,
        hub_files_written=generated.files_written,
        stale_hub_files_removed=generated.stale_files_removed,
        sidecar_files_seen=sidecar_seen,
        sidecar_files_written=sidecar_written,
        sidecar_errors=sidecar_errors,
    )


def build_card_projections(
    connection: sqlite3.Connection,
    *,
    contract: ProcessorContractSpec = DEFAULT_CARD_PROJECTION_CONTRACT,
) -> tuple[ProjectionArtifact, ...]:
    """Build current eligible card documents without coupling them to claims/hubs."""
    activate_processor_contract(
        connection,
        stage_kind=CARD_PROJECTION_STAGE_KIND,
        contract=contract,
    )
    rows = connection.execute(
        """
        SELECT
            lineage.source_lineage_id,
            card_head.current_card_revision_id,
            input.canonical_payload_json,
            card.canonical_payload_json AS card_payload_json
        FROM source_lineages AS lineage
        JOIN source_lineage_heads AS card_head
          ON card_head.source_lineage_id = lineage.source_lineage_id
        JOIN card_revisions AS card
          ON card.card_revision_id = card_head.current_card_revision_id
        JOIN lineage_input_heads AS input_head
          ON input_head.source_lineage_id = lineage.source_lineage_id
         AND input_head.input_kind = ?
        JOIN lineage_input_versions AS input
          ON input.input_version_id = input_head.current_input_version_id
        JOIN eligibility_heads AS eligibility
          ON eligibility.source_lineage_id = lineage.source_lineage_id
         AND eligibility.evaluated_card_revision_id =
             card_head.current_card_revision_id
        WHERE eligibility.current_eligible = 1
        ORDER BY lineage.source_lineage_id
        """,
        (CARD_PROJECTION_INPUT_KIND,),
    ).fetchall()
    artifacts: list[ProjectionArtifact] = []
    for row in rows:
        source_lineage_id = row["source_lineage_id"]
        card_revision_id = row["current_card_revision_id"]
        projection_input = json.loads(row["canonical_payload_json"])
        card_payload = json.loads(row["card_payload_json"])
        title, rendered, search_text = _render_card_projection(
            card_revision_id=card_revision_id,
            projection_input=projection_input,
            card_payload=card_payload,
        )
        artifact = _build_projection(
            connection,
            projection_kind="card",
            scope_key=card_revision_id,
            source_lineage_id=source_lineage_id,
            stage_kind=CARD_PROJECTION_STAGE_KIND,
            input_kinds=(CARD_PROJECTION_INPUT_KIND,),
            dependencies=(
                DependencyKey("eligibility_state", source_lineage_id),
            ),
            contract=contract,
            title=title,
            rendered_content=rendered,
            search_text=search_text,
            card_revision_id=card_revision_id,
        )
        _publish_projection_dependency(
            connection,
            artifact=artifact,
            dependency_kind="card_projection_snapshot",
            stage_version_id=_projection_stage_version_id(
                connection, artifact.projection_artifact_id
            ),
        )
        artifacts.append(artifact)
    return tuple(artifacts)


def build_claim_projections(
    connection: sqlite3.Connection,
    *,
    contract: ProcessorContractSpec = DEFAULT_CLAIM_PROJECTION_CONTRACT,
) -> tuple[ProjectionArtifact, ...]:
    """Build one source-preserving projection for every active effective claim group."""
    activate_processor_contract(
        connection,
        stage_kind=CLAIM_PROJECTION_STAGE_KIND,
        contract=contract,
    )
    group_rows = connection.execute(
        """
        SELECT DISTINCT group_table.claim_group_id, group_table.canonical_claim_json
        FROM claim_groups AS group_table
        JOIN effective_claim_group_memberships AS membership
          ON membership.claim_group_id = group_table.claim_group_id
        JOIN effective_active_occurrences AS occurrence
          ON occurrence.occurrence_version_id =
             membership.occurrence_version_id
        ORDER BY group_table.claim_group_id
        """
    ).fetchall()
    artifacts: list[ProjectionArtifact] = []
    for group_row in group_rows:
        group_id = group_row["claim_group_id"]
        occurrences = _claim_group_occurrences(connection, group_id)
        dependencies: set[DependencyKey] = set()
        source_refs: list[dict[str, object]] = []
        for occurrence in occurrences:
            lineage_id = occurrence["source_lineage_id"]
            card_revision_id = occurrence["current_card_revision_id"]
            dependencies.add(DependencyKey("effective_claim_groups", lineage_id))
            dependencies.add(
                DependencyKey("card_projection_snapshot", card_revision_id)
            )
            source_refs.append(
                _source_ref_from_payload(
                    card_revision_id=card_revision_id,
                    source_lineage_id=lineage_id,
                    payload=json.loads(occurrence["card_payload_json"]),
                )
            )
        claim = json.loads(group_row["canonical_claim_json"])
        claim_text = _claim_text(claim)
        rendered = _render_claim_projection(
            claim_group_id=group_id,
            claim_text=claim_text,
            field_kind=str(claim.get("field_kind") or "claim"),
            source_refs=_dedupe_source_refs(source_refs),
        )
        lineage = ensure_source_lineage(
            connection,
            source_kind="wiki_claim",
            external_key=group_id,
            source_lineage_id=deterministic_source_lineage_id(
                source_kind="wiki_claim",
                external_key=group_id,
            ),
        )
        artifact = _build_projection(
            connection,
            projection_kind="claim",
            scope_key=group_id,
            source_lineage_id=lineage.source_lineage_id,
            stage_kind=CLAIM_PROJECTION_STAGE_KIND,
            input_kinds=(),
            dependencies=tuple(sorted(dependencies)),
            contract=contract,
            title=claim_text[:160] or "Claim",
            rendered_content=rendered,
            search_text="\n".join(
                [
                    claim_text,
                    str(claim.get("field_kind") or ""),
                    *(
                        str(item.get("title") or item.get("source_id") or "")
                        for item in source_refs
                    ),
                ]
            ),
            claim_group_id=group_id,
        )
        _publish_projection_dependency(
            connection,
            artifact=artifact,
            dependency_kind="claim_projection_snapshot",
            stage_version_id=_projection_stage_version_id(
                connection, artifact.projection_artifact_id
            ),
        )
        artifacts.append(artifact)
    return tuple(artifacts)


def build_hub_projections(
    connection: sqlite3.Connection,
    *,
    contract: ProcessorContractSpec = DEFAULT_HUB_PROJECTION_CONTRACT,
) -> tuple[ProjectionArtifact, ...]:
    """Build hubs only for approved concepts that currently have related cards."""
    activate_processor_contract(
        connection,
        stage_kind=HUB_PROJECTION_STAGE_KIND,
        contract=contract,
    )
    artifacts: list[ProjectionArtifact] = []
    for concept in list_concepts(connection):
        cards = _concept_cards(connection, concept.concept_id)
        if not cards:
            continue
        claim_groups = _concept_claim_groups(connection, concept.concept_id)
        aliases = [
            dict(row)
            for row in connection.execute(
                """
                SELECT display_surface, alias_kind
                FROM identity_aliases
                WHERE concept_id = ?
                ORDER BY alias_kind, display_surface
                """,
                (concept.concept_id,),
            ).fetchall()
        ]
        hierarchy = _concept_hierarchy(connection, concept.concept_id)
        sidecar = get_manual_sidecar(connection, concept.concept_id)
        dependencies: set[DependencyKey] = {
            DependencyKey("registry_snapshot", concept.concept_id),
            DependencyKey("concept_display_snapshot", concept.concept_id),
            DependencyKey(
                "approved_identity_alias_snapshot", concept.concept_id
            ),
            DependencyKey("hierarchy_snapshot", concept.concept_id),
            DependencyKey("manual_sidecar", concept.concept_id),
        }
        for card in cards:
            lineage_id = card["source_lineage_id"]
            card_revision_id = card["card_revision_id"]
            dependencies.update(
                {
                    DependencyKey("effective_concept_links", lineage_id),
                    DependencyKey("effective_claim_groups", lineage_id),
                    DependencyKey("eligibility_state", lineage_id),
                    DependencyKey(
                        "card_projection_snapshot", card_revision_id
                    ),
                }
            )
        for claim_group in claim_groups:
            dependencies.add(
                DependencyKey(
                    "claim_projection_snapshot",
                    claim_group["claim_group_id"],
                )
            )
        rendered = _render_hub_projection(
            concept=concept,
            aliases=aliases,
            hierarchy=hierarchy,
            sidecar_markdown=sidecar.markdown_text,
            claim_groups=claim_groups,
            cards=cards,
        )
        search_text = _hub_search_text(
            concept=concept,
            aliases=aliases,
            claim_groups=claim_groups,
            cards=cards,
            sidecar_markdown=sidecar.markdown_text,
        )
        lineage = ensure_source_lineage(
            connection,
            source_kind="wiki_hub",
            external_key=concept.concept_id,
            source_lineage_id=deterministic_source_lineage_id(
                source_kind="wiki_hub",
                external_key=concept.concept_id,
            ),
        )
        artifact = _build_projection(
            connection,
            projection_kind="hub",
            scope_key=concept.concept_id,
            source_lineage_id=lineage.source_lineage_id,
            stage_kind=HUB_PROJECTION_STAGE_KIND,
            input_kinds=(),
            dependencies=tuple(sorted(dependencies)),
            contract=contract,
            title=concept.canonical_label,
            rendered_content=rendered,
            search_text=search_text,
            concept_id=concept.concept_id,
        )
        artifacts.append(artifact)
    return tuple(artifacts)


def rebuild_wiki_fts(connection: sqlite3.Connection) -> int:
    """Rebuild Wiki FTS exclusively from current, eligible projection heads."""
    documents: list[tuple[str, str, str, str, str, str, str]] = []
    card_rows = connection.execute(
        """
        SELECT
            artifact.projection_scope_key,
            artifact.search_text,
            artifact.projection_output_hash,
            card.canonical_payload_json,
            lineage.source_lineage_id
        FROM projection_heads AS head
        JOIN projection_artifacts AS artifact
          ON artifact.projection_artifact_id =
             head.current_projection_artifact_id
        JOIN card_revisions AS card
          ON card.card_revision_id = artifact.card_revision_id
        JOIN source_lineage_heads AS card_head
          ON card_head.current_card_revision_id = card.card_revision_id
        JOIN source_lineages AS lineage
          ON lineage.source_lineage_id = card.source_lineage_id
        JOIN eligibility_heads AS eligibility
          ON eligibility.source_lineage_id = lineage.source_lineage_id
         AND eligibility.evaluated_card_revision_id = card.card_revision_id
        WHERE head.projection_kind = 'card'
          AND eligibility.current_eligible = 1
        ORDER BY artifact.projection_scope_key
        """
    ).fetchall()
    for row in card_rows:
        payload = json.loads(row["canonical_payload_json"])
        source_ref = _source_ref_from_payload(
            card_revision_id=row["projection_scope_key"],
            source_lineage_id=row["source_lineage_id"],
            payload=payload,
        )
        documents.append(
            (
                f"card:{row['projection_scope_key']}",
                "card",
                row["projection_scope_key"],
                str(source_ref.get("title") or source_ref.get("source_id") or "Card"),
                row["search_text"],
                canonical_json(source_ref),
                row["projection_output_hash"],
            )
        )

    claim_rows = connection.execute(
        """
        SELECT
            artifact.projection_scope_key,
            artifact.search_text,
            artifact.projection_output_hash,
            group_table.canonical_claim_json
        FROM projection_heads AS head
        JOIN projection_artifacts AS artifact
          ON artifact.projection_artifact_id =
             head.current_projection_artifact_id
        JOIN claim_groups AS group_table
          ON group_table.claim_group_id = artifact.claim_group_id
        WHERE head.projection_kind = 'claim'
          AND EXISTS (
              SELECT 1
              FROM effective_claim_group_memberships AS membership
              JOIN effective_active_occurrences AS occurrence
                ON occurrence.occurrence_version_id =
                   membership.occurrence_version_id
              WHERE membership.claim_group_id = artifact.claim_group_id
          )
        ORDER BY artifact.projection_scope_key
        """
    ).fetchall()
    for row in claim_rows:
        claim_id = row["projection_scope_key"]
        refs = [
            _source_ref_from_payload(
                card_revision_id=item["current_card_revision_id"],
                source_lineage_id=item["source_lineage_id"],
                payload=json.loads(item["card_payload_json"]),
            )
            for item in _claim_group_occurrences(connection, claim_id)
        ]
        documents.append(
            (
                f"claim:{claim_id}",
                "claim",
                claim_id,
                _claim_text(json.loads(row["canonical_claim_json"]))[:160],
                row["search_text"],
                canonical_json(_dedupe_source_refs(refs)),
                row["projection_output_hash"],
            )
        )

    hub_rows = connection.execute(
        """
        SELECT
            artifact.projection_scope_key,
            artifact.search_text,
            artifact.projection_output_hash,
            revision.canonical_payload_json
        FROM projection_heads AS head
        JOIN projection_artifacts AS artifact
          ON artifact.projection_artifact_id =
             head.current_projection_artifact_id
        JOIN concept_heads AS concept_head
          ON concept_head.concept_id = artifact.concept_id
        JOIN concept_revisions AS revision
          ON revision.concept_revision_id =
             concept_head.current_concept_revision_id
        WHERE head.projection_kind = 'hub'
          AND EXISTS (
              SELECT 1
              FROM effective_card_relations AS relation
              JOIN source_lineage_heads AS card_head
                ON card_head.current_card_revision_id =
                   relation.card_revision_id
              JOIN eligibility_heads AS eligibility
                ON eligibility.source_lineage_id =
                   card_head.source_lineage_id
               AND eligibility.evaluated_card_revision_id =
                   relation.card_revision_id
              WHERE relation.concept_id = artifact.concept_id
                AND eligibility.current_eligible = 1
          )
        ORDER BY artifact.projection_scope_key
        """
    ).fetchall()
    for row in hub_rows:
        payload = json.loads(row["canonical_payload_json"])
        concept_id = row["projection_scope_key"]
        cards = _concept_cards(connection, concept_id)
        source_refs = [
            {
                key: value
                for key, value in card.items()
                if key
                in {
                    "card_revision_id",
                    "source_lineage_id",
                    "source_id",
                    "title",
                    "date",
                    "url",
                    "start_seconds",
                    "end_seconds",
                }
            }
            for card in cards
        ]
        documents.append(
            (
                f"hub:{concept_id}",
                "hub",
                concept_id,
                str(payload.get("canonical_label") or concept_id),
                row["search_text"],
                canonical_json(
                    {
                        "concept_id": concept_id,
                        "sources": _dedupe_source_refs(source_refs),
                    }
                ),
                row["projection_output_hash"],
            )
        )

    with _immediate_transaction(connection):
        connection.execute("DELETE FROM wiki_fts_documents")
        connection.executemany(
            """
            INSERT INTO wiki_fts_documents (
                document_key,
                document_kind,
                scope_key,
                title,
                body,
                source_ref_json,
                projection_output_hash,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(*document, _utc_now()) for document in documents],
        )
    return len(documents)


def write_generated_hubs(
    connection: sqlite3.Connection,
    output_directory: str | Path,
) -> GeneratedHubStats:
    """Materialize current hub artifacts; files are explicitly disposable."""
    root = Path(output_directory)
    root.mkdir(parents=True, exist_ok=True)
    rows = connection.execute(
        """
        SELECT
            artifact.concept_id,
            artifact.rendered_content,
            artifact.projection_output_hash,
            revision.canonical_payload_json
        FROM projection_heads AS head
        JOIN projection_artifacts AS artifact
          ON artifact.projection_artifact_id =
             head.current_projection_artifact_id
        JOIN concept_heads AS concept_head
          ON concept_head.concept_id = artifact.concept_id
        JOIN concept_revisions AS revision
          ON revision.concept_revision_id =
             concept_head.current_concept_revision_id
        WHERE head.projection_kind = 'hub'
          AND EXISTS (
              SELECT 1
              FROM effective_card_relations AS relation
              JOIN source_lineage_heads AS card_head
                ON card_head.current_card_revision_id =
                   relation.card_revision_id
              JOIN eligibility_heads AS eligibility
                ON eligibility.source_lineage_id =
                   card_head.source_lineage_id
               AND eligibility.evaluated_card_revision_id =
                   relation.card_revision_id
              WHERE relation.concept_id = artifact.concept_id
                AND eligibility.current_eligible = 1
          )
        ORDER BY artifact.concept_id
        """
    ).fetchall()
    expected: dict[Path, str] = {}
    index_entries: list[tuple[str, str]] = []
    for row in rows:
        payload = json.loads(row["canonical_payload_json"])
        label = str(payload.get("canonical_label") or row["concept_id"])
        filename = _hub_filename(label, row["concept_id"])
        content = (
            "---\n"
            f"{_GENERATED_MARKER}\n"
            f"concept_id: {row['concept_id']}\n"
            f"projection_output_hash: {row['projection_output_hash']}\n"
            "---\n"
            f"{row['rendered_content'].lstrip()}"
        )
        expected[root / filename] = content
        index_entries.append((label, filename))
    index_content = (
        "---\n"
        f"{_GENERATED_MARKER}\n"
        "document_kind: index\n"
        "---\n"
        "# GeoSpoiler Wiki\n\n"
        + "\n".join(
            f"- [{label}]({filename})"
            for label, filename in sorted(
                index_entries, key=lambda item: item[0].casefold()
            )
        )
        + ("\n" if index_entries else "")
    )
    expected[root / "README.md"] = index_content

    written = 0
    for path, content in expected.items():
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old != content:
            _atomic_write_text(path, content)
            written += 1

    removed = 0
    for path in root.glob("*.md"):
        if path in expected:
            continue
        try:
            prefix = path.read_text(encoding="utf-8")[:512]
        except (OSError, UnicodeError):
            continue
        if _GENERATED_MARKER in prefix:
            path.unlink()
            removed += 1
    return GeneratedHubStats(files_written=written, stale_files_removed=removed)


def get_projection_artifact(
    connection: sqlite3.Connection,
    *,
    projection_kind: ProjectionKind,
    scope_key: str,
) -> ProjectionArtifact | None:
    row = connection.execute(
        """
        SELECT artifact.*
        FROM projection_heads AS head
        JOIN projection_artifacts AS artifact
          ON artifact.projection_artifact_id =
             head.current_projection_artifact_id
        WHERE head.projection_kind = ?
          AND head.projection_scope_key = ?
        """,
        (projection_kind, scope_key),
    ).fetchone()
    return None if row is None else _projection_from_row(row, changed=False)


def _build_projection(
    connection: sqlite3.Connection,
    *,
    projection_kind: ProjectionKind,
    scope_key: str,
    source_lineage_id: str,
    stage_kind: str,
    input_kinds: Sequence[str],
    dependencies: Sequence[DependencyKey],
    contract: ProcessorContractSpec,
    title: str,
    rendered_content: str,
    search_text: str,
    card_revision_id: str | None = None,
    concept_id: str | None = None,
    claim_group_id: str | None = None,
) -> ProjectionArtifact:
    activate_processor_contract(
        connection,
        stage_kind=stage_kind,
        contract=contract,
    )
    stage = schedule_stage(
        connection,
        source_lineage_id=source_lineage_id,
        stage_kind=stage_kind,
        input_kinds=input_kinds,
        dependencies=dependencies,
    )
    run = start_stage_run(
        connection,
        stage_version_id=stage.stage_version_id,
        idempotency_key=(
            f"wiki-projection:{projection_kind}:{scope_key}:"
            f"{stage.stage_version_id}"
        ),
        artifact_source_card_revision_id=card_revision_id,
    )
    committed = commit_stage_run(connection, stage_run_id=run.stage_run_id)
    if committed.status not in {"committed", "no_op"}:
        raise ProjectionError(
            f"{projection_kind} projection stage is {committed.status}"
        )
    return _store_projection_artifact(
        connection,
        projection_kind=projection_kind,
        scope_key=scope_key,
        stage=stage,
        title=title,
        rendered_content=_normalize_rendered(rendered_content),
        search_text=_normalize_search(search_text),
        card_revision_id=card_revision_id,
        concept_id=concept_id,
        claim_group_id=claim_group_id,
    )


def _store_projection_artifact(
    connection: sqlite3.Connection,
    *,
    projection_kind: ProjectionKind,
    scope_key: str,
    stage: StageVersion,
    title: str,
    rendered_content: str,
    search_text: str,
    card_revision_id: str | None,
    concept_id: str | None,
    claim_group_id: str | None,
) -> ProjectionArtifact:
    inputs_hash = content_hash(
        {
            "stage_inputs_hash": stage.stage_inputs_hash,
            "processor_contract_hash": stage.processor_contract_hash,
        },
        namespace=f"wiki-v2-{projection_kind}-projection-inputs",
    )
    output_hash = content_hash(
        {"rendered_content": rendered_content},
        namespace=f"wiki-v2-{projection_kind}-projection-output",
    )
    fts_hash = content_hash(
        {"title": title, "search_text": search_text},
        namespace=f"wiki-v2-{projection_kind}-fts-document",
    )
    with _immediate_transaction(connection):
        if not _stage_is_current(
            connection,
            source_lineage_id=stage.source_lineage_id,
            stage_kind=stage.stage_kind,
            stage_version_id=stage.stage_version_id,
        ):
            raise ProjectionError("Projection stage became stale before artifact apply")
        current = connection.execute(
            """
            SELECT artifact.*
            FROM projection_heads AS head
            JOIN projection_artifacts AS artifact
              ON artifact.projection_artifact_id =
                 head.current_projection_artifact_id
            WHERE head.projection_kind = ?
              AND head.projection_scope_key = ?
            """,
            (projection_kind, scope_key),
        ).fetchone()
        if (
            current is not None
            and current["projection_inputs_hash"] == inputs_hash
            and current["projection_output_hash"] == output_hash
            and current["fts_document_hash"] == fts_hash
        ):
            return _projection_from_row(current, changed=False)
        generation = (
            1 if current is None else int(current["projection_generation"]) + 1
        )
        artifact_id = (
            "projection-artifact:v1:sha256:"
            + sha256_hex(
                canonical_json(
                    {
                        "projection_kind": projection_kind,
                        "scope_key": scope_key,
                        "generation": generation,
                        "inputs_hash": inputs_hash,
                        "output_hash": output_hash,
                        "fts_hash": fts_hash,
                    }
                )
            )
        )
        now = _utc_now()
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                rendered_content,
                search_text,
                stage.processor_contract_version_id,
                stage.stage_version_id,
                now,
            ),
        )
        if current is None:
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    projection_kind,
                    scope_key,
                    card_revision_id,
                    concept_id,
                    claim_group_id,
                    artifact_id,
                    generation,
                    inputs_hash,
                    output_hash,
                    fts_hash,
                    now,
                ),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE projection_heads
                SET
                    card_revision_id = ?,
                    concept_id = ?,
                    claim_group_id = ?,
                    current_projection_artifact_id = ?,
                    current_projection_generation = ?,
                    current_projection_inputs_hash = ?,
                    current_projection_output_hash = ?,
                    current_fts_document_hash = ?,
                    updated_at = ?
                WHERE projection_kind = ?
                  AND projection_scope_key = ?
                  AND current_projection_artifact_id = ?
                """,
                (
                    card_revision_id,
                    concept_id,
                    claim_group_id,
                    artifact_id,
                    generation,
                    inputs_hash,
                    output_hash,
                    fts_hash,
                    now,
                    projection_kind,
                    scope_key,
                    current["projection_artifact_id"],
                ),
            )
            if cursor.rowcount != 1:
                raise ProjectionError("Projection head changed concurrently")
        return ProjectionArtifact(
            projection_artifact_id=artifact_id,
            projection_kind=projection_kind,
            scope_key=scope_key,
            generation=generation,
            inputs_hash=inputs_hash,
            output_hash=output_hash,
            fts_document_hash=fts_hash,
            rendered_content=rendered_content,
            search_text=search_text,
            changed=True,
        )


def _publish_projection_dependency(
    connection: sqlite3.Connection,
    *,
    artifact: ProjectionArtifact,
    dependency_kind: str,
    stage_version_id: str,
) -> bool:
    current = get_dependency_head(
        connection,
        dependency_kind=dependency_kind,
        dependency_scope_key=artifact.scope_key,
    )
    published = publish_dependency(
        connection,
        dependency_kind=dependency_kind,
        dependency_scope_key=artifact.scope_key,
        payload={
            "projection_kind": artifact.projection_kind,
            "scope_key": artifact.scope_key,
            "projection_generation": artifact.generation,
            "projection_output_hash": artifact.output_hash,
            "fts_document_hash": artifact.fts_document_hash,
        },
        expected_version_id=(
            None if current is None else current.dependency_version_id
        ),
        producer_kind="stage",
        produced_by_stage_version_id=stage_version_id,
    )
    return published.changed


def _projection_stage_version_id(
    connection: sqlite3.Connection,
    artifact_id: str,
) -> str:
    row = connection.execute(
        """
        SELECT produced_by_stage_version_id
        FROM projection_artifacts
        WHERE projection_artifact_id = ?
        """,
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise ProjectionError(f"Unknown projection artifact {artifact_id}")
    return row["produced_by_stage_version_id"]


def _stage_is_current(
    connection: sqlite3.Connection,
    *,
    source_lineage_id: str,
    stage_kind: str,
    stage_version_id: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM lineage_stage_heads AS stage_head
        JOIN lineage_stage_versions AS stage
          ON stage.stage_version_id = stage_head.current_stage_version_id
        JOIN active_processor_contract_heads AS contract_head
          ON contract_head.stage_kind = stage.stage_kind
         AND contract_head.current_activation_generation =
             stage.processor_contract_activation_generation
        WHERE stage_head.source_lineage_id = ?
          AND stage_head.stage_kind = ?
          AND stage_head.current_stage_version_id = ?
          AND NOT EXISTS (
              SELECT 1
              FROM lineage_stage_input_bindings AS binding
              LEFT JOIN lineage_input_heads AS input_head
                ON input_head.source_lineage_id = binding.source_lineage_id
               AND input_head.input_kind = binding.input_kind
              WHERE binding.stage_version_id = stage.stage_version_id
                AND (
                    input_head.current_input_version_id IS NULL
                    OR input_head.current_input_version_id <>
                       binding.input_version_id
                    OR input_head.current_input_generation <>
                       binding.input_generation
                    OR input_head.current_input_hash <> binding.input_hash
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM stage_dependency_bindings AS binding
              LEFT JOIN dependency_heads AS dependency_head
                ON dependency_head.dependency_kind = binding.dependency_kind
               AND dependency_head.dependency_scope_key =
                   binding.dependency_scope_key
              WHERE binding.stage_version_id = stage.stage_version_id
                AND (
                    dependency_head.current_dependency_version_id IS NULL
                    OR dependency_head.current_dependency_version_id <>
                       binding.dependency_version_id
                    OR dependency_head.current_generation <>
                       binding.dependency_generation
                    OR dependency_head.current_hash <> binding.dependency_hash
                )
          )
        """,
        (source_lineage_id, stage_kind, stage_version_id),
    ).fetchone()
    return row is not None


def _claim_group_occurrences(
    connection: sqlite3.Connection,
    claim_group_id: str,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            occurrence.occurrence_version_id,
            occurrence.source_lineage_id,
            card_head.current_card_revision_id,
            card.canonical_payload_json AS card_payload_json
        FROM effective_claim_group_memberships AS membership
        JOIN effective_active_occurrences AS occurrence
          ON occurrence.occurrence_version_id =
             membership.occurrence_version_id
        JOIN source_lineage_heads AS card_head
          ON card_head.source_lineage_id = occurrence.source_lineage_id
        JOIN card_revisions AS card
          ON card.card_revision_id = card_head.current_card_revision_id
        WHERE membership.claim_group_id = ?
        ORDER BY occurrence.source_lineage_id, occurrence.occurrence_version_id
        """,
        (claim_group_id,),
    ).fetchall()


def _concept_cards(
    connection: sqlite3.Connection,
    concept_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT
            relation.card_revision_id,
            card.source_lineage_id,
            relation.relation_kind,
            relation.strongest_relation_role,
            card.canonical_payload_json
        FROM effective_card_relations AS relation
        JOIN card_revisions AS card
          ON card.card_revision_id = relation.card_revision_id
        JOIN source_lineage_heads AS card_head
          ON card_head.source_lineage_id = card.source_lineage_id
         AND card_head.current_card_revision_id = relation.card_revision_id
        JOIN eligibility_heads AS eligibility
          ON eligibility.source_lineage_id = card.source_lineage_id
         AND eligibility.evaluated_card_revision_id = relation.card_revision_id
        WHERE relation.concept_id = ?
          AND eligibility.current_eligible = 1
        ORDER BY
            CASE relation.relation_kind
                WHEN 'direct' THEN 0
                WHEN 'context' THEN 1
                ELSE 2
            END,
            relation.card_revision_id
        """,
        (concept_id,),
    ).fetchall()
    cards: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(row["canonical_payload_json"])
        ref = _source_ref_from_payload(
            card_revision_id=row["card_revision_id"],
            source_lineage_id=row["source_lineage_id"],
            payload=payload,
        )
        cards.append(
            {
                **ref,
                "relation_kind": row["relation_kind"],
                "strongest_relation_role": row["strongest_relation_role"],
            }
        )
    return cards


def _concept_claim_groups(
    connection: sqlite3.Connection,
    concept_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT
            membership.claim_group_id,
            group_table.canonical_claim_json,
            link.relation_role,
            occurrence.source_lineage_id,
            card_head.current_card_revision_id,
            card.canonical_payload_json
        FROM effective_occurrence_concept_links AS link
        JOIN effective_active_occurrences AS occurrence
          ON occurrence.occurrence_version_id = link.occurrence_version_id
        JOIN effective_claim_group_memberships AS membership
          ON membership.occurrence_version_id = occurrence.occurrence_version_id
        JOIN claim_groups AS group_table
          ON group_table.claim_group_id = membership.claim_group_id
        JOIN source_lineage_heads AS card_head
          ON card_head.source_lineage_id = occurrence.source_lineage_id
        JOIN card_revisions AS card
          ON card.card_revision_id = card_head.current_card_revision_id
        WHERE link.concept_id = ?
        ORDER BY membership.claim_group_id, occurrence.source_lineage_id
        """,
        (concept_id,),
    ).fetchall()
    grouped: dict[str, dict[str, object]] = {}
    direct_roles = {"subject", "actor", "object"}
    for row in rows:
        group_id = row["claim_group_id"]
        item = grouped.setdefault(
            group_id,
            {
                "claim_group_id": group_id,
                "claim_text": _claim_text(
                    json.loads(row["canonical_claim_json"])
                ),
                "roles": set(),
                "source_refs": [],
            },
        )
        roles = item["roles"]
        assert isinstance(roles, set)
        roles.add(row["relation_role"])
        refs = item["source_refs"]
        assert isinstance(refs, list)
        refs.append(
            _source_ref_from_payload(
                card_revision_id=row["current_card_revision_id"],
                source_lineage_id=row["source_lineage_id"],
                payload=json.loads(row["canonical_payload_json"]),
            )
        )
    result: list[dict[str, object]] = []
    for item in grouped.values():
        roles = set(item["roles"])
        refs = _dedupe_source_refs(item["source_refs"])
        result.append(
            {
                **item,
                "roles": sorted(roles),
                "relation_kind": (
                    "direct"
                    if roles & direct_roles
                    else "mentioned"
                    if roles == {"mentioned"}
                    else "context"
                ),
                "source_refs": refs,
                "source_count": len(refs),
            }
        )
    return sorted(
        result,
        key=lambda item: (
            0 if item["relation_kind"] == "direct" else 1,
            str(item["claim_text"]).casefold(),
        ),
    )


def _concept_hierarchy(
    connection: sqlite3.Connection,
    concept_id: str,
) -> dict[str, object]:
    parent = connection.execute(
        """
        SELECT revision.canonical_payload_json
        FROM effective_primary_hierarchy_edges AS edge
        JOIN concept_heads AS head
          ON head.concept_id = edge.parent_concept_id
        JOIN concept_revisions AS revision
          ON revision.concept_revision_id = head.current_concept_revision_id
        WHERE edge.child_concept_id = ?
        """,
        (concept_id,),
    ).fetchone()
    children = connection.execute(
        """
        SELECT edge.child_concept_id, revision.canonical_payload_json
        FROM effective_primary_hierarchy_edges AS edge
        JOIN concept_heads AS head
          ON head.concept_id = edge.child_concept_id
        JOIN concept_revisions AS revision
          ON revision.concept_revision_id = head.current_concept_revision_id
        WHERE edge.parent_concept_id = ?
        ORDER BY edge.child_concept_id
        """,
        (concept_id,),
    ).fetchall()
    related = connection.execute(
        """
        SELECT
            CASE
                WHEN edge.left_concept_id = ? THEN edge.right_concept_id
                ELSE edge.left_concept_id
            END AS related_concept_id
        FROM effective_related_concept_edges AS edge
        WHERE edge.left_concept_id = ? OR edge.right_concept_id = ?
        ORDER BY related_concept_id
        """,
        (concept_id, concept_id, concept_id),
    ).fetchall()
    related_items: list[dict[str, str]] = []
    for row in related:
        related_concept = get_concept(connection, row["related_concept_id"])
        related_items.append(
            {
                "concept_id": related_concept.concept_id,
                "label": related_concept.canonical_label,
            }
        )
    return {
        "parent": (
            None
            if parent is None
            else json.loads(parent["canonical_payload_json"]).get(
                "canonical_label"
            )
        ),
        "children": [
            {
                "concept_id": row["child_concept_id"],
                "label": json.loads(row["canonical_payload_json"]).get(
                    "canonical_label"
                ),
            }
            for row in children
        ],
        "related": related_items,
    }


def _render_card_projection(
    *,
    card_revision_id: str,
    projection_input: Mapping[str, object],
    card_payload: Mapping[str, object],
) -> tuple[str, str, str]:
    source = projection_input.get("display_source")
    source = source if isinstance(source, Mapping) else {}
    title = str(
        source.get("source_title")
        or source.get("title")
        or source.get("channel")
        or source.get("source_id")
        or source.get("segment_id")
        or card_revision_id
    )
    summary = str(projection_input.get("summary") or "").strip()
    key_points = projection_input.get("key_points")
    key_points = key_points if isinstance(key_points, list) else []
    topics = projection_input.get("topics")
    topics = topics if isinstance(topics, list) else []
    lines = [f"# {title}", "", f"`{card_revision_id}`"]
    url = source.get("post_url") or source.get("start_url")
    if url:
        lines.extend(["", f"Источник: {url}"])
    if summary:
        lines.extend(["", "## Кратко", "", summary])
    if key_points:
        lines.extend(["", "## Ключевые пункты", ""])
        lines.extend(
            f"- {_payload_text(item)}"
            for item in key_points
            if _payload_text(item)
        )
    if topics:
        lines.extend(["", "## Темы", ""])
        lines.append(
            ", ".join(
                _payload_text(item)
                for item in topics
                if _payload_text(item)
            )
        )
    search_parts = [
        title,
        summary,
        *(_payload_text(item) for item in key_points),
        *(_payload_text(item) for item in topics),
        *(
            str(item)
            for item in projection_input.get("search_phrases", [])
            if isinstance(item, str)
        ),
        str(card_payload.get("content_type") or ""),
        str(card_payload.get("language") or ""),
    ]
    return title, "\n".join(lines).rstrip() + "\n", "\n".join(search_parts)


def _render_claim_projection(
    *,
    claim_group_id: str,
    claim_text: str,
    field_kind: str,
    source_refs: Sequence[Mapping[str, object]],
) -> str:
    lines = [
        f"# {claim_text or 'Claim'}",
        "",
        f"- Тип: `{field_kind}`",
        f"- Claim group: `{claim_group_id}`",
        "",
        "## Источники",
        "",
    ]
    lines.extend(f"- {_format_source_ref(ref)}" for ref in source_refs)
    return "\n".join(lines).rstrip() + "\n"


def _render_hub_projection(
    *,
    concept: ApprovedConcept,
    aliases: Sequence[Mapping[str, object]],
    hierarchy: Mapping[str, object],
    sidecar_markdown: str,
    claim_groups: Sequence[Mapping[str, object]],
    cards: Sequence[Mapping[str, object]],
) -> str:
    lines = [f"# {concept.canonical_label}", ""]
    if concept.description:
        lines.extend([concept.description, ""])
    alias_labels = [
        str(item["display_surface"])
        for item in aliases
        if str(item["display_surface"]) != concept.canonical_label
    ]
    if alias_labels:
        lines.extend(
            [
                f"**Алиасы:** {', '.join(dict.fromkeys(alias_labels))}",
                "",
            ]
        )
    parent = hierarchy.get("parent")
    children = hierarchy.get("children") or []
    related = hierarchy.get("related") or []
    if parent or children or related:
        lines.extend(["## Навигация", ""])
        if parent:
            lines.append(f"- Родительская тема: {parent}")
        if children:
            lines.append(
                "- Подтемы: "
                + ", ".join(str(item["label"]) for item in children)
            )
        if related:
            lines.append(
                "- Связанные темы: "
                + ", ".join(str(item["label"]) for item in related)
            )
        lines.append("")
    if sidecar_markdown:
        lines.extend(["## Ручные заметки", "", sidecar_markdown.rstrip(), ""])

    direct_claims = [
        item for item in claim_groups if item["relation_kind"] == "direct"
    ]
    context_claims = [
        item for item in claim_groups if item["relation_kind"] != "direct"
    ]
    lines.extend(["## Основные утверждения", ""])
    if direct_claims:
        lines.extend(
            f"- {item['claim_text']} "
            f"([источников: {item['source_count']}])"
            for item in direct_claims
        )
    else:
        lines.append("- Пока нет прямых утверждений.")
    if context_claims:
        lines.extend(
            [
                "",
                "<details>",
                f"<summary>Контекстные утверждения и упоминания ({len(context_claims)})</summary>",
                "",
            ]
        )
        lines.extend(
            f"- {item['claim_text']} "
            f"([источников: {item['source_count']}])"
            for item in context_claims
        )
        lines.extend(["", "</details>"])

    direct_cards = [item for item in cards if item["relation_kind"] == "direct"]
    context_cards = [item for item in cards if item["relation_kind"] != "direct"]
    lines.extend(["", "## Связанные Enriched-карточки", "", "### Прямые", ""])
    lines.extend(
        (f"- {_format_source_ref(item)}" for item in direct_cards),
    )
    if not direct_cards:
        lines.append("- Нет.")
    if context_cards:
        lines.extend(
            [
                "",
                "<details>",
                f"<summary>Контекст и упоминания ({len(context_cards)})</summary>",
                "",
            ]
        )
        lines.extend(f"- {_format_source_ref(item)}" for item in context_cards)
        lines.extend(["", "</details>"])
    return "\n".join(lines).rstrip() + "\n"


def _hub_search_text(
    *,
    concept: ApprovedConcept,
    aliases: Sequence[Mapping[str, object]],
    claim_groups: Sequence[Mapping[str, object]],
    cards: Sequence[Mapping[str, object]],
    sidecar_markdown: str,
) -> str:
    return "\n".join(
        [
            concept.canonical_label,
            concept.description,
            *(str(item["display_surface"]) for item in aliases),
            *(str(item["claim_text"]) for item in claim_groups),
            *(
                str(item.get("title") or item.get("source_id") or "")
                for item in cards
            ),
            sidecar_markdown,
        ]
    )


def _source_ref_from_payload(
    *,
    card_revision_id: str,
    source_lineage_id: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    source = payload.get("source")
    source = source if isinstance(source, Mapping) else {}
    source_id = (
        source.get("source_id")
        or source.get("segment_id")
        or source.get("video_id")
        or source_lineage_id
    )
    title = (
        source.get("source_title")
        or source.get("title")
        or source.get("channel")
        or source_id
    )
    return {
        "card_revision_id": card_revision_id,
        "source_lineage_id": source_lineage_id,
        "source_id": source_id,
        "title": title,
        "date": source.get("date"),
        "url": source.get("post_url") or source.get("start_url"),
        "start_seconds": source.get("start_seconds"),
        "end_seconds": source.get("end_seconds"),
    }


def _dedupe_source_refs(
    values: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[object, object]] = set()
    for value in values:
        key = (value.get("source_lineage_id"), value.get("card_revision_id"))
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(value))
    return result


def _format_source_ref(value: Mapping[str, object]) -> str:
    title = str(value.get("title") or value.get("source_id") or "Источник")
    source_id = str(value.get("source_id") or "").strip()
    date = str(value.get("date") or "").strip()
    url = str(value.get("url") or "").strip()
    time_suffix = ""
    if value.get("start_seconds") is not None:
        time_suffix = f", {value['start_seconds']}s"
    label = f"{title} ({date}{time_suffix})" if date or time_suffix else title
    if source_id and source_id != title:
        label = f"{label} — `{source_id}`"
    return f"[{label}]({url})" if url else label


def _claim_text(claim: Mapping[str, object]) -> str:
    payload = claim.get("payload")
    if isinstance(payload, Mapping):
        return _payload_text(payload)
    return _payload_text(claim)


def _payload_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, Mapping):
        return ""
    for key in ("text", "summary", "title", "name", "topic"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ""


def _projection_from_row(
    row: sqlite3.Row,
    *,
    changed: bool,
) -> ProjectionArtifact:
    return ProjectionArtifact(
        projection_artifact_id=row["projection_artifact_id"],
        projection_kind=row["projection_kind"],
        scope_key=row["projection_scope_key"],
        generation=int(row["projection_generation"]),
        inputs_hash=row["projection_inputs_hash"],
        output_hash=row["projection_output_hash"],
        fts_document_hash=row["fts_document_hash"],
        rendered_content=row["rendered_content"],
        search_text=row["search_text"],
        changed=changed,
    )


def _normalize_rendered(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").strip()
    return f"{normalized}\n"


def _normalize_search(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def _hub_filename(label: str, concept_id: str) -> str:
    normalized = unicodedata.normalize("NFC", label).strip().lower()
    slug = "".join(character if character.isalnum() else "-" for character in normalized)
    slug = re.sub(r"-+", "-", slug).strip("-")[:80] or "hub"
    return f"{slug}--{sha256_hex(concept_id)[:12]}.md"


@contextmanager
def _immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
